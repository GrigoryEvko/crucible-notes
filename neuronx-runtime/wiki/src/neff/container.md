# NEFF Container

> *All addresses, offsets, sizes, and version strings on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. `.text`/`.rodata` VMA equals file offset, so every `0x…` is an analysis VMA. The three vendored decoders are statically linked: **libarchive 3.8.0dev**, **zlib 1.3.1**, **simdjson 0.9.0** (binary strings `"libarchive 3.8.0dev"`, `" inflate 1.3.1 Copyright 1995-2024 Mark Adler "`, `"SIMDJSON_VERSION 0.9.0"`). Source TU: `/opt/workspace/KaenaRuntime/kelf/neff.cpp`. Other versions will differ.*

## Abstract

A **NEFF** ("Neuron Executable File Format") is the on-disk model package the runtime consumes: a **flat 1024-byte binary header** followed by an inner archive payload. Read it the way you read a self-extracting tarball with a fixed-size preamble — the first 1024 bytes are a C `struct` whose fields the loader validates by hand, and everything after byte `0x400` is a single **gzip-compressed POSIX-pax/ustar TAR stream** that the runtime unpacks *entirely in memory*. There is no temp file, no disk staging, and no `IOCTL`: the whole container parse runs in userspace inside `neff_parse` (`@0x4ca3f0`), driving a statically-linked **libarchive 3.8.0dev** reader over an in-RAM buffer, with **zlib 1.3.1** doing the inflate and **simdjson 0.9.0** parsing the JSON manifests inside the tar.

The single fact a reimplementer must internalize is that the NEFF format has **no self-describing structure beyond the 1024-byte header**. There is *no magic number*, *no section table*, and *no directory*. The logical contents — the graph manifest, the per-subgraph KELF definition, the per-engine binary blobs — are expressed purely as **TAR member pathnames** plus a chain of JSON manifests that name each other by string. The header's only discriminator is `pkg_version` (a `uint64` at offset 0): value `1` = raw-tar + SHA-256 integrity, value `2` = gzip-tar + MD5 integrity (the production packager). To unpack a NEFF you read the header, optionally verify the hash, hand the `[0x400 .. 0x400+data_size)` window to libarchive as a memory blob, and walk the tar; the names of the members tell you what each one is.

This page documents three artifacts: (1) the **1024-byte `neff_header_t`** field layout and its hand-rolled validation gate; (2) the **in-memory unpack pipeline** — header-validate → `archive_read_open_memory` over the gzip/tar stream → zlib inflate → per-entry extract into a `malloc`'d buffer → `std::map` insert keyed by pathname; and (3) the **simdjson manifest-routing chain** that turns the unpacked member table into the load graph. The downstream KELF→KBIN lowering, the metadata schema, and the section semantics are owned by sibling pages (see [Cross-References](#cross-references)).

For reimplementation, the contract is:

- **The 1024-byte header** — every field's offset/width, the `pkg_version` discriminator, the `data` payload starting at `+0x400`, and the four validation rejects (`size`, `header_size`, `data_size`, version ceiling, feature mask).
- **The integrity model** — SHA-256 over `data[data_size]` for `pkg_version==1`, MD5 (first 16 bytes of `hash`) for `pkg_version==2`; verification is **conditional on a flag** and asserts `len > 0`.
- **The in-memory tar walk** — `archive_read_new` + `support_format_tar` + `support_filter_gzip` + `open_memory(data, data_size)`; loop `archive_read_next_header`; skip `AE_IFDIR`; strip a leading `"./"`; drop any member whose pathname ends in the 18-byte literal `"wavegraph-bin.json"`; `malloc(entry_size)` + `archive_read_data`; insert `{pathname → (buf,size)}` into a red-black tree.
- **The gzip filter glue** — `inflateInit2_(z, -15, "1.3.1", 112)` (raw deflate, 64 KB out-buffer, `crc32` trailer), driven by the gzip bidder that recognizes magic `1f 8b`.
- **The manifest chain** — `neff.json` (or legacy `kelp.json`) → `parse_neff_json` (find the `attrs.func_name=="__kelf"` node) → `dlr_kelf_load` → the named `kelf-a.json` member, all parsed by simdjson DOM across three SIMD tiers (`fallback` / `westmere` / `haswell`).

