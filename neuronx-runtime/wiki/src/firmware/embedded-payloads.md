# Embedded NCFW Payloads (8 RAW Xtensa Blobs)

> *All symbols, offsets, sizes, digests, and entropy figures on this page apply to the eight on-device firmware images embedded in `libncfw.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (ELF64 x86-64 DYN, 615,640 B, md5 `e01ea384a76e59d511b4f005b7db98ac`, SONAME `libncfw.so.2.31.1.0.cf13a49f`, build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`, **not** stripped). `.rodata` is at `0x65000` with VMA == file offset (`readelf -S`), so every blob symbol address below is **simultaneously an analysis VMA and a host file offset** — `dd skip=<addr>` carves the blob directly. Sizes are read from the adjacent `*_bin_size` u64 word; `sha256[0:16]` and entropy are recomputed from the carved bytes this pass. Other versions will differ.*
>
> *Evidence grade: **Confirmed (byte-anchored)** — all eight symbol addresses (`nm`), all eight sizes (size-word `struct.unpack`), all eight `sha256`, entropy, nonzero-fraction, the DRAM exception/SoC tables, the v4≡v4_plus DRAM identity, and the v4↔v4_plus IRAM diff window were re-derived from the binary and **agree byte-for-byte with [The NCFW Sequencer](ncfw-sequencer.md)** (zero CORRECTIONs). The on-device disassembly is owned there and not duplicated here. · Part X — Collectives Firmware (libncfw) · [back to index](../index.md)*

## Abstract

`libncfw.so` ships the NCFW ("Neuron Collective FirmWare") sync-core firmware as a **header-less, pre-split RAW blob store** baked into its `.rodata`. The cleanest familiar frame is a board-support package's embedded-firmware array — a flat table of `{code, data}` images keyed by an integer device generation — with two unusual properties. First, the images are **pre-split into IRAM (code) and DRAM (data) halves**: the host provider hands out a `{&iram, iram_size, &dram, dram_size}` quad, the on-device IRAM half is DMA'd into the NeuronCore TPB sequencer's instruction RAM and the DRAM half is written to HBM. Second, the format is **as raw as a firmware blob gets**: no ELF wrapper, no magic number, no length prefix, no checksum, no version field inside the bytes — byte 0 of an IRAM image is already the on-device packed XEA2 vector table the silicon fetches the instant reset deasserts, and byte 0 of a DRAM image is already the exception-dispatch table the sequencer reads from HBM.

Four arch generations each contribute one `{IRAM, DRAM}` pair, for **eight blobs** total, selected by the integer `nrtucode_coretype` key `5/12/20/28` (= `v2/v3/v4/v4_plus`). This page is the **carve and inventory** page: it pins every blob to its `nm`-`'r'` symbol, host offset, size, `sha256` digest, Shannon entropy, and coretype key; documents the carve method and the IRAM/DRAM split; and summarizes the per-arch byte-delta structure — which arches share which regions. It does **not** disassemble the IRAM (the reset vector, boot path, idle dispatcher, exception handlers, and opaque sequencer TIE are owned by [The NCFW Sequencer](ncfw-sequencer.md)); it establishes the *container model* the disassembly page then decodes.

The carve is the whole container model. Because there is no header to parse, a reimplementer's loader is trivial — `memcpy`/DMA `*_bin` of length `*_bin_size` — but the *consequence* is strict: the blobs must be self-contained and position-correct, every IRAM image must begin with a valid packed XEA2 vector table, and the only metadata that exists lives in the **host-side symbol table and size words**, not in the payload. The entropy split is the fastest self-check: an IRAM blob measures **H ≈ 6.4–6.8 b/B** (real Xtensa code, 74–84 % nonzero) while a DRAM blob measures **H ≈ 0.13–0.15 b/B** (a sparse table — ~1.0–1.2 % nonzero, all payload in the first `~0x240`/`~0x3f4` bytes, the rest a zero-filled BSS template). Mixing the two halves up is the easiest way to brick the upload, and the entropy figure catches it instantly.

For reimplementation, the contract is:

- **The blob-store layout** — eight `nm`-`'r'` symbols in `.rodata` (`v{2,3,4,4_plus}_ncfw_{iram,dram}_bin` + the four-byte-true `*_bin_size` words), their addresses/sizes/digests, and the rule that the provider returns `{ptr, len}` pairs into `.rodata` with **no copy and no container**.
- **The RAW format invariant** — no ELF, no magic, no checksum: IRAM byte 0 is the packed XEA2 vector table, DRAM byte 0 is the exception-dispatch table; the only metadata is host-side (symbol + size word).
- **The coretype keying** — `5/12/20/28` → `v2/v3/v4/v4_plus`, the same key that drives the [Carrier Library](carrier-library.md)'s `get_image` switch.
- **The dedup/delta model** — `v4_dram == v4_plus_dram` byte-for-byte; `v4_iram != v4_plus_iram` but differs **only** inside the handler window `0x1190..0x4ab0`; v2/v3 IRAM share a byte-identical reset prologue, as do v4/v4+ (the `+0x1c` reset shift separates the two camps).

| | |
|---|---|
| **Carrier** | `libncfw.so`, build-id `a98f8e1c…`, SONAME `libncfw.so.2.31.1.0.cf13a49f`, md5 `e01ea384…` |
| **Store location** | `.rodata` @ `0x65000` (VMA == file offset); blobs span `0x66a60`..`0x918e0` |
| **Blob count** | **8** = `{IRAM, DRAM}` × `{v2, v3, v4, v4_plus}`, eight `nm`-`'r'` symbols |
| **Format** | RAW — **no ELF, no magic, no length prefix, no checksum**; metadata is host-side only |
| **Selector** | `nrtucode_coretype` ∈ `{5, 12, 20, 28}` → `{v2, v3, v4, v4_plus}` |
| **IRAM entropy** | H ≈ **6.40–6.81** b/B (74–84 % nonzero) — real Xtensa LX code |
| **DRAM entropy** | H ≈ **0.13–0.15** b/B (1.05–1.22 % nonzero) — sparse table + zero BSS template |
| **Largest blob** | `v2_ncfw_iram_bin` — 43,232 B (`0xa8e0`), H ≈ 6.81 |
| **DRAM dedup** | `v4_ncfw_dram_bin` == `v4_plus_ncfw_dram_bin` (sha `1c3ac5f4…`, byte-identical) |
| **Provider** | `libncfw_get_image` @`0x1179` → `{&iram, iram_size, &dram, dram_size}` (no copy) |

---

## 1. The Blob Store

### Purpose

A reimplementer must first answer three questions before touching the on-device code: which bytes are an image, how long is it, and which generation does it serve. All three are answered host-side, not by parsing the payload. Each image is a single `nm`-`'r'` (read-only) symbol in `.rodata`; its length is the adjacent `*_bin_size` u64 word; its generation is the position in the `5/12/20/28` provider switch. There is no on-disk index, manifest, or section table beyond the ELF symbol table itself.

### The eight symbols

`.rodata` opens at `0x65000` with VMA == file offset (`readelf -S` confirms `[16] .rodata PROGBITS 0x65000 00065000`), so each symbol address is also the host file offset. The eight images and their four size words interleave as `dram, dram_size, iram, iram_size` per arch, in arch order `v2 → v3 → v4 → v4_plus`:

```text
.rodata 0x65000  ──────────────────────────────────────────────── VMA == file offset
  0x66a60  v2_ncfw_dram_bin        (14016 B) ─┐
  0x6a120  v2_ncfw_dram_bin_size   = 0x36c0   │  v2 / sunda
  0x6a140  v2_ncfw_iram_bin        (43232 B)  │
  0x74a20  v2_ncfw_iram_bin_size   = 0xa8e0  ─┘
  0x74a40  v3_ncfw_dram_bin        (19968 B) ─┐
  0x79840  v3_ncfw_dram_bin_size   = 0x4e00   │  v3 / cayman
  0x79860  v3_ncfw_iram_bin        (19392 B)  │
  0x7e420  v3_ncfw_iram_bin_size   = 0x4bc0  ─┘
  0x7e440  v4_ncfw_dram_bin        (19968 B) ─┐
  0x83240  v4_ncfw_dram_bin_size   = 0x4e00   │  v4 / mariana
  0x83260  v4_ncfw_iram_bin        (19488 B)  │
  0x87e80  v4_ncfw_iram_bin_size   = 0x4c20  ─┘
  0x87ea0  v4_plus_ncfw_dram_bin   (19968 B) ─┐
  0x8cca0  v4_plus_ncfw_dram_bin_size= 0x4e00 │  v4_plus / mariana_plus
  0x8ccc0  v4_plus_ncfw_iram_bin   (19488 B)  │
  0x918e0  v4_plus_ncfw_iram_bin_size         ─┘  (u64 hi bytes overlap SONAME tail — read low u32)
```

### Considerations

