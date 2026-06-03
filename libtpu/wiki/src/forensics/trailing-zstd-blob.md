# Trailing zstd Blob

> *All offsets, addresses, and section names on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (package version `0.0.40`, build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes). Pin to the build-id; it is the only unambiguous identifier. Other builds will differ.*

## Abstract

An earlier binwalk-style carve reported a ~4.1 MB **zstd-dictionary-compressed blob appended to `libtpu.so` past the ELF section headers**, at file offset `0x20F99BEF`, supposedly decoded through a recovered dictionary into a per-codename hardware-constants bundle. That finding is **wrong on every material claim**, and the correction is the whole point of this page. There is no trailing payload, no embedded dictionary, and no `ZSTD_DCtx_loadDictionary` call site to recover one from. The byte sequence the carve anchored on is an **x86-64 instruction immediate** sitting deep inside `.text`.

The mechanics are simple once the binary is read directly. The four bytes `28 b5 2f fd` are the little-endian encoding of `ZSTD_MAGICNUMBER` (`0xFD2FB528`, RFC 8878 §3.1.1.1). `libtpu.so` statically links the **entire** libzstd source — compressor and decompressor, 309 `ZSTD_*` symbols, full symbols preserved as local (`t`) entries — so that constant appears five times, every time as a `mov`/`cmp` immediate in a libzstd function that either *writes* a frame header into a caller-supplied output buffer or *checks* it against an input buffer. None of the five sit in a data section; none are referenced by a pointer or relocation; all five are reachable only by program execution flowing through libzstd code.

This page does three things: it establishes that the ELF section-header table ends *exactly* at EOF (no trailing-past-sections region exists), it walks the `ZSTD_compressEnd_public` empty-frame epilogue that produces the false-positive `mov`, and it documents the *actual* runtime zstd surface — riegeli-driven stream (de)compression whose dictionaries, when used at all, are supplied by the application at runtime through `ZSTD_DCtx_refDDict`, not baked into the image. The hardware-constants bundle the carve imagined is real but lives elsewhere; see [chip-parts binarypb](../targets/chip-parts-binarypb.md).

For a reader re-verifying this, the contract is:

- **The ELF layout math** — `e_shoff + e_shnum × e_shentsize = file size`, so nothing is appended.
- **The five magic occurrences** — each one's file offset, containing libzstd function, and instruction role.
- **The empty-frame epilogue** — the exact byte sequence in `ZSTD_compressEnd_public` that emits `28 b5 2f fd` into a heap buffer.
- **The absent symbol** — why `ZSTD_DCtx_loadDictionary` not existing falsifies the dictionary-recovery plan, and what the present `ZSTD_DCtx_refDDict` does instead.

| | |
|---|---|
| **Claimed blob** | ~4.1 MB zstd-dict-compressed payload at `0x20F99BEF` — **does not exist** |
| **What is at `0x20F99BEF`** | `movl $0xfd2fb528,(%r14)` immediate (file off `0x20F99BEF`, `.text`) |
| **Container** | section `[21] .text` (PROGBITS, R+X) — *not* past the section table |
| **Section table** | `e_shoff` `0x2E979BA8` + 52 × 64 = `0x2E97A8A8` = EOF (no trailing region) |
| **Magic occurrences** | 5 total, all in `.text`, all `mov`/`cmp` immediates |
| **Dictionary mechanism** | none static; `ZSTD_DCtx_loadDictionary` **absent**; runtime `ZSTD_DCtx_refDDict` only |
| **Decompressed format** | n/a — no blob to decompress |
| **Confidence** | **HIGH** (negative result, byte- and disassembly-anchored) |

> **CORRECTION (ZSTD-01) —** the prior "trailing zstd blob at `0x20F99BEF`, ~4.1 MB, zstd-dictionary-encoded, decoding to per-codename hardware constants" claim is withdrawn in full. The offset is inside `.text`, not past EOF; the bytes are a `mov` immediate, not a frame; no dictionary is embedded; no payload exists. Every downstream task gated on "decode the blob" is closed as *no blob exists*. The page below is the resolution the [forensics overview](overview.md) routed here.

---

## The "Trailing Past EOF" Premise Is False

### Where the ELF actually ends

The originating hypothesis was that the blob "sits after the last ELF section header," implying an appended payload outside any section. The ELF header math falsifies this directly.

```text
e_shoff (Start of section headers) = 781,687,720 = 0x2E979BA8
e_shentsize                        = 64          = 0x40
e_shnum                            = 52          = 0x34
section-header table end = 0x2E979BA8 + 52 × 0x40
                         = 0x2E979BA8 + 0xD00
                         = 0x2E97A8A8
file size                          = 781,691,048 = 0x2E97A8A8   ← identical
```