| | |
|---|---|
| **Header struct** | `neff_header_t` — **1024 bytes** (`structures.json` ord 5896); the buffer head |
| **Payload start** | `data` @ `+0x400`; inner archive = gzip-pax-tar (pkg2) of `data_size` bytes |
| **Discriminator** | `pkg_version` @ `+0` — `1`=raw-tar/SHA-256, `2`=gzip-tar/MD5 (no magic byte exists) |
| **Header validator** | `neff_get_header_from_buffer` `@0x4ca2c0` (non-null, `size>0x3FF`, `header_size<size`) |
| **Container parser** | `neff_parse` `@0x4ca3f0` → `neff_t::files` (member table); cold path `@0x6445f` |
| **Member table** | `neff_t` — **48 bytes** (ord 5894); `std::map<std::string,std::pair<void*,long>>` |
| **Member lookup** | `neff_get_file_content` `@0x4cb670` — RB-tree find by pathname |
| **Archive reader** | libarchive 3.8.0dev — `archive_read_open_memory` `@0x4d1810`, `archive_read_next_header` `@0x4e3ba0`, `archive_read_data` `@0x4cecc0` |
| **Decompressor** | zlib 1.3.1 — `inflate` `@0x502260`, `inflateInit2_` `@0x5020d0`, `crc32` `@0x501d10` |
| **JSON parser** | simdjson 0.9.0 DOM — tiers `haswell` / `westmere` / `fallback` (28/28/27 symbols) |
| **Manifest router** | `kmgr_load_nn_nc` `@0xde280` → `parse_neff_json` `@0xe1d10` → `dlr_kelf_load` `@0xe0830` |

---

## 1. The 1024-Byte Header

### Purpose

The header is a flat, fixed-size C `struct` that **is the head of the NEFF buffer** — the loader casts the buffer pointer directly to `const neff_header_t*` with no copy. It carries the packager discriminator, the integrity hash, the inner-archive size, a 256-byte model name, the TNC cache UUID, and a forward-compat feature trap. Every field is read by `neff_parse`; a reimplementer must reproduce the exact offsets because nothing in the format is self-describing — there is no field-tag, no length-prefix, just absolute byte positions.

### Encoding

Verbatim from `structures.json` ord 5896 (size 1024); offsets shown in both decimal and hex. The inner archive begins at `data` (`+0x400`), so the header occupies exactly the first `0x400` bytes regardless of what `header_size` says.

| Offset | Width | Field | Meaning |
|---|---|---|---|
| `+0x000` | 8 | `pkg_version` | packager discriminator: `1`=raw-tar+SHA-256, `2`=gzip-tar+MD5. **The only "magic" the format has.** |
| `+0x008` | 8 | `header_size` | validated `< neff_size` by `neff_get_header_from_buffer`; **not** forced to `0x400` |
| `+0x010` | 8 | `data_size` | inner-archive byte count; validated `≤ neff_size − 1024` |
| `+0x018` | 8 | `neff_version_major` | accepted range `0.x .. 2.x`; `> 2` is rejected |
| `+0x020` | 8 | `neff_version_minor` | logged only, never gated |
| `+0x028` | 128 | `neff_build_version` | `uint8[128]` compiler build tag; logged only |
| `+0x0A8` | 4 | `unused_0` | padding |
| `+0x0AC` | 32 | `hash` | SHA-256 (32 B) for pkg1; pkg2 compares only the first 16 B (MD5) |
| `+0x0CC` | 16 | `uuid` | 128-bit TNC cache key (single-slot UUID dedup) |
| `+0x0DC` | 256 | `name` | `char[256]` model name → `nrt_model_t.name` via `neff_copy_name` `@0x4cb5d0` |
| `+0x1DC` | 4 | `requested_tpb_count` | requested NeuronCore count |
| `+0x1E0` | 64 | `tpb_per_node` | `uint8[64]` per-node TPB map |
| `+0x220` | 8 | `feature_bits` | forward-compat trap (see below) |
| `+0x228` | 4 | `vnc_size` | LNC (logical-NeuronCore) config compatibility |
| `+0x22C` | 468 | `pad` | reserved padding to `0x400` |
| `+0x400` | `data_size` | `data` | inner archive payload (gzip-pax-tar for pkg2) |

> **QUIRK — there is no magic number.** A reimplementer who reaches for the conventional "read 4 magic bytes, dispatch on them" idiom will find nothing to read: a rodata scan turns up no NEFF signature, and `neff_get_header_from_buffer` does not check one. The format's identity check is **structural, not magical** — `pkg_version` at byte 0 selects the archive flavor and the hash algorithm, and the *inner* magic (`1f 8b` for gzip, `ustar` for tar) is checked by libarchive after `open_memory`, not by the NEFF layer. Treat byte 0 as a `uint64` enum, not a 4-char tag.

### Algorithm — header validation

`neff_get_header_from_buffer` is the preflight gate. It validates only the three structural invariants and returns the buffer pointer as the header; the version, feature, and data-size checks happen in `neff_parse` itself.