The blob store has no relocation, no compression, and no integrity field — the carrier is a passive provider, and the integrity guarantee is whatever the ELF on-disk image and the upload DMA provide. The consumer side (`libncfw_get_image`'s switch, the `dlopen`/`dlsym` handshake, the DMA-to-IRAM and DRAM-to-HBM upload, and the reset release) is owned by [The Carrier Library](carrier-library.md) and [Firmware Upload Path](upload-path.md); this page owns only the bytes and their carve.

---

## 2. The Blob Inventory

### The eight blobs

Carved this pass from `.rodata` (VMA == file offset); size from each `*_bin_size` u64 word (low u32, see the GOTCHA); `sha256[0:16]` and entropy recomputed from the carved bytes. Confidence is **HIGH** for every row — addresses (`nm`), sizes (size-word read), digests (`sha256`), and entropy all reproduce against the binary and agree with the sequencer page.

| Arch | Half | Symbol (`nm` `'r'`) | Host off / VMA | Size (B) | hex | `sha256[0:16]` | Entropy (b/B) | coretype key | Confidence |
|---|---|---|---|---|---|---|---|---|---|
| v2 / sunda | DRAM | `v2_ncfw_dram_bin` | `0x66a60` | 14,016 | `0x36c0` | `ca01951124e505b6` | 0.13 (1.05 % nz) | `5` | HIGH |
| v2 / sunda | IRAM | `v2_ncfw_iram_bin` | `0x6a140` | 43,232 | `0xa8e0` | `e379980b7ec3f2fe` | **6.81** (84.1 % nz) | `5` | HIGH |
| v3 / cayman | DRAM | `v3_ncfw_dram_bin` | `0x74a40` | 19,968 | `0x4e00` | `2418ab0f6350ce93` | 0.15 (1.22 % nz) | `12` | HIGH |
| v3 / cayman | IRAM | `v3_ncfw_iram_bin` | `0x79860` | 19,392 | `0x4bc0` | `d7bc8b814b03c1f0` | 6.42 (74.5 % nz) | `12` | HIGH |
| v4 / mariana | DRAM | `v4_ncfw_dram_bin` | `0x7e440` | 19,968 | `0x4e00` | `1c3ac5f445865844` | 0.15 (1.22 % nz) | `20` | HIGH |
| v4 / mariana | IRAM | `v4_ncfw_iram_bin` | `0x83260` | 19,488 | `0x4c20` | `ed8eed3429da3834` | 6.44 (75.0 % nz) | `20` | HIGH |
| v4_plus / mariana_plus | DRAM | `v4_plus_ncfw_dram_bin` | `0x87ea0` | 19,968 | `0x4e00` | `1c3ac5f445865844` | 0.15 (1.22 % nz) | `28` | HIGH |
| v4_plus / mariana_plus | IRAM | `v4_plus_ncfw_iram_bin` | `0x8ccc0` | 19,488 | `0x4c20` | `abc4d4521dd857ab` | 6.40 (74.3 % nz) | `28` | HIGH |

> **NOTE —** the entropy figure alone classifies a blob: IRAM H ≈ 6.4–6.8 b/B is dense Xtensa LX code; DRAM H ≈ 0.13–0.15 b/B is a sparse table (≈ 99 % zero). The v4 IRAM digest `ed8eed34…` and entropy 6.44 b/B reproduce the sequencer page's focus image (which rounds to 6.45 — a rounding difference, not a disagreement). CONFIDENCE: **HIGH** (sha256, sizes, entropy all re-derived).

> **GOTCHA —** the `v4_plus_ncfw_iram_bin_size` symbol's u64 word reads `0x3b031b0100004c20`: its **high four bytes overlap the SONAME string tail** that follows in `.rodata`. The true length is the **low u32 only** — `0x4c20 = 19,488`, byte-identical to v4. A loader that reads the full u64 size gets `0x3b031b0100004c20` ≈ 4.2 EB and faults. Read every `*_bin_size` as a u32 for safety; this is the only symbol where it matters, but it is mandatory there. CONFIDENCE: **HIGH** (byte-read of the word; v4_plus IRAM otherwise carves cleanly at `0x4c20`).

### The coretype key

The arch dimension is not in the blob — it is the integer the provider switches on. The same `nrtucode_coretype` key (`5/12/20/28`) selects both the firmware blob (`get_image`) and the JSON serializer (`ctx_log`); the silicon codenames are recovered from the `ctx_log` symbol strings inside the binary (canonical mapping in [Coretype Numbering Reconciliation](../arch/coretype-numbering.md)).

| coretype key | Generation | Codename (INFERRED) | Blob prefix | IRAM `sha256[0:16]` | DRAM `sha256[0:16]` |
|---|---|---|---|---|---|
| `5` | v2 | sunda | `v2_ncfw_*` | `e379980b…` | `ca019511…` |
| `12` | v3 | cayman | `v3_ncfw_*` | `d7bc8b81…` | `2418ab0f…` |
| `20` | v4 | mariana | `v4_ncfw_*` | `ed8eed34…` | `1c3ac5f4…` |
| `28` | v4_plus | mariana_plus | `v4_plus_ncfw_*` | `abc4d452…` | `1c3ac5f4…` (== v4) |

---

## 3. The Carve Method and the IRAM/DRAM Split

### Purpose

Reproducing the inventory requires exactly four operations per blob: locate the symbol, read its size word, slice the bytes, and hash/measure them. Because `.rodata` is VMA == file offset and the format is header-less, the carve is mechanical — there is no container to descend.

### The carve

```c
// reconstruct the inventory from libncfw.so — no container parsing required
function carve_ncfw_blobs(elf):
    rodata = section(elf, ".rodata")            // @0x65000, VMA == file offset (readelf -S)
    for arch in {v2, v3, v4, v4_plus}:          // = coretype {5, 12, 20, 28}
        for half in {iram, dram}:
            sym      = lookup_symbol(elf, f"{arch}_ncfw_{half}_bin")        // nm 'r'
            size_sym = lookup_symbol(elf, f"{arch}_ncfw_{half}_bin_size")
            len      = read_u32(elf.bytes, size_sym.addr)   // LOW u32 ONLY — see v4_plus GOTCHA
            blob     = elf.bytes[sym.addr : sym.addr + len] // addr is the file offset
            record(arch, half, sym.addr, len, sha256(blob), shannon_entropy(blob))
```

The provider does precisely the same slice at runtime, minus the hash: `libncfw_get_image(arch_id, out[4])` writes `out = {&iram_bin, iram_size, &dram_bin, dram_size}` — four words pointing **into `.rodata`**, no copy. The full carrier-side ABI (the `cmpl`-chain switch, the EINVAL/ENOENT returns) is owned by [The Carrier Library](carrier-library.md).

### The IRAM half — code

The IRAM blob is the executable Xtensa LX image. Byte 0 is the on-device **packed XEA2 vector table**, not a header: the silicon begins fetching there the instant reset deasserts, so the carve offset *is* the reset address. The first eight bytes are the reset vector's two `j` slots, and they are the cleanest per-arch fingerprint:

```text
arch   first 8 bytes              reset slot A → target   slot B → target
v2     06 76 00 00 00 00 86 77    j 0x1dc                 j 0x1e8
v3     06 76 00 00 00 00 86 77    j 0x1dc                 j 0x1e8   (== v2 prologue, byte-identical)
v4     06 7d 00 00 00 00 86 7e    j 0x1f8                 j 0x204
v4+    06 7d 00 00 00 00 86 7e    j 0x1f8                 j 0x204   (== v4 prologue, byte-identical)
```

The `+0x1c` jump-target shift (`0x1f8 − 0x1dc`) is exactly the byte length of the D-cache invalidate loop the v4/v4_plus boot path adds; the off18 arithmetic and the full vector/boot/idle decode are owned by [The NCFW Sequencer](ncfw-sequencer.md) and not repeated here.

### The DRAM half — a sparse data template

The DRAM blob is **not code** — it is a sparse data template the sequencer reads from HBM. Its low ~`0x240` (v2) / ~`0x3f4` (v3+) bytes hold the payload; the remainder is zero (a BSS-style template the host patches before upload). Byte 0 is the four-entry exception-dispatch table; a 40-bit SoC physical-address grid follows at `+0x10`:

```text
DRAM blob layout (offsets image-relative; values read LE this pass):
  +0x00  u32[4]  exception-dispatch table   (slot0 == slot3 catchall)
                   v2          = { 0x1bb3, 0x1bcf, 0x1be3, 0x1bb3 }
                   v3/v4/v4+   = { 0x1399, 0x13b1, 0x13c5, 0x1399 }
  +0x10  u64[]   SoC 40-bit physical-address grid (DMA-descriptor targets)
                   v2  = { 0x0fffc2700000, 0x0fffc6700000, 0x0ffff0600000, 0x0ffff0d00000, … }
                   v3+ = { 0x002802700000, 0x003802700000, 0x006802700000, 0x007802700000,
                           0x802802700000, 0x803802700000 }
  …      (payload through ~+0x240 v2 / ~+0x3f4 v3+ — last nonzero byte)
  …      zero-fill to end (the BSS template the host patches pre-upload)
```

> **NOTE —** the v2 DRAM uses a legacy `0x0fffc2_700000`-class address layout; v3+ switch to the structured `0x__802700000` grid. The dispatch-table semantics (each entry is an IRAM handler-stub address; slot0==slot3 is a catchall alias) and the SoC-region decode are owned by [The NCFW Sequencer](ncfw-sequencer.md) §5; this page records only that the DRAM payload is *this table*, not arbitrary data. CONFIDENCE: **HIGH** (table bytes and SoC grid read directly; the v2-vs-v3+ split reproduces this pass).

---

## 4. The Per-Arch Delta Summary

### Purpose

The eight blobs are not eight independent images — they share large byte-identical regions, and knowing which regions overlap tells a reimplementer where the per-generation work actually lives (and where a byte-diff is wasted effort). This is the *carve-level* delta map; the instruction-level deltas (the `+0x1c` D-cache loop, the idle-loop encoding change) are owned by the disassembly page.

### What shares what

```text
DRAM dedup:
  v2_dram   ── unique (legacy SoC layout, 14016 B, sha ca019511…)
  v3_dram   ── unique (19968 B, sha 2418ab0f…)
  v4_dram   ═══ BYTE-IDENTICAL ═══ v4_plus_dram   (19968 B, sha 1c3ac5f4…)
              (v3_dram != v4_dram — the two cayman-class DRAMs differ)

IRAM reset-prologue sharing (first 0x40 bytes):
  v2_iram   ═══ identical prologue ═══ v3_iram     (06 76 00 …; reset → 0x1dc / 0x1e8)
  v4_iram   ═══ identical prologue ═══ v4_plus_iram(06 7d 00 …; reset → 0x1f8 / 0x204)
              (v2 prologue != v4 prologue — the +0x1c shift camp split)

v4 vs v4_plus IRAM whole-image diff:
  0x0000 .. 0x1190   IDENTICAL  (reset + boot + vector table + early handlers)
  0x1190 .. 0x4ab0   DIFFER     (the CC-op handler bodies)
  0x4ab0 .. end      IDENTICAL  (slave loop + idle dispatcher + dcache-wb)
  first differing byte 0x1190 · last differing byte 0x4aac
```

### The delta table

| Dimension | v2 (sunda) | v3 (cayman) | v4 (mariana) | v4_plus (mariana_plus) |
|---|---|---|---|---|
| coretype key | `5` | `12` | `20` | `28` |
| IRAM size (B) | 43,232 | 19,392 | 19,488 | 19,488 |
| IRAM `sha256[0:16]` | `e379980b…` | `d7bc8b81…` | `ed8eed34…` | `abc4d452…` |
| IRAM entropy (b/B) | 6.81 | 6.42 | 6.44 | 6.40 |
| DRAM size (B) | 14,016 | 19,968 | 19,968 | 19,968 |
| DRAM `sha256[0:16]` | `ca019511…` | `2418ab0f…` | `1c3ac5f4…` | `1c3ac5f4…` (== v4) |
| Reset prologue shares with | v3 | v2 | v4_plus | v4 |
| DRAM shares with | — | — | v4_plus | v4 |
| Host idioms (simcall/waiti15/break1,15/break1,1/extw) | 1/1/1/1/1 | 1/1/1/1/1 | 1/1/1/1/1 | 1/1/1/1/1 |

> **QUIRK —** the host-interface idiom set is byte-uniform across all four IRAMs: each of `simcall` (`00 51 00`), `waiti 15` (`00 7f 00`), `break 1,15` (`f0 41 00`), `break 1,1` (`10 41 00`), and `extw` (`d0 20 00`) appears **exactly once per image** (count `1/1/1/1` for every idiom, byte-scanned this pass). A reimplementer can treat the host interface as a single fixed idle-loop construct shared verbatim by every generation; only the handler bodies and the boot-path cache loops vary. CONFIDENCE: **HIGH** (counts re-scanned across all four IRAM blobs). The semantics of these idioms are owned by [The NCFW Sequencer](ncfw-sequencer.md) §5.

> **NOTE —** because `v4_dram == v4_plus_dram` and the v4/v4_plus IRAMs share `0x0..0x1190` and `0x4ab0..end`, **`mariana_plus` is a CC-op handler-logic revision on the `mariana` framework**, not a new image — the entire boot/idle/exception scaffold and both DRAM templates are reused verbatim. The divergence is confined to the IRAM handler window `0x1190..0x4ab0`. Characterizing that diff instruction-by-instruction is blocked on the opaque sequencer TIE (owned by [The NCFW Sequencer](ncfw-sequencer.md) §7). CONFIDENCE: **HIGH** (the identical/diff ranges and the DRAM identity are `cmp`-proven this pass).

---

## 5. The RAW Format Invariant

> **QUIRK — header-less RAW, keyed only by an external integer.** These eight blobs are about as raw as an embedded firmware image gets. There is **no ELF wrapper** (unlike the GPSIMD/Q7 microcode in `libnrtucode_extisa.so`, which ships 13 real Xtensa ELF32 `EXEC` objects — see [The 13 Q7 Microcode Blobs](../gpsimd/q7-blobs.md)); **no magic number**; **no length prefix inside the payload** (the length is the external `*_bin_size` word); **no checksum or signature**; and **no version field in the bytes** (the version is the external coretype key and the carrier's `libncfw_get_version` == `2` gate). IRAM byte 0 is immediately the on-device packed XEA2 vector table, and DRAM byte 0 is immediately the exception-dispatch table — the silicon and the sequencer consume the carved bytes verbatim, at their natural base, with no preamble to skip. A reimplementer must therefore carry the metadata *out of band* (symbol table + size word + coretype), and must keep each `{IRAM, DRAM}` pair position-correct: there is nothing inside a blob to tell a mis-routed upload that it loaded the DRAM template into IRAM (or v3's image onto v4 silicon). The entropy split (IRAM ≈ 6.5 vs DRAM ≈ 0.14) is the only built-in sanity check, and it is a measurement the loader must perform itself, not a field the format provides. CONFIDENCE: **HIGH** (the absence of any header/magic/checksum is confirmed by the byte-0 disassembly reproducing as live code/table, and by the provider returning raw `.rodata` pointers with no transform).

The keying restates the same invariant from the host side: the **integer coretype `5/12/20/28`** is the *only* thing that selects a blob. There is no name lookup, no capability negotiation, no probe of the bytes — `get_image` is a flat four-way `cmpl`-chain switch on that integer, returning `EINVAL`/`ENOENT` for anything else. The integer comes from libnrt's `nrtucode_coretype` table; this firmware store assumes the caller already knows its silicon generation, because the blobs themselves carry no way to ask.

---

## Related Components

| Name | Relationship |
|---|---|
| `libncfw_get_image()` @`0x1179` (host) | The provider that slices these eight blobs out of `.rodata` as `{ptr, len}` quads — no copy, no container |
| `libncfw_get_version()` @`0x12fa` (host) | The out-of-band version field the RAW format lacks — returns `2`, asserted by libnrt |
| `*_ncfw_dram_bin` template | The DRAM half — exception-dispatch table + SoC-address grid + zero BSS; patched host-side before HBM upload |
| GPSIMD/Q7 microcode (`libnrtucode_extisa.so`) | The *sibling* firmware carrier — 13 **ELF-wrapped** Xtensa blobs, the opposite of this RAW format |

## Cross-References

- [The NCFW Sequencer (Xtensa LX Disassembly)](ncfw-sequencer.md) — owns the on-device decode of these IRAM/DRAM bytes (reset vector, boot, idle dispatcher, exception table, opaque TIE); this page is the carve/inventory, that page is the disassembly — blob count, addresses, and digests are kept consistent across both
- [Collectives Firmware — Section Map](overview.md) — where the blob store sits in Part X and the three-Xtensa-core distinction
- [The Carrier Library (libncfw.so)](carrier-library.md) — the host provider ABI: the `get_image` switch, the `5/12/20/28` keying, the version-2 gate, the zero-device-I/O proof
- [Firmware Upload Path (DKMS → Device DRAM)](upload-path.md) — how the carved bytes reach device IRAM/HBM (DMA-to-IRAM, DRAM-to-HBM template patch, reset release)
- [Coretype Numbering Reconciliation](../arch/coretype-numbering.md) — the canonical `5/12/20/28` ↔ `v2/v3/v4/v4_plus` ↔ silicon mapping that keys this store
- [The 13 Q7 Microcode Blobs](../gpsimd/q7-blobs.md) — the sibling carrier's ELF-wrapped microcode, the contrast that makes this RAW format notable
- [back to index](../index.md)