The section-header table runs to the **exact** last byte of the file. There is no region after it. The last data-bearing section is `[51] .strtab`, ending at `0x23DF76C3 + 0xAB824DE = 0x2E979BA1`; a 7-byte alignment gap precedes the table at `0x2E979BA8`. Every byte of the 745 MB image is accounted for by a section or by the section-header table itself.

> **GOTCHA —** the section-header table ending at EOF is the cleanest possible disproof of an "appended payload" story, and it is one `readelf -h` away. Any future "trailing blob past the sections" claim against this binary should be checked against `e_shoff + e_shnum × e_shentsize` *first*; in this build that sum is the file size to the byte.

### The anchor offset is ~2.6 MB before the end of `.text`

The carve anchor `0x20F99BEF` (file offset 553,229,295) is **not** at or past EOF. It lands inside section `[21] .text`:

| Region | File range | Confidence |
|---|---|---|
| `[21] .text` (PROGBITS, R+X) | `0xE63C000 .. 0x21217484` | CERTAIN |
| `[11] .rodata` (PROGBITS, R) | `0x84A0000 .. 0xBE8AF28` | CERTAIN |
| `[51] .strtab` (last data section) | `0x23DF76C3 .. 0x2E979BA1` | CERTAIN |
| Section-header table | `0x2E979BA8 .. 0x2E97A8A8` (= EOF) | CERTAIN |
| Anchor `0x20F99BEF` | inside `.text`, ~2.6 MB before its end | CERTAIN |

`.text` ends at `0xE63C000 + 0x12BDB484 = 0x21217484`. The anchor sits `0x21217484 − 0x20F99BEF = 0x27D895 ≈ 2.6 MB` before the end of the code section — squarely in executable code, not in any data region and not appended anywhere.

> **NOTE —** because `.text` maps with file offset equal to virtual address in this image (the segment is laid out 1:1 in the `0xE63C000..0x21217484` range), every offset below is simultaneously a file offset and a runtime vaddr. That is why `objdump --start-address=0x20F99BEC` disassembles the on-disk bytes correctly without an offset/vaddr conversion.

### The grep-returns-zero artifact

A plain `grep -c -a -P '\x28\xb5\x2f\xfd'` over the file returns **0** hits — the result the forensics overview flagged. This is a tooling artifact, not evidence of absence: GNU `grep` line-buffers and mishandles a 745 MB binary studded with NUL bytes and overlong "lines." A chunked exact-byte scan (read 16 MB windows, carry a 3-byte overlap, `bytes.find`) returns the true count:

```text
total occurrences of 28 b5 2f fd: 5
  0x20F99BEF  (553,229,295)
  0x20F9B2FE  (553,235,198)
  0x2100C714  (553,699,092)
  0x2100C72A  (553,699,114)
  0x2100C77D  (553,699,197)
```

> **GOTCHA —** "magic byte scan returned zero hits" can mean *the scanner is wrong*, not *the bytes are absent*. The honest answer for this binary is **five** occurrences, all inside `.text`. Use a binary-safe scanner (chunked `bytes.find`, `xxd | rg`, or a hex-aware tool) before concluding a magic is missing.

---

## The Five Magic Occurrences

### What each one is

Each occurrence is an immediate operand of a libzstd instruction. None is data. The table is the complete inventory.

| File offset | Containing function | Function base | Instruction | Role | Confidence |
|---|---|---|---|---|---|
| `0x20F99BEF` | `ZSTD_compressEnd_public` | `0x20F99AC0` | `movl $0xfd2fb528,(%r14)` (at `0x20F99BEC`, +0x12C) | Emit magic into output buffer (empty-frame epilogue) | CERTAIN |
| `0x20F9B2FE` | `ZSTD_writeFrameHeader` | `0x20F9B200` | `movl $0xfd2fb528,(%rdi)` (at `0x20F9B2FC`, +0xFC) | Emit magic at the start of every written frame | CERTAIN |
| `0x2100C714` | `ZSTD_getFrameHeader_advanced` | `0x2100C6C0` | `movl $0xfd2fb528,-0x1c(%rbp)` (at `0x2100C711`, +0x51) | Sentinel write to a stack-local before the header `memcpy` | CERTAIN |
| `0x2100C72A` | `ZSTD_getFrameHeader_advanced` | `0x2100C6C0` | `cmpl $0xfd2fb528,-0x1c(%rbp)` (at `0x2100C727`, +0x67) | Compare the copied stack-local against the magic | CERTAIN |
| `0x2100C77D` | `ZSTD_getFrameHeader_advanced` | `0x2100C6C0` | `cmp $0xfd2fb528,%ecx` (at `0x2100C77B`, +0xBB) | Verify input frame magic; `jne` to error on mismatch | CERTAIN |

