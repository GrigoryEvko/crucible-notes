# The `neff_header` POD, the In-Memory BOM, and the NeffPackager Writer

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). The writer lives in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset). The NEFF emission path is byte-identical in the cp311/cp312 wheels, but their addresses shift by a small per-wheel delta (e.g. `addToBom` is `0x153fb80` here, `0x153fae0` in cp312); treat every address as version-pinned. All claims were re-verified against the cp312 `libwalrus.so` via `nm -DC`, `strings`, and `grep -aob`; the in-function byte offsets come from the cp310 IDA decompile. See [versions](../reference/versions.md).*

## Abstract

A NEFF is **not** a binary file with a header struct at offset 0. It is a libarchive PAX tar stream (gzip-wrapped by default; the container itself is the subject of Part 12.1) whose "sections" are tar members — named files copied verbatim from a per-core build directory. The struct that everyone calls "the NEFF header" — `neuronxcc::backend::neff_header`, the flat POD carrying the format version (`2`), a `0x400`-byte region size, an RFC-4122-v4 UUID, the output filename, a per-engine present bitmap, and the 64-bit feature mask — is built **on the stack in-process** by `NeffFileWriter::initializeNeffHeader` (`0x1540a00`) and its authoritative on-disk copy is serialized **inside `info.json`** (itself one tar member). There is no leading on-disk header record to parse. This is the single most important fact on the page, and the one a reimplementer is most likely to get wrong.

The second hazard is the word **"BOM."** It denotes three unrelated objects in this codebase, and prior analysis conflated them. (1) The **NEFF BOM** — Bill-Of-Materials — is the writer's in-memory file manifest: a `std::map<boost::filesystem::path, std::string>` at `NeffFileWriter+0x90` mapping each artifact's on-disk path to its in-tar member name, populated one entry at a time by `addToBom` (`0x153fb80`) and drained in key order by `writeArchiveFile` (`0x153e030`). It has **no byte marker** and is never serialized as a manifest record — the tar member list *is* the manifest. (2) The **UTF-8 BOM** (`0xEF 0xBB 0xBF`) is the byte-order-mark the nlohmann JSON lexer tolerates at the head of any JSON sidecar (`skip_bom`, `0x853a50`); it has nothing to do with the NEFF container. (3) The source-named object **`bom`** is `bir::ModuleArtifactInfo`, the per-module record that owns the engine→file `DenseMap`s. The page pins each sense separately.

For reimplementation, the contract is:

- The `neff_header` POD field layout — every offset, the override (`file.neff`) fast path vs. the `info.json`-driven normal path, and the proof that the header lives inside `info.json` rather than as a leading record.
- The BOM `std::map<path,member>` entry format, the `addToBom` upsert algorithm, and the IR-signature side-set that decides which members feed the whole-archive MD5.
- The `writeArchiveFile` libarchive PAX write loop — member ordering, per-member `stat`/`fopen`/`fread`, and the MD5 "IR signature."

| | |
|---|---|
| **Header builder** | `NeffFileWriter::initializeNeffHeader` @ `0x1540a00` (`neff_file_writer.cpp`) |
| **POD type** | `neuronxcc::backend::neff_header` — flat POD ≥ 556 B, built on stack, copied into `info.json` |
| **Format version** | `2` (rodata OWORD; the `info.json` major/minor are a *separate* `1.x`) |
| **Region size** | `0x400` (1024) — same OWORD |
| **UUID** | 16-byte RFC-4122 v4, ChaCha20-12 CSPRNG seeded `"expand 32-byte k"` |
| **BOM manifest** | `std::map<path,string>` @ `NeffFileWriter+0x90` (root `+0xA0`), upserted by `addToBom` @ `0x153fb80` |
| **IR-sig side-set** | `std::set<string>` @ `NeffFileWriter+0xD8`; ctor-seeds `"info.json"` |
| **Writer** | `NeffFileWriter::writeArchiveFile` @ `0x153e030` (`archive_write_set_format_pax`) |
| **Driver** | `NeffPackager::run` @ `0x15307e0` → `writePackageFile` @ `0x15200e0` → `writeFile` @ `0x1541040` |
| **Shipped consumer** | `neuronxcc/starfish/bin/analyze_neff_artifacts.py` (pure `json.load` + `os.stat`) |

---

## The `neff_header` POD