```c
// neff_get_header_from_buffer @0x4ca2c0 — returns header* or NULL.
function neff_get_header_from_buffer(neff /* uint8* */, neff_size):
    if neff == NULL:                                   // "Invalid NEFF buffer"
        return NULL
    if neff_size <= 0x3FF:                              // header is 1024 B; reject undersized buffers
        return NULL                                     // "Invalid number of NEFF bytes received %lx"
    header_size = *(uint64*)(neff + 8)                  // neff_header_t.header_size @+0x008
    if header_size >= neff_size:                         // hdr cannot claim to exceed the file
        return NULL                                     // "Invalid NEFF file size hdr sz: %lx, file sz: %lx:"
    return (neff_header_t*)neff                          // cast in place, NO copy

// The remaining gates live in neff_parse @0x4ca3f0, lines 170-198:
function validate_version_and_size(h, neff_size):
    if h.neff_version_major > 2:                          // accepted 0.x .. 2.x
        reject  // "NEFF version: %lu.%lu is not supported, accepted version range: %u.x-%u.x" (0,2)
    if h.neff_version_major == 2 and (h.feature_bits & 0x7FFFFFFFF8000000) != 0:
        reject  // newer-compiler trap; runtime feature set = 0x7FFFFFF
    if neff_size - 1024 < h.data_size:                    // payload must fit after the header
        reject  // "Invalid NEFF data size(%lx vs %lx)"
```

> **NOTE — the forward-compat feature trap.** For `neff_version_major == 2`, `feature_bits` (`@+0x220`) is masked with `0x7FFFFFFFF8000000`; any set bit in that mask means the NEFF was compiled by a *newer* Neuron compiler than this runtime understands, and the load is rejected (status `10`) with the message *"… compiled by a newer version of Neuron compiler. Features supported by this Neuron Runtime: 0x%lx …"*, where the runtime's own feature set is the literal `0x7FFFFFF`. The mask is the bitwise complement of the supported-feature window: bits `0..26` are "known", bits `27..62` are the trap. A reimplementer must carry the same `0x7FFFFFF` ceiling or it will accept NEFFs whose features it cannot honor.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `neff_get_header_from_buffer` | `0x4ca2c0` | preflight: non-null, `size>0x3FF`, `header_size<size` | HIGH |
| `neff_parse` | `0x4ca3f0` | header gate + integrity + tar walk → `neff_t::files` | HIGH |
| `neff_parse.cold` | `0x6445f` | cold/throw path (alloc-fail, `"./"` prefix assert) | HIGH |
| `neff_copy_name` | `0x4cb5d0` | copies the 256-byte `name` field into the model handle | HIGH |

---

## 2. The Integrity Gate

### Purpose