The third and fourth rows are 22 bytes apart (`0x2100C711`/`0x2100C727`) within the same basic block of `ZSTD_getFrameHeader_advanced`: the first writes the magic into a stack-local as a sentinel, then a `memcpy` copies the candidate header over it, and the second compares the result back against the magic. They participate in the same header-validation logic and are not independent finds.

> **QUIRK —** the constant appears on both sides of the codec. `ZSTD_writeFrameHeader` and `ZSTD_compressEnd_public` *emit* it; `ZSTD_getFrameHeader_advanced` *checks* it. A naive "search for the magic to find the payload" treats an encoder writing a header and a decoder validating one as if both were stored frames. Neither is.

### The decoder-side check at `0x2100C77B`

The clearest single proof that these are code, not data, is the magic-verification `cmp` in the decoder. The disassembly reads the first four bytes of an input buffer and compares them against the constant:

```asm
2100c779:  8b 0e                 mov   (%rsi),%ecx          ; load 4 bytes of input frame
2100c77b:  81 f9 28 b5 2f fd     cmp   $0xfd2fb528,%ecx     ; compare to ZSTD_MAGICNUMBER
                                                            ; jne -> "not a zstd frame" error path
```

`%rsi` is the caller-supplied source pointer. The constant is the *expected* magic the decoder demands of *external* input — exactly the opposite of a stored frame. The 4-byte `cmp` immediate `28 b5 2f fd` begins at file offset `0x2100C77D` (opcode `81` at `0x2100C77B`, ModR/M `f9` at `0x2100C77C`, then the immediate); the carve anchored on that immediate.

---

## The Empty-Frame Epilogue — `ZSTD_compressEnd_public`

### Why this function writes the magic

`ZSTD_compressEnd_public` (`0x20F99AC0`, size `0x27F` = 639 bytes; next symbol `ZSTD_createCDict_advanced` at `0x20F99D40`) is upstream zstd's stream finalizer. It flushes the last block, and in the degenerate "compress 0 bytes / stream never started" case it synthesizes a complete **empty zstd frame** directly into the caller's output buffer. That synthesis is where the `movl $0xfd2fb528,(%r14)` lives. `%r14` holds the `dst` pointer — a heap buffer the caller owns, never a region of the ELF image.

### Algorithm

```c
function ZSTD_compressEnd_public(cctx, dst, dstCap, src, srcSize):   // sub @ 0x20F99AC0
    // ... normal flush path elided ...
    if cctx.stage == init_only:               // degenerate "empty frame" case
        out = (byte*)dst;                     // %r14 = dst (heap, caller-owned)

        // --- build the 6-byte frame header from cctx.cParams ---
        WD  = window_descriptor(cParams.windowLog);   // movzbl 0xf4(%rbx); shl/add
        chk = (cParams.checksumFlag != 0);            // setne %dl  -> Content_Checksum_flag
        did = (cParams.dictID > 0);                   // setg %dil  -> Dictionary_ID present
        FHD = assemble_frame_header_descriptor(chk, did);  // shl/or into FHD bit positions

        // --- emit the empty frame, 10 bytes total ---
        *(uint32_t*)out = 0xFD2FB528;         // 0x20F99BEC: movl  -> magic 28 b5 2f fd
        r8 = 4;                               // 0x20F99BF3: cursor past the 4-byte magic
        out[r8 + 0] = FHD;                    // 0x20F99C01: Frame_Header_Descriptor
        out[r8 + 1] = WD;                     // 0x20F99C05: Window_Descriptor
        *(uint16_t*)&out[r8 + 2] = 0x0001;    // 0x20F99C10: block header, last=1 type=Raw
        out[r8 + 4] = 0x00;                   // 0x20F99C18: 1-byte raw block content
        return 10;                            // magic(4)+FHD(1)+WD(1)+blkhdr(3)+content(1)
```

### Raw bytes vs. their meaning

The bytes following `0x20F99BEF`, read as a hex dump, look like they could be frame data — which is exactly how the carve was fooled:

```text
20f99bef: 28 b5 2f fd  41 b8 04 00 00 00  85 f6  0f b6 c9  0f
20f99bff: 44 f9  43 88 14 06  43 88 7c 06 01  c7 03 02 00 00 00
```