### Purpose

`neff_header` is the metadata block describing one compiled NEFF: which format version it is, which of the five TPB engines carry instructions, a unique build identity (UUID), the output filename, and the 64-bit feature bitmask the runtime uses to gate loader behavior. It is the closest thing a NEFF has to an "ELF header" — but unlike an ELF header it is never written as a leading binary record. It is materialized on the stack, then its fields are emitted as JSON keys into `info.json`.

> **QUIRK — there is no on-disk header struct.** A reimplementer who seeks `e_ident`/magic at offset 0 of a `.neff` finds either the gzip magic `0x1F 0x8B` (default) or the tar `ustar` magic at `0x101` of the first 512-byte member header — never `neff_header`. The POD is built by `initializeNeffHeader` purely so `writeArchiveFile` can carry it as metadata and so the packager can serialize its fields into the `info.json` member. To read a NEFF's "header," you extract `info.json` and parse its JSON. This corrects the long-standing "NEFF ELF container" premise: `nm -DC libwalrus.so` shows the *complete* libarchive tar API including `archive_write_set_format_pax` and **zero** ELF-writer symbols (`ELFObjectWriter`/`elf_begin`/`elf_newscn`/`Elf64_Ehdr`/`writeELF` = no hits).

### Field Layout

The POD is built on the stack in `initializeNeffHeader` (`0x1540a00`). The first two quadwords are a single 16-byte rodata constant (`0x1DD7900`, read directly: `02 00 00 00 00 00 00 00  00 04 00 00 00 00 00 00`); the rest are written field-by-field from `info.json` (normal path) or hardcoded (override path).

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `version` | `+0` | `u64` | NEFF format version = **`2`** (rodata OWORD `0x1DD7900`, xxd-confirmed) | CONFIRMED |
| `region_size` | `+8` | `u64` | **`0x400`** (1024) region/header size (same OWORD) | CONFIRMED |
| *(reserved)* | `+16` | `u64` | `0` | STRONG |
| `section_flag` | `+24` | `u64` | `= NeffFileWriter+0xC8 (=1)` — module/section count flag | STRONG |
| `version_major` | `+168` | `u32` | `1` (override) or `info.json` uint (normal) | STRONG |
| `feature_flags` | `+192` | `u64` | 64-bit feature mask (built by `writeNEFFFeatures` `0x15294b0`; bit catalog is [12.5](neff-feature-flags.md)) | CONFIRMED |
| `schema_min` | `+200` | `u32` | schema min version; bumped ≥ 2 when functions/ext-feats and arch > 39 | STRONG |
| `uuid` | `+204` | `byte[16]` | RFC-4122 **v4** UUID, ChaCha20-12 CSPRNG | CONFIRMED |
| `filename` | `+220` | `char[255]` | `"file.neff"` (override) or `info.json` (normal) | CONFIRMED |
| `version_minor` | `+476` | `u32` | `1` (override) or `info.json` uint | STRONG |
| `engine_present` | `+480` | `byte[64]` | per-engine "has instructions" bitmap (`+480..+544`) | CONFIRMED |
| `cc_field` | `+544` | — | `= NeffFileWriter+192` (collective rank / world-size) | STRONG |
| `arch_mode` | `+552` | — | `= NeffFileWriter+88` (arch-mode word) | STRONG |

> **NOTE — two different "versions" live in one NEFF.** `neff_header.version == 2` is the binary container-format version (the rodata OWORD). It is **not** the same as the `info.json` `version` major/minor (a `1.x` pair, default `1.1` in the override path), nor the `def.json` schema string `"0.6"` / `neff.json` `"0.5"`. A reimplementer who reads `info.json["version"]` gets the `1.x` document version, not the `2`. The `2` is structural and lives in `neff_header+0`.

### The UUID — ChaCha20 CSPRNG

The 16-byte UUID at `+204` is a standard RFC-4122 version-4 (random) UUID, but the randomness source is a **ChaCha20-12** stream cipher, not `/dev/urandom` or `boost::random`. The rodata constant immediately after the version OWORD — `0x1DD7910`, the ASCII `"expand 32-byte k"` (xxd-confirmed, the canonical ChaCha/Salsa sigma constant) — is the cipher's block-init string. After 16 random bytes are drawn, the version and variant nibbles are forced in the usual way:

```c
// initializeNeffHeader @0x1540a00 — UUID field (header+204), normal path
chacha20_fill(uuid, 16);              // ChaCha20-12 keystream; sigma "expand 32-byte k" @0x1DD7910
uuid[6] = (uuid[6] & 0x0F) | 0x40;    // RFC-4122 version 4 (random) nibble
uuid[8] = (uuid[8] & 0x3F) | 0x80;    // RFC-4122 variant (10xxxxxx)
```

> **NOTE —** "version 4" here is the *UUID* version (RFC-4122 random), an entirely separate `4` from the `neff_header.version == 2` container version. Both numbers are real and both matter; do not collapse them.

### Override vs. Normal Path

`initializeNeffHeader` branches on `NeffFileWriter+0x108` (offset 264), the `internalOverride` bool set when the writer is in the single-output `file.neff` fast path:

```c
function initializeNeffHeader(neff_header &h):      // 0x1540a00
    h.qword[0] = 2;                                  // rodata OWORD @0x1DD7900 (version)
    h.qword[1] = 0x400;                              // region size
    h.qword[16] = 0;
    h.qword[24] = *(this + 0xC8);                    // section/flag count (=1)
    fill_uuid_v4(&h.uuid[204]);                       // ChaCha20-12, sigma @0x1DD7910

    if (*(this + 0x108)) {                            // internalOverride (writer+264)
        strcpy(&h.filename[220], "file.neff");        // hardcoded name (string @rodata)
        h.version_major[168] = 1;
        h.version_minor[476] = 1;
        memset(&h.engine_present[480], 0, 64);        // zero the engine bitmap
        // skips info.json entirely
    } else {
        path ij = findInfoJson();                     // 0x153ca20 -> <baseDir>/info.json
        json j  = loadJsonFile(ij);
        h.version_major[168] = j["version"].major;    // info.json uint
        h.version_minor[476] = j["version"].minor;
        copy_filename(&h.filename[220], j /* name */);
        copy_engine_bitmap(&h.engine_present[480], j);// per-engine bool array
    }
    h.feature_flags[192] = compute_features();        // writeNEFFFeatures 0x15294b0
    // h.cc_field[544], h.arch_mode[552] from writer+192 / writer+88
```

> **GOTCHA — the override path zeroes the engine bitmap and skips `info.json`.** In the `file.neff` single-output fast path, the engine-present array at `+480` is `memset` to zero and `info.json` is never read. A reimplementer who assumes the bitmap always reflects the real engine set will be wrong for override-mode NEFFs. The bitmap is only meaningful on the normal path, where it is copied from `info.json`'s per-engine bool array.

---

## The In-Memory BOM Manifest

### Purpose

The BOM (Bill-Of-Materials) is the writer's manifest of every file that will become a tar member. It is the NEFF's "section index" — except it is an *in-process* `std::map`, never shipped in the NEFF. What the runtime sees is the *result*: the tar member list, whose names are exactly the BOM map's values, in BOM key order (path-sorted, deterministic).

> **QUIRK — the BOM is a `std::map`, not a serialized table.** It has no byte marker, no per-entry offset/size, and no per-entry checksum. Offsets and sizes come from `stat()` at write time; integrity is a single whole-archive MD5 over a *subset* of members (below), not a per-entry hash. Anyone hunting for a `0xEF 0xBB 0xBF` "BOM marker" is hunting a phantom — that byte sequence is the nlohmann JSON UTF-8 byte-order-mark, a property of the *JSON sidecars' encoding*, checked by `skip_bom` (`0x853a50`), not the NEFF manifest.

### The Three "BOM" Senses

| Sense | Object | Where | What it is | Confidence |
|---|---|---|---|---|
| **(1) NEFF BOM** | `std::map<path,string>` | `NeffFileWriter+0x90` | file manifest: on-disk path → in-tar member name; built by `addToBom` `0x153fb80` | CONFIRMED |
| **(2) UTF-8 BOM** | `0xEF 0xBB 0xBF` | rodata `0x1d1bfb8`/`0x1dc8da0` | JSON byte-order-mark; checked by nlohmann `skip_bom` `0x853a50`/`0x181f3e0` | CONFIRMED |
| **(3) `bom` object** | `bir::ModuleArtifactInfo` | `libBIR` (`U` in libwalrus) | per-module engine→file `DenseMap` holder (`getEngInstrFile`/`getEngDMADescFile`) | CONFIRMED |