Before any archive bytes are touched, `neff_parse` can verify the payload against the header `hash`. The algorithm is selected by `pkg_version`: SHA-256 for `pkg1`, MD5 for `pkg2`. Verification is **conditional on a runtime flag** (`neff_parse`'s third argument), so a caller can skip it — but when enabled, a mismatch aborts the load before the tar walk begins.

### Algorithm

```c
// neff_parse @0x4ca3f0, lines 199-240. `verify` is the third arg (char a3).
function verify_integrity(h, verify /* bool */):
    if h.pkg_version == 1:                                // raw-tar packager
        data = h.data                                     // = (uint8*)h + 0x400
        if not verify: return OK                          // skip path — no hash computed
        if h.data_size == 0:                              // hard invariant
            assert_fail("len > 0", "kelf/neff.cpp", 0x34, "sha256(...)")
        sha256(h.data, h.data_size, digest)               // sha256_init/update/final
        if memcmp(h.hash, digest, 32) != 0:               // full 32-byte compare
            reject  // "SHA256 mismatch!"
        return OK

    if h.pkg_version != 2:                                 // only 1 and 2 are defined
        reject  // "Unsupported NEFF packager: %lu"  (status 10)

    // pkg_version == 2 — the production packager
    data = h.data
    if verify:
        if h.data_size == 0:
            assert_fail("len > 0", "kelf/neff.cpp", 0x40, "md5(...)")
        MD5(h.data, h.data_size, digest)                  // MD5_Init/Update/Final
        if memcmp(h.hash, digest, 16) != 0:               // only the FIRST 16 bytes of hash[32]
            reject  // "MD5 mismatch!"
    data_size = h.data_size                                // re-read after the (optional) verify
```

> **QUIRK — the `hash` field is 32 bytes but MD5 uses only the first 16.** The header reserves a full 32-byte `hash` (`+0x0AC`) sized for SHA-256. For `pkg_version==2` the packager writes a 16-byte MD5 digest into the *low* half and the runtime compares only `hash[0:16]` (`*(_OWORD*)h->hash - *(_OWORD*)digest`, a single 128-bit compare). The upper 16 bytes are unused for pkg2. A reimplementer that compares all 32 bytes against an MD5 digest will reject every valid pkg2 NEFF. The integrity algorithm is a function of `pkg_version`, not of the field width.

> **NOTE — verification is opt-in, and `data_size==0` is a hard assert.** The hash check runs only when the third argument is set; the unpack proceeds regardless of the flag. But independent of the flag, an empty payload is a fatal invariant: `sha256`/`md5` `__assert_fail("len > 0", …)` at `kelf/neff.cpp:0x34` (SHA) / `:0x40` (MD5). Production NEFFs observed are uniformly `pkg_version==2`; whether `pkg_version==1` ships outside test fixtures is **LOW confidence** (the SHA path is present and correct but not seen in a production corpus this pass).

---

## 3. The In-Memory Unpack Pipeline

### Purpose

This is the heart of the container: turn the gzip-tar payload at `+0x400` into a `{pathname → (buffer, size)}` map, with no disk I/O. The pipeline is four stages — **open** the in-RAM blob as a libarchive object, let the **gzip filter** inflate it via zlib, **iterate** the tar members, and **extract** each member into a `malloc`'d buffer inserted into a red-black tree. Every stage is a libarchive call; the runtime supplies the buffer and consumes the decompressed bytes.

### Entry Point

```text
neff_parse (0x4ca3f0)
  ├─ archive_read_new                   (0x4ce790)   ── alloc archive object
  ├─ archive_read_support_format_tar    (0x4da0c0)   ── register ustar/pax/gnu bidder+reader
  ├─ archive_read_support_filter_gzip   (0x4d1f40)   ── register gzip bidder (magic 1f 8b)
  ├─ archive_read_open_memory           (0x4d1810)   ── wrap data@+0x400 as a client stream
  │    └─ archive_read_open_memory2     (0x4d1740)   ── wire memory_read* callbacks
  │         ├─ memory_read              (0x4d1650)   ── hand out [pos..end] capped at read-size
  │         └─ (gzip filter inserted on top)
  │              └─ gzip_filter_read    (0x4d1970)   ── inflateInit2_(-15,"1.3.1"); inflate loop; crc32
  │                   └─ inflate        (0x502260, zlib 1.3.1)
  └─ loop:
       ├─ archive_read_next_header      (0x4e3ba0)   ── parse one tar member header
       ├─ archive_entry_filetype        (0x4cbb00)   ── AE_IFDIR (0x4000) skip
       ├─ archive_entry_pathname        (0x4cc050)   ── member name
       ├─ archive_entry_size            (0x4cc2e0)   ── member byte count
       ├─ archive_read_data             (0x4cecc0)   ── copy decompressed entry into malloc'd buf
       └─ archive_read_data_skip        (0x4ceec0)   ── discard a skipped member's body
```

### Stage 1 — open the in-RAM archive

`archive_read_open_memory` is libarchive's "treat this byte range as a file" entry. It forwards to `archive_read_open_memory2(a, buf, size, size)`, which installs three client callbacks (`memory_read`, `memory_read_skip`, `memory_read_close`) over a 4-qword client-data block `{base, pos, end, read_cap}`. `memory_read` (`@0x4d1650`) hands the reader the slice `[pos .. end)` capped at the per-read size and advances `pos`; there is no copy and no syscall — the "file" is the NEFF buffer itself. The bidder phase then selects the filter and format: the gzip bidder peeks the first bytes, sees `1f 8b`, and inserts the gzip filter; the tar bidder claims the format slot.

```c
// neff_parse @0x4ca3f0, lines 242-255.
function open_inner_archive(data, data_size):
    a = archive_read_new()                               // 0x4ce790
    archive_read_support_format_tar(a)                   // 0x4da0c0 — ustar/pax/gnu
    archive_read_support_filter_gzip(a)                  // 0x4d1f40 — gzip filter bidder
    rc = archive_read_open_memory(a, data, data_size)    // 0x4d1810 -> open_memory2(a,data,size,size)
    if rc != 0:
        err = archive_error_string(a)                    // "Failed to open NEFF %s"
        archive_read_close(a); archive_read_free(a)
        return FAIL
    return a
```

> **QUIRK — there is no archive-level section table; the members ARE the structure.** A reimplementer expecting an index, a directory record, or a section header at the front of the payload will find none. libarchive presents the tar as a flat sequence of `(pathname, size, body)` members in *write order*. The logical layout — which file is the graph manifest, which are the per-engine blobs — is encoded entirely in the member **pathnames** (`neff.json`, `<dir>/def.json`, `<dir>/<engine>.bin`) and in the JSON manifests that cross-reference each other by name (§4). To find a member you walk the whole tar once into a `std::map` and then look up by string; there is no seek-by-offset.

### Stage 2 — the gzip filter (zlib glue)

The gzip filter is a thin shim over zlib. On the first read it allocates a `z_stream` plus a 64 KB output buffer (`malloc(0x10000)`) and calls `inflateInit2_(z, -15, "1.3.1", 112)`. The window-bits `-15` selects **raw deflate** (libarchive strips the 10-byte gzip member header itself and feeds zlib the raw stream); `"1.3.1"` is the version-guard string zlib checks against its own `ZLIB_VERSION`; `112` is `sizeof(z_stream)`. Each read pumps `inflate(z, 0)` until `Z_STREAM_END`, accumulates the CRC-32, and on completion calls `inflateEnd`.

```c
// gzip_bidder_bid @0x4d1e10 — first-bytes recognition.
function gzip_bidder_bid(filter):
    head = peek 2 bytes
    if head != 0x8B1F:           return 0                 // "1f 8b" little-endian; not gzip
    if CM_byte != 8:             return 0                 // CM=8 (deflate) required
    // (flag/MTIME/XFL/OS bytes validated)
    return 27                                             // bid strength = 27 (bits)

// gzip_filter_read @0x4d1970 — the inflate loop.
function gzip_filter_read(filter, out_block):
    if first_call:
        z.next_out = out_buf; z.avail_out = 0x10000       // 64 KB out window (malloc'd)
        crc = crc32(0, NULL, 0)                            // 0x501d10 — CRC accumulator init
        inflateInit2_(&z, -15, "1.3.1", 112)               // 0x5020d0 — RAW deflate
    loop:
        rc = inflate(&z, 0)                                // 0x502260
        if rc == Z_STREAM_END(1): break
        if rc < 0: archive_set_error("gzip decompression failed"); return FAIL
        // ("truncated gzip input" on short input)
    crc = crc32(crc, produced, n)                          // verify member CRC trailer
    inflateEnd(&z)                                          // 0x504490 ("Failed to clean up gzip decompressor")
```

> **NOTE — raw-deflate window, not auto-gzip.** The `-15` window-bits to `inflateInit2_` is deliberate: libarchive parses the gzip framing (magic, flags, MTIME, the optional FNAME/FCOMMENT, and the trailing CRC-32/ISIZE) in its *own* C, and only the raw DEFLATE block is handed to zlib. A reimplementation that passes `15 + 16` (zlib's "auto-detect gzip") to its own `inflateInit2_` and then *also* lets the filter strip the header will double-strip and corrupt the stream. Match the split: framing in the filter, raw inflate in zlib.

### Stage 3 + 4 — iterate and extract

The walk loop calls `archive_read_next_header` (the public entry `@0x4e3ba0`, which routes through the internal `_archive_read_next_header` and the tar `read_header` vtable). For each member it applies three filters before extracting: directories are skipped, a leading `"./"` is stripped from the pathname, and any member whose name **ends with** the 18-byte literal `"wavegraph-bin.json"` is discarded. Survivors are extracted into a `malloc`'d buffer and inserted into the `neff_t::files` red-black tree keyed by the (stripped) pathname.

```c
// neff_parse @0x4ca3f0, lines 256-739 — the tar walk.
function walk_members(a, neff /* neff_t* */):
    while true:
        rc = archive_read_next_header(a, &entry)           // 0x4e3ba0
        if rc == ARCHIVE_EOF(1):                            // clean end
            archive_read_close(a); archive_read_free(a)
            return OK
        if rc != 0:                                         // rc<0 or warn
            err = archive_error_string(a)                   // "failure while reading NEFF metadata %s"
            return FAIL

        if archive_entry_filetype(entry) == 0x4000:         // AE_IFDIR — no body
            continue                                        // skip directories

        path = archive_entry_pathname(entry)                // member name
        if path startswith "./":                            // .rodata "./" prefix
            path = path + 2                                 // strip 2 leading bytes (rfind at pos 0)

        // 18-byte suffix skip: compare path tail to "wavegraph-bin.json"
        if len(path) >= 18 and
           memcmp(path + len(path) - 18, &byte_84987E - 18, 18) == 0:   // 0x84987E-18 = 0x84986C
            archive_read_data_skip(a)                       // 0x4ceec0 — discard body, NOT stored
            continue

        size = archive_entry_size(entry)                    // 0x4cc2e0
        if size < 0:                                        // "invalid files size(%ld) for %s - %s"
            return FAIL
        buf = malloc(size)                                  // exact entry size
        if buf == NULL:                                     // "fail to allocate memory(%ld) for file %s"
            return FAIL
        neff.files[path] = {buf, size}                      // RB-tree insert, dedup by pathname
        n = archive_read_data(a, buf, size)                 // 0x4cecc0 — copy decompressed bytes
        if n != size:                                       // "(open NEFF) ..." short-read reject
            return FAIL
```

> **GOTCHA — the 18-byte skip suffix is `"wavegraph-bin.json"`, matched at the END of the pathname.** The compare is `memcmp(path_tail, &byte_84987E - 18, 18)` where `byte_84987E - 18 == 0x84986C` (`.rodata` string `"wavegraph-bin.json"`, length exactly 18). It is a **suffix** test, not an exact-name test, so a member at `<dir>/wavegraph-bin.json` is also dropped. The skipped member's body is consumed with `archive_read_data_skip` so the stream stays aligned. A reimplementer must (a) match the suffix, not the whole name, and (b) still advance past the skipped body.

> **CORRECTION (W2-NEFF-CONTAINER) —** an earlier scan (SCAN-02 §2) described the skip suffix as a 18-byte `"…checksum"` literal. That is wrong: the literal is `"wavegraph-bin.json"` at `.rodata 0x84986C`, confirmed by the `memcmp((char*)path + len - 18, &byte_84987E - 18, 18)` site in `neff_parse`. There is no `"checksum"` suffix skip in the container walk.

> **CORRECTION (W2-NEFF-CONTAINER) —** the same scan pinned `archive_read_next_header` to `0x4cf0c0`. By `nm`, `0x4cf0c0` is the *internal* `_archive_read_next_header`; the **public** `archive_read_next_header` that `neff_parse` actually calls is `0x4e3ba0` (and `_archive_read_next_header2` `0x4cef40` / `archive_read_next_header2` `0x4e3bb0` are the paired variants). The `0xDEB0C5` object-magic check (`__archive_check_magic`) lives in the internal entry at `0x4cf0c0`.

### The member table — `neff_t`

The output of the walk is `neff_t` (ord 5894, 48 bytes): a single `std::map<std::string, std::pair<void*, long>>` (`files`), backed by a libstdc++ `_Rb_tree`. The key is the stripped pathname; the value is `(decompressed buffer, byte size)`. `neff_get_file_content` (`@0x4cb670`) is the RB-tree lookup the manifest router uses to fetch members by name.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `files` | `+0` | `std::map<std::string, std::pair<void*,long>>` (48 B) | pathname → `(malloc'd buffer, size)`; red-black tree, dedup by name |

> **NOTE — extraction is eager and total.** The walk unpacks *every* surviving member into its own heap buffer up front; there is no lazy/streaming member access. Memory cost is the full decompressed archive, held until `neff_destroy` frees each buffer and the tree. The map is keyed by the *stripped* pathname, so a lookup for `"neff.json"` matches a tar member written as `"./neff.json"`.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `archive_read_new` | `0x4ce790` | allocate the libarchive object | HIGH |
| `archive_read_support_format_tar` | `0x4da0c0` | register ustar/pax/gnu bidder + reader | HIGH |
| `archive_read_support_filter_gzip` | `0x4d1f40` | register the gzip filter bidder | HIGH |
| `archive_read_open_memory` | `0x4d1810` | wrap `data@+0x400` as a client stream → `open_memory2` | HIGH |
| `archive_read_open_memory2` | `0x4d1740` | wire `memory_read*` callbacks over the RAM buffer | HIGH |
| `memory_read` | `0x4d1650` | hand out `[pos..end]` capped at read-size (no copy) | HIGH |
| `gzip_bidder_bid` | `0x4d1e10` | recognize `1f 8b` / CM=8 → bid `27` | HIGH |
| `gzip_bidder_init` | `0x4d1820` | alloc `z_stream` + `malloc(0x10000)` 64 KB out-buf | HIGH |
| `gzip_filter_read` | `0x4d1970` | `inflateInit2_(-15,"1.3.1",112)`; inflate loop; `crc32` | HIGH |
| `archive_read_next_header` | `0x4e3ba0` | parse one tar member header (public entry) | HIGH |
| `archive_entry_filetype` | `0x4cbb00` | `AE_IFDIR (0x4000)` directory check | HIGH |
| `archive_entry_pathname` | `0x4cc050` | member name accessor | HIGH |
| `archive_entry_size` | `0x4cc2e0` | member byte-count accessor | HIGH |
| `archive_read_data` | `0x4cecc0` | copy decompressed entry bytes into the caller buffer | HIGH |
| `archive_read_data_skip` | `0x4ceec0` | discard a skipped member's body | HIGH |
| `inflate` / `inflateInit2_` / `inflateEnd` | `0x502260` / `0x5020d0` / `0x504490` | zlib 1.3.1 raw-deflate decompressor | HIGH |
| `crc32` | `0x501d10` | gzip-member CRC-32 verification | HIGH |
| `neff_get_file_content` | `0x4cb670` | RB-tree lookup `files[name] → (buf,size)` | HIGH |

---

## 4. Manifest Routing

### Purpose

The member table is just bytes-by-name; the **manifest chain** gives them meaning. `kmgr_load_nn_nc` fetches the top-level graph manifest (`neff.json`, or legacy `kelp.json`), parses it with simdjson to find the KELF discriminator node, then follows the named `kelf-a.json` member to enumerate the subgraphs. The whole chain is string-addressed: each manifest names the next by a JSON string value, never by an offset or an index into a table.

### Entry Point

```text
kmgr_load_nn_nc (0xde280)
  ├─ neff_parse (0x4ca3f0)                              ── -> neff_load_info->neff (member table)
  ├─ neff_get_file_content(neff, "neff.json")           ── primary graph manifest
  │    └─ (fallback) neff_get_file_content("kelp.json")  ── legacy; else "Invalid NN: %s, neff.json is missing"
  ├─ parse_neff_json (0xe1d10)                           ── simdjson DOM; find attrs.func_name=="__kelf"
  │                                                         -> strdup(attrs.kelf) = kelf-a.json member name
  ├─ dlr_kelf_load (0xe0830)                             ── -> kelf::kelf::load: version/target/graphs[]
  └─ get_model_hlo_stats (0x4c9c70)                      ── optional "hlo_stats.json" -> mac_count
```

### Algorithm

```c
// kmgr_load_nn_nc @0xde280, lines 285-365 (the container-facing slice).
function load_manifests(neff_buf, neff_size, neff_load_info):
    rc = neff_parse(neff_buf, neff_size)                  // §1-§3: header+integrity+unpack
    if rc: return rc

    // primary manifest, with legacy fallback
    if neff_get_file_content(neff, "neff.json", &json, &len) != OK:    // 0x4cb670
        if neff_get_file_content(neff, "kelp.json", &json, &len) != OK:
            reject  // "Invalid NN: %s, neff.json is missing"

    // simdjson DOM parse; locate the __kelf node and build the IO maps
    rc = parse_neff_json(json, &kelf_member_name,                       // 0xe1d10
                         &neff_load_info.input_info,
                         &neff_load_info.output_info)
    // parse_neff_json reads top-level {nodes, arg_nodes, heads, node_row_ptr, attrs};
    // the KELF node is the one whose attrs.func_name == "__kelf" (.rodata 0x83fd8d);
    // *kelf_member_name = strdup(attrs.kelf string).  Errors:
    //   "No KELF node found in NEFF JSON" / "KELF node found but missing 'kelf' attribute" /
    //   "KELF attribute is not a string" / "Found KELF node: %s"

    rc = dlr_kelf_load(neff, kelf_member_name, &kelf_gr)               // 0xe0830
    // -> kelf::kelf::load: neff_get_file_content(kelf_member_name) + simdjson DOM
    //    keys {version, target, graphs[]{definition, name}}; each graph -> construct_kbin

    // optional model statistics
    if neff_get_file_content(neff, "hlo_stats.json", &stats, &slen) == OK:
        get_model_hlo_stats(stats, slen, &mac_count)                   // 0x4c9c70
    else:
        warn  // "Unable to find hlo_stats.json in the NEFF"
```

### Encoding — the three SIMD tiers

Every manifest is parsed by **simdjson 0.9.0** in DOM mode (`parser::parse_into_document`, then `at_key` / `get_string` / `get_uint64` / `get_array`). simdjson compiles three target-specific stage-1/stage-2 implementations and dispatches at runtime on CPU features; all three are present in `libnrt.so`:

| Tier | Symbol namespace | Target | Selected when |
|---|---|---|---|
| `haswell` | `simdjson::haswell::` | AVX2 + BMI | AVX2-capable host (the common Trn/Inf instance) |
| `westmere` | `simdjson::westmere::` | SSE4.2 | SSE4.2 host without AVX2 |
| `fallback` | `simdjson::fallback::` | scalar | no SIMD; portability floor |

> **NOTE — the SIMD tier is a runtime CPU dispatch, not a build flag.** All three implementations ship in the binary (28 `haswell` + 28 `westmere` + 27 `fallback` symbols by `nm`). simdjson's `internal::dom_parser_implementation` is selected once on first use by `__builtin_cpu_supports`. A reimplementer porting the manifest parse can pick any conformant JSON DOM parser — the tier choice affects throughput, not semantics; the three tiers produce byte-identical DOM trees.

### The member-name vocabulary

The format's named members, with their `.rodata` string addresses where fixed. The KELF member and the per-subgraph `def.json` / `<eng>.bin` members are **not** fixed literals — their names are JSON string values resolved at parse time.

| Member | String addr | Role |
|---|---|---|
| `neff.json` | (literal in `kmgr_load_nn_nc`) | top-level TVM-relay graph manifest (preferred) |
| `kelp.json` | `0x83f9cd` | legacy fallback graph manifest (same parser) |
| `hlo_stats.json` | `0x83fa32` | optional MAC-count statistics |
| `wavegraph-bin.json` | `0x84986c` | the 18-byte SKIP suffix — any member ending with it is dropped |
| `"./"` | (prefix) | leading prefix stripped from every member pathname |
| `__kelf` | `0x83fd8d` | `attrs.func_name` value identifying the KELF graph node |
| `<attrs.kelf>` | — | the `kelf-a.json` member, named by the `__kelf` node's `kelf` string |
| `<dir>/def.json` | — | per-subgraph definition (path = `graphs[].definition`) |
| `<dir>/<eng>.bin` | — | per-engine instruction/data blob (built by `load_bin_file`) |

> **QUIRK — `neff.json` is a TVM-relay graph, not a NEFF table of contents.** The top-level manifest is a TVM-relay-style graph (`nodes`/`arg_nodes`/`heads`/`node_row_ptr`/`attrs`), inherited from the compiler's IR. The container's "directory" is therefore a *graph node attribute*: the executable lives wherever the node with `attrs.func_name == "__kelf"` points its `attrs.kelf` string. There is no separate index member listing the archive contents — see [NEFF Metadata Schema](metadata-schema.md) for the full `neff.json` DOM.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `kmgr_load_nn_nc` | `0xde280` | top-level loader: parse → select manifest → route | HIGH |
| `parse_neff_json` | `0xe1d10` | simdjson DOM of `neff.json`; `__kelf` discriminator + IO maps | HIGH |
| `parse_neff_json.cold` | `0x45a5d` | cold/clone entry of the same function | MEDIUM |
| `dlr_kelf_load` | `0xe0830` | `calloc(kelf_nn_t)` + `kelf::kelf::load` over `kelf-a.json` | HIGH |
| `get_model_hlo_stats` | `0x4c9c70` | read `hlo_stats.json` → `mac_count` (warn if missing) | HIGH |

### The downstream structures (bridged out of this page)

`parse_neff_json` builds two structs this page only hands off. `io_node_info_t` (ord 2948, 144 B) is the per-tensor IO descriptor (`+0 size`, `+8 dtype`, `+12 shape[32]`, `+140 ndim`); `neff_load_info_t` (ord 13443, 112 B) is the TNC cache value (`+0 neff_t*`, `+8 kelf_nn_t* kg`, `+16/+64` the input/output `IO_MAP`s). `kelf_nn_t` (ord 6845, 24 B: `+0 nkbin`, `+8 kbin*`, `+16 sg_names`) is the loaded executable. Their fields and the `def.json`/`<eng>.bin` parse are owned by the schema, taxonomy, and load-pipeline siblings.

---

## Related Components

| Name | Relationship |
|---|---|
| `neff_parse` (`@0x4ca3f0`) | the container entry — header gate, integrity, tar walk into `neff_t::files` |
| `neff_t` / `neff_get_file_content` | the in-memory member table and its by-name RB-tree lookup |
| libarchive 3.8.0dev | the in-RAM gzip-tar reader; `open_memory` + tar bidder + gzip filter |
| zlib 1.3.1 | the raw-deflate decompressor driven by the gzip filter |
| simdjson 0.9.0 | the DOM JSON parser for every manifest, three CPU tiers |
| `kmgr_load_nn_nc` (`@0xde280`) | the manifest router that consumes the member table |

## Cross-References

- [Overview: the Model-File Journey](overview.md) — where the container parse sits in the full NEFF → KBIN → device load
- [NEFF Metadata Schema (TVM-Relay JSON)](metadata-schema.md) — the `neff.json` / `kelp.json` DOM that `parse_neff_json` walks for the `__kelf` node and IO maps
- [NEFF Section Taxonomy](section-taxonomy.md) — what each named TAR member means once unpacked (the "sections" that are really member pathnames)
- [Static Memory Planning (mem_ref)](memory-planning.md) — `parse_one_variable` / `kelf_load_from_neff`, the `def.json` parse that consumes the members this page extracts
- [The Load Pipeline (Parse → Build → Stage → Relocate)](load-pipeline.md) — where `kmgr_load_nn_nc` and `dlr_kelf_load` sit in the model-load sequence
- [Public C API: Lifecycle and Init/Teardown](../runtime/api-lifecycle.md) — the `nrt_load` entry whose buffer this container parse receives
- [back to index](../index.md)