Disassembled as x86-64 they are a coherent instruction stream — `movl $magic,(%r14)`; `mov $0x4,%r8d`; `test %esi,%esi`; `movzbl %cl,%ecx`; `cmove %ecx,%edi`; `mov %dl,(%r14,%r8,1)`; … flowing to the function's `vzeroupper`/`call ZSTD_trace_compress_end` exit. Disassembled (mis-)as a zstd frame they yield immediate "frame parameters too large" errors: the byte after the bogus header decodes to a window log of 33 (`2^33` = 8 GiB, over `ZSTD_WINDOWLOG_MAX = 31`) and a first-block size far past the 128 KiB block cap. zstd refuses such a frame; the carve's "4.1 MB" length came from the signature handler chasing nonsense block-size fields through executable code until it tripped, ending mid-MLIR-pass — still inside `.text`.

> **QUIRK —** a 4-byte magic detector with no cross-validation will false-positive on *any* code path that constructs a zstd frame via a 32-bit `mov` immediate. The reliable tell is the containing symbol: a hit inside a function named `ZSTD_writeFrameHeader`, `ZSTD_compressEnd_*`, or `ZSTD_*FrameHeader*` is a code immediate, not a stored frame. Resolve the anchor against the symbol map before carving.

---

## The Dictionary Mechanism — There Isn't a Static One

### The absent symbol falsifies the recovery plan

The dictionary-recovery hypothesis required finding `ZSTD_DCtx_loadDictionary` call sites, back-tracing each to a fixed dictionary offset in `.rodata`/`.data`, and matching a dictionary-ID in the frame header. The premise dies at step one: the public **decompression-side** dictionary-loader does not exist in this binary.

| Symbol the recovery plan assumed | Present in `libtpu.so`? | Address | Confidence |
|---|---|---|---|
| `ZSTD_DCtx_loadDictionary` | **NO** | — | CERTAIN |
| `ZSTD_DCtx_loadDictionary_byReference` | **NO** | — | CERTAIN |
| `ZSTD_DDict_createByReference` | **NO** | — | CERTAIN |
| `ZSTD_loadDictionaryContent` | YES (compressor-internal) | `0x20FA0020` | CERTAIN |
| `ZSTD_dedicatedDictSearch_lazy_loadDictionary` | YES (compressor match-finder) | `0x20FBB120` | CERTAIN |
| `ZSTD_DDict_dictContent` | YES | `0x2100C100` | CERTAIN |
| `ZSTD_createDDict_advanced` | YES | `0x2100C200` | CERTAIN |
| `ZSTD_DCtx_refDDict` | YES | `0x2100E300` | CERTAIN |
| `ZSTD_decompressDCtx` | YES | `0x2100D3E0` | CERTAIN |
| `ZSTD_decompressStream` | YES | `0x2100E840` | CERTAIN |

The two "loadDictionary"-named symbols that *are* present are **compressor** internals (`ZSTD_loadDictionaryContent` ingests a dictionary into a `ZSTD_CCtx`; `ZSTD_dedicatedDictSearch_lazy_loadDictionary` is a lazy match-finder helper). Neither loads a dictionary into a decompression context, and neither is wired to a static buffer in this image. With no `ZSTD_DCtx_loadDictionary`, there is no "load a fixed embedded dictionary into a DCtx and decompress the blob" path to reverse-engineer.

### What the runtime dictionary path actually is

The present `ZSTD_DCtx_refDDict` and `ZSTD_createDDict_advanced` are the API for **referencing a digested dictionary supplied at runtime**. The strings sidecar shows where they are driven from — riegeli's zstd reader/writer and an RPC/program compression layer, not a static blob:

```text
REQUEST_COMPRESSION_ZSTD          ZstdReaderBase           ZSTD_DCtx_refDDict
RESPONSE_COMPRESSION_ZSTD         ZstdReaderINS_           ZSTD_decompressStream
TPU_CORE_PROGRAM_COMPRESSION_ZSTD ZStdCompressor           ZSTD_CCtx_refCDict
zstd_compression_level            zstd_reader / zstd_writer
```

These name a streaming compression facility for RPC payloads and TPU-core programs. Any dictionary such a stream uses is handed in by the application at runtime (a JAX/TF artifact, an RPC negotiation), attached via `ZSTD_DCtx_refDDict`, and never read out of a fixed region of `libtpu.so`.

### The only embedded zstd-parameter data