> **CORRECTION (S2-10 / task premise) —** the task premise stated "the BOM marker is `0xEF 0xBB 0xBF` (UTF-8 BOM bytes, reused as a file-manifest marker)." This is false. The `0xEF 0xBB 0xBF` bytes are **not** a NEFF manifest marker, are not stored in the BOM, and are not per-entry. They are the literal UTF-8 byte-order-mark that the nlohmann lexer tolerates at the head of any JSON sidecar. The only code touching `0xEF/0xBB/0xBF` in `libwalrus` is `skip_bom` (string `"invalid BOM; must be 0xEF 0xBB 0xBF if given"`, present twice — the two lexer instantiations). The NEFF BOM (sense 1) has no byte marker whatsoever.

### Entry Format

A BOM entry is a `std::_Rb_tree` node in the `NeffFileWriter+0x90` map:

```c
struct bom_entry {            // std::_Rb_tree node in map @writer+0x90
    boost::filesystem::path on_disk_path;   // KEY   — node+0x20; post-computeOutputPath
    std::string             tar_member_name;// VALUE  — node+0x40; the in-tar member name
};                             // NO offset/size/hash/type field — those are derived at write time
```

There is **no** offset, size, checksum, or type field. File typing is expressed three other ways (member-name convention; the `def.json` `var['type']` tag; and a boolean MD5-sig membership), none of which lives in the BOM node.

### Algorithm — `addToBom`

```c
function addToBom(path file, optional<string> entryNameOverride):  // 0x153fb80
    out = computeOutputPath(file);            // 0x153c7a0 — the MAP KEY
                                              //   rehomes per-module files under nc<core>/
                                              //   if file in the per-module dir map (writer+0x68),
                                              //   else joins to the writer base dir
    entry = entryNameOverride.has_value()     // disasm: cmp byte [a3+0x20],0 @0x153fbcb
              ? *entryNameOverride
              : file.filename();              // 0x175D1C0 = basename -> the MAP VALUE

    // --- IR-signature classification (writer+0xD8, a std::set<string>) ---
    base = out.filename();                    // basename of the ON-DISK path
    if (base.compare(".npy") == 0)            // @0x153fc6f; ".npy" @rodata 0x1c8b929
        irsig_set.insert(entry);              // full-string equal (NOT endsWith) — see Gap G1
    if (base.find(".dbg", 0, 4) != npos)      // @0x153fc9b; ".dbg" @rodata 0x1c86c5b (n=4,pos=0)
        irsig_set.insert(entry);

    // --- BOM map UPSERT (last-write-wins on the path key) ---
    node = rb_tree_insert_or_locate(map@+0x90, file);   // sub_153FA40; path-compares walk tree
    node->value._M_assign(entry);             // @0x153fd53 -> node+0x40; re-adding overwrites
```

> **GOTCHA — last-write-wins on the path key.** Re-adding the same on-disk path overwrites its member name. The `.dbg` debug-info files are registered twice — once by `writeDefJson` under `"<eng>_dbg"`/`"<eng>_asm_dbg"`, and again by `writePackageFile` (`0x15200e0:551`) under the literal override `"debug_info"`. A `.dbg` file's *final* tar member name is whichever registration ran last. Either way its basename contains `.dbg`, so it enters the MD5 IR-sig set regardless.

> **CORRECTION (J34 G1 / J36 "Sig?" column) —** the MD5 IR-signature subset is **not** ".bin streams + signature JSON," as earlier strands guessed. The byte-proven `addToBom` predicate is: a member enters the IR-sig set iff its on-disk basename **equals `".npy"`** OR **contains `".dbg"`**, plus the ctor pre-seed `"info.json"`. So the signed set is exactly `{info.json} ∪ {*.dbg debug-info} ∪ {*.npy const/weight}`. The per-engine `.bin` instruction streams are **not** signed. (Predicate at `0x153fc6f`/`0x153fc9b`; targets `0x1c8b929=".npy"`/`0x1c86c5b=".dbg"`; ctor seed at `0x1543eb0:228`.)