The single zstd-parameter region baked into the image is **8 bytes**, not a dictionary:

| Symbol | Vaddr / file off | Section | Bytes | Decoded | Confidence |
|---|---|---|---|---|---|
| `tpu::(anonymous)::kZstdParams` | `0x0B831ACC` | `[11] .rodata` | `01 00 00 00 1B 00 00 00` | `{ level = 1, window_log = 27 }` | HIGH |

This is the default `tpu::ZStdParams` struct used when a TPU zstd compressor is constructed with no override (level 1, 128 MiB window). It is configuration, not content — and certainly not a 4 MB dictionary.

---

## Runtime Decompress Path — What Exists Instead of a Blob

### There is nothing to locate or decompress

Because no static blob and no static dictionary exist, there is no "runtime locates + decompresses the trailing blob" sequence to document — the question the page title poses has the answer *the operation does not occur*. The runtime zstd code that *does* exist is the generic statically-linked libzstd, driven by riegeli for streaming compression of data that arrives from outside the `.so`:

```text
application / RPC layer
  └─ supplies a compressed stream (+ optional dictionary buffer) at runtime
       └─ riegeli ZstdReaderBase
            └─ ZSTD_createDCtx        (0x2100C480)
            └─ ZSTD_DCtx_refDDict     (0x2100E300)   ── attach app-supplied digested dict
            └─ ZSTD_decompressStream  (0x2100E840)   ── stream-decompress into output
                 └─ ZSTD_getFrameHeader_advanced (0x2100C6C0)  ── validates magic (the cmp above)
```

The frame-magic `cmp` at `0x2100C77B` is on this decode path: it validates frames the application feeds in. The blob hunt mistook that validation constant — and the encoder's emission constants — for stored data.

### Where the imagined payload actually lives

The hardware-constants bundle the carve speculated the blob would decompress into is a real artifact in this binary, but it is an **uncompressed protobuf** parsed from data sections, documented separately:

- Per-codename hardware constants and the `chip_parts` binarypb bundle are covered under [`targets/chip-parts-binarypb.md`](../targets/chip-parts-binarypb.md) and [`targets/per-codename-hw-constants.md`](../targets/per-codename-hw-constants.md). They are not zstd-compressed and not gated on any blob recovery.

> **NOTE —** the generalizable lesson for the rest of the corpus: any binary that statically links libzstd (several do across nvopen-tools) will produce the same false-positive carve wherever a `ZSTD_writeFrameHeader` / `ZSTD_compressEnd_*` / `ZSTD_*FrameHeader*` function builds a frame via a `mov` immediate. Treat a zstd magic hit whose anchor resolves inside `.text` as a code immediate until a stored frame is proven by a containing *data* section and a valid (window-log ≤ 31, block-size ≤ 128 KiB) header.

---

## Related Components

| Name | Relationship |
|---|---|
| `ZSTD_compressEnd_public` (`0x20F99AC0`) | Source of the `0x20F99BEF` false-positive magic (empty-frame epilogue) |
| `ZSTD_writeFrameHeader` (`0x20F9B200`) | Source of the `0x20F9B2FE` magic (every emitted frame header) |
| `ZSTD_getFrameHeader_advanced` (`0x2100C6C0`) | Source of the `0x2100C714/72A/77D` magic (input-frame validation) |
| riegeli `ZstdReaderBase` / `ZStdCompressor` | The real runtime zstd surface — streaming, app-supplied dictionaries |
| `tpu::(anonymous)::kZstdParams` (`0x0B831ACC`) | The only embedded zstd config: `{level=1, window_log=27}`, 8 bytes |

## Cross-References

- [Forensics Overview](overview.md) — routed the magic-byte puzzle here; its GOTCHA on the section-table-ends-at-EOF is resolved on this page.
- [ELF Anatomy](elf-anatomy.md) — the section table whose end coincides with EOF; full `[0]..[51]` layout that leaves no trailing region.
- [Embedded Library Atlas](embedded-library-atlas.md) — the statically-linked libzstd (309 `ZSTD_*` symbols) whose code immediates produce the five magic hits.
- [Custom Sections](custom-sections.md) — the non-standard data sections (`filewrapper_toc`, `.lrodata`, etc.) that *do* carry payloads, none of them a zstd blob.
- [chip-parts binarypb](../targets/chip-parts-binarypb.md) — the real, uncompressed hardware-constants bundle the carve imagined the blob would decode into.
- [Per-Codename HW Constants](../targets/per-codename-hw-constants.md) — per-codename silicon constants, parsed from data sections, not from any compressed blob.