> **NOTE (Gap G1, STRONG) —** the `.npy` branch is `compare(".npy")==0` — a *full-string* equality, firing only when a basename is literally `".npy"` (an extension-only name). A real weight named `"<w>.npy"` would **fail** the `==0` test. If const files are named `<weight>.npy`, the `.npy` branch is vestigial and the signature is carried by `info.json` + the `.dbg` files. The byte-truth is `compare()==0`, not `endsWith`.

---

## The NeffPackager Writer Path

### Entry Point

```text
NeffPackager::run(vector<unique_ptr<Module>>&)        0x15307e0  — pass "neff_packager" (last)
  └─ writePackageFile(NeffFileWriter&, modules)        0x15200e0  — BOM assembler
       ├─ writeNeffJson(...)        0x152c740  -> neff.json  (v0.5 subgraph table)
       ├─ writeDefJson(...)         0x152a0e0  -> def.json   (v0.6 per-core, + per-engine addToBom)
       ├─ writeNEFFFeatures(...)    0x15294b0  -> neff_header+192 feature mask
       ├─ kelf- prefix scan         0x15200e0:31  — collect kelf-<N>.json into BOM
       └─ debug_info dual-register  0x15200e0:551 — addToBom .dbg under "debug_info"
  └─ NeffFileWriter::writeFile(outPath, uncompressed)  0x1541040  — top-level driver
       ├─ initializeNeffHeader(neff_header&)            0x1540a00  — POD build
       ├─ writeArchiveFile(&hdr, md5, compress)         0x153e030  — libarchive PAX write
       └─ MD5::final -> hex -> log "IR signature (MD5): <hex>" -> dump BOM
```

### Per-Engine Registration — `writeDefJson`

`writeDefJson` (`0x152a0e0`) walks the `bir::ModuleArtifactInfo` (sense-3 `bom`) `DenseMap`s — `getEngInstrFile` (instruction files) and `getEngDMADescFile` (DMA-descriptor files), bucket stride 40 bytes — and for each engine emits a `def.json` token and registers the file in the BOM. The engine name has **two spellings at two layers**, the single most error-prone point:

- **On-disk `.bin`/`.json` basenames** use `bir::EngineInfo2string` (libBIR) = **TitleCase**: `PE`, `Pool`, `Activation`, `SP`, `DVE`. Confirmed directly by the shipped consumer `analyze_neff_artifacts.py` line 74: `for engine in ['Pool','SP','DVE','PE','Activation']`.
- **`def.json` `"definition"` key tokens** use the NEFF-local formatter `sub_15248C0` (`neff_packager.cpp:49`) = **lowercase**, a byte-confirmed strcpy switch: `1→"pool" 2→"act" 3→"pe" 4→"dma" 5→"dve" 6→"sp"` (default 0/7 throws NeuronAssertion 1222).

```c
// writeDefJson instr loop @0x152a0e0:275-351 — stride 40 over getEngInstrFile DenseMap
for (eng, basename) in module.bom.getEngInstrFile():     // TitleCase basename, e.g. "PE.bin"
    token = sub_15248C0(eng.type) + "_instr";            // lowercase, e.g. "pe_instr"  @296-299
    if (*((int*)module + 43) <= 49)                       // arch-level gate @284
        defjson["definition"][tok]["_instr"] = basename;  // @314
    addToBom(artifactDir / basename, /*override=*/token); // @340  -> path "../PE.bin" => member "pe_instr"

// dma-desc loop @392-549: addToBom(artifactDir/<dmaBasename>, override=<eng token>)   @544
// dbg loop      @682-727: token + "_dbg",      addToBom @727
// asm-dbg loop  @794-839: token + "_asm_dbg",  addToBom @839
```

So `def.json["definition"]["pe"]["_instr"] == "PE.bin"`; the BOM then maps the on-disk `.../PE.bin` path → the in-tar member name `"pe_instr"`. The five `.bin`-bearing engines are `{PE,Pool,Activation,SP,DVE}`; **DMA (token 4) has no `.bin`** — DMA descriptors fold into the issuing compute engine's `.bin` stream, and the per-engine `.json` is the descriptor-table sidecar. See [`.bin` emission](../walrus/bin-emission.md) for the per-engine stream contents.

### Algorithm — `writeArchiveFile`

```c
function writeArchiveFile(void *hdr, md5 &sig, bool compress):   // 0x153e030
    a = archive_write_new();
    archive_write_set_format_pax(a);                  // POSIX-extended (PAX) tar
    if (compress) {                                   // compress = uncompressed_flag ^ 1 (gzip default ON)
        archive_write_add_filter_gzip(a);
        archive_write_set_filter_option(a, ...) x2;   // gzip level/options (not decoded, Gap G3)
    }
    archive_write_open_filename(a, outFile);

    for (path, member) in BOM map@+0x90:               // KEY ORDER (path-sorted, deterministic)
        if (stat(path, &st) != 0)                      // else NeuronAssertion "statResult==0" (cpp:310)
            fail();
        ae = archive_entry_new();
        archive_entry_copy_stat(ae, &st);
        archive_entry_set_pathname(ae, member);        // member name = the BOM VALUE
        archive_entry_set_uname/_gname/_mtime/_ctime/_atime(ae, ...);
        fp = fopen(path, "rb");                        // else NeuronAssertion "fp" (cpp:322) +
                                                       //   log "writeKelp missing file <path>"
        archive_write_header(a, ae);
        while ((n = fread(buf, 1, 0x2000, fp))) {       // 8 KiB streaming chunks
            if (irsig_set@+0xD8 contains member)        // _Rb_tree-search the sig set on member name
                md5_update(&sig, buf, n);               // log "Adding <n> bytes of <file> to the IR Signature."
            archive_write_data(a, buf, n);
        }
        fclose(fp); archive_entry_free(ae);
    archive_write_close(a); archive_write_free(a);
```

`writeFile` (`0x1541040`) wraps this: guard empty `outPath` → assert `"foundActJson"` (cpp:160) → log `"Neff will be written to: <outPath>"` → `initializeNeffHeader` → `writeArchiveFile` → `MD5::final` → hex → log `"IR signature: <hex> for neff artifacts"` / `"IR signature (MD5): <hex>"` → dump the BOM (between literals `"The NeffPacakger BOM:"` … `"Entry <path> written to <entry>"` … `"End of NeffPacakger BOM"` — the `Pacakger` typo is in the binary, confirmed at file offset `29912342`).

> **NOTE — the identity hash is MD5 over a subset.** The "IR signature" is `boost::uuids::detail::md5` (16-byte digest), not SHA-1, and it covers only the IR-sig-set members (`info.json` + every `*.dbg` + every `*.npy`), not the whole archive. The MD5 is computed *during* the tar write (per-chunk `md5_update`), so it hashes the raw file bytes, not the compressed tar.

### The Member Roster

A `*.neff` is one PAX tar of these members (one on-disk file each), emitted in BOM key order. "Sig" = in the MD5 IR-signature set; "Per" = top-level (T) or per-core (C, under `nc<core>/sg<subgraph>/sgLnk/`).

| Member (tar entry) | On-disk file | Type | Sig | Per |
|---|---|---|---|---|
| `info.json` | `info.json` | JSON header oracle (carries the `neff_header` fields) | Y | T |
| `neff.json` | `neff.json` (v0.5) | JSON subgraph table (`sg_coreV1<i>`, `__kelf`) | n | T |
| `def.json` | `def.json` (v0.6) | JSON per-core definition (engine→{`_instr`,`_dbg`,`dma`}; var table) | n | C |
| `tensor_map.json` | `tensor_map.json` | JSON IO tensor map | n | C |
| `pe_instr` … `dve_instr` | `PE.bin`…`DVE.bin` | per-engine ISA instruction streams (DMA descs folded in) | n | C |
| `<eng token>` (×5) | `PE/Pool/…/DVE.json` | per-engine DMA-descriptor table | n | C |
| `kelf-0.json` (`kelf-<N>`) | `kelf-<N>.json` | per-core KELF engine-offset index | n | C |
| `ucode_lib.json` | `ucode_lib.json` | custom-op µcode lib | n | C |
| `<const>.bin` / `<w>.npy` | `*.bin` / `*.npy` | const/weight backing data (de-dup'd) | Y | C |
| `<eng>_dbg` / `debug_info` | `debug_info_backend.<eng>.dbg` | protobuf BIR debug info | Y | C |
| `<eng>_asm_dbg` / `debug_info` | `debug_info_asm.<eng>.dbg` | protobuf ASM debug info | Y | C |
| `kernel_debug_info.json` | `kernel_debug_info.json` | JSON ext kernel debug | n | T |
| `global_metric_store.json` | `global_metric_store.json` | JSON autotuner read-out | n | T |
| `metrics.json` / `hlo_metrics.json` | (as emitted) | JSON compile / perf-sim metrics | n | T |
| `icMetadata.json` | `icMetadata.json` | JSON IC/autotuner metadata | n | C |
| `loop.json` | `loop.json` (if any) | JSON auto-loop spec | n | T |
| `cpu.so` / `cpu.params` | `cpu.so` / `cpu.params` | custom-op host kernel (ELF) / bytes | n | T |
| `[neff_header]` | (inside `info.json`) | **NOT a member** — rebuilt in-process | – | – |

> **NOTE —** `cpu.so` *is* a real ELF — but it is a custom-op host kernel carried verbatim as a tar member, unrelated to the (non-existent) NEFF ELF header. The member-name literals above (`def.json`, `neff.json`, `tensor_map.json`, `ucode_lib.json`, `kelf-`, `debug_info_backend`, `debug_info_asm`, `_asm_dbg`, `cpu.so`, `cpu.params`) were all re-verified present via `grep -aob` in the cp312 `libwalrus.so`; the engine `.bin`/`.json` basenames are confirmed by `analyze_neff_artifacts.py` line 74.

---

## Verification and Re-Verify Ceiling

The five strongest claims were re-checked against the cp312 `libwalrus.so` this session:

| Claim | Evidence | Result |
|---|---|---|
| NEFF is PAX tar, not ELF | `nm -DC`: `archive_write_set_format_pax` present `U`; zero ELF-writer symbols | CONFIRMED |
| `neff_header` is a real named struct, built (not on-disk) | `initializeNeffHeader(neff_header&)` exported `T`; no leading record | CONFIRMED |
| Three "BOM" senses | `addToBom`/`writeArchiveFile` exports; `"invalid BOM…"` ×2; `bir::EngineInfo2string` `U` | CONFIRMED |
| UUID via ChaCha20 | `"expand 32-byte k"` present at file offset `31284944` | CONFIRMED |
| Shipped consumer is name-keyed | `analyze_neff_artifacts.py:74` `['Pool','SP','DVE','PE','Activation']` | CONFIRMED |

> **NOTE — re-verify ceiling.** The cp312 `.so` exports the full writer/packager symbol roster (every method signature matches the reports verbatim), and all the rodata strings (`file.neff`, the `NeffPacakger BOM` typo, the IR-signature logs, the engine tokens, the member-name literals, the ChaCha sigma) are present. What this session could **not** independently re-walk is the *in-function byte offsets* inside `initializeNeffHeader` (e.g. `+168`/`+480`/`+204`) and the `addToBom` IR-sig predicate addresses, because the shipped `.so` carries no local symbols and the offsets were read from the cp310 IDA decompile. Those rows are tagged STRONG/CONFIRMED-per-decompile rather than re-derived here; the cp312 addresses shift by a small delta (`addToBom` `0x153fae0` vs cp310 `0x153fb80`), so the *relative* layout is consistent but the absolute cp310 addresses are the report's, not re-walked byte-for-byte this session.

---

## Related Components

| Name | Relationship |
|---|---|
| `NeffPackager::run` (`0x15307e0`) | The `neff_packager` pass driver; assembles the BOM, then calls `writeFile` |
| `bir::ModuleArtifactInfo` (`bom`, libBIR) | Sense-3 `bom`: holds the engine→file `DenseMap`s `writeDefJson` walks |
| `nlohmann::…::skip_bom` (`0x853a50`) | The UTF-8 BOM (sense 2) lexer check — *not* the NEFF manifest |
| `analyze_neff_artifacts.py` | Shipped consumer; pure `json.load` + `os.stat` on extracted members |

## Cross-References

- [Per-Engine `.bin` Emission](../walrus/bin-emission.md) — the instruction streams `writeDefJson`/`addToBom` register and the writer packs as `<eng>_instr` members.
- [Part 12.1 — The NEFF Container](neff-container.md): the PAX tar/gzip container this writer emits; the byte-level container layout and the no-ELF/no-magic proof.
- [Part 12.5 — NEFF Feature Flags](neff-feature-flags.md): the 64-bit mask at `neff_header+192` built by `writeNEFFFeatures`, bit by bit.
