# NEFF Container

> *All addresses on this page apply to `libwalrus.so` from neuronx_cc 2.24.5133.0+58f8de22 (**cp310 wheel**; `.text` @ `0x62d660`, `.rodata` @ `0x1c72000`, VMA == file-offset for both). The NEFF write path is byte-identical across the cp310/cp311/cp312 wheels and the wheels **share the same section bases**; function *bodies*, however, shift by a per-wheel delta, so every address on this page is version-pinned to **cp310** to match the cited disassembly. Other versions will differ.*

## Abstract

A `*.neff` file is the single deliverable a neuronx_cc compilation produces: the packaged executable image a Neuron device loads. The natural assumption — and the one this part of the wiki inherited from an earlier survey — is that a NEFF is an **ELF**-style object container, with a magic number, an `e_type`, a section header table, and per-section `sh_offset`/`sh_size`/`sh_flags`. That assumption is **false in every particular**. There is no ELF header, no bespoke "NEFF" magic, and no section table anywhere in the file.

A NEFF is a **libarchive PAX-format tar stream, gzip-wrapped by default**. Its "sections" are ordinary tar *members* — named files (`info.json`, `def.json`, `PE.bin`, `Pool.bin`, weight `.bin` blobs, protobuf `.dbg` files) copied verbatim from a build directory. Its "magic" is whatever the container layer puts first: the gzip member header `1f 8b` in the default compressed mode, or the tar `ustar` magic at byte `0x101` of the first member header in `--uncompressed` mode. The structured metadata that a reader might expect in a leading binary record — version, UUID, engine-present bitmap, feature mask — lives **inside** `info.json` (itself a tar member), assembled in-process and never written as a header at offset 0.

This is provable three ways from the binary alone, and each is independent. **(1)** The container writer, `NeffFileWriter::writeArchiveFile`, calls the complete libarchive tar API (`archive_write_set_format_pax`, `archive_write_add_filter_gzip`, `archive_entry_set_*`) and **no** ELF/object-file emitter exists in the symbol table. **(2)** The CLI help text calls the output a "`file.neff` archive". **(3)** The shipped consumer, `analyze_neff_artifacts.py`, parses no container header at all — it `open()`s members *by name* and `json.load()`/`os.stat()`s them, the behavioural signature of a tar/directory, not an object file. The page reproduces the pack and unpack paths as annotated pseudocode and grounds every step in a symbol, address, or string.

For reimplementation, the contract is:

- **The container identity**: a PAX tar + gzip stream, not an object file. The exact libarchive call sequence, the format selector, and the gzip filter options.
- **Deterministic packing**: owner `nobody`, `mtime`/`ctime`/`atime` zeroed, gzip `compression-level=1` + `timestamp=false` — so two compiles of identical IR produce byte-identical NEFFs.
- **Member layout and order**: members are streamed in the key order of an in-memory `std::map<path, member-name>` BOM; data is padded to libarchive's default 512-byte tar record.
- **The header POD**: where `neff_header` (version 2, region `0x400`, RFC-4122-v4 UUID, feature mask) is built and where it actually lands (inside `info.json`).
- **The UUID CSPRNG**: a ChaCha20 keystream seeded from the constant `expand 32-byte k`, masked to RFC-4122 version 4.

| | |
|---|---|
| **Container format** | libarchive **PAX** tar (`archive_write_set_format_pax`), **gzip**-filtered by default |
| **Offset 0** | gzip `1f 8b` (default) **or** tar `ustar`@`0x101` (`--uncompressed`) — **never** ELF `7f 45 4c 46`, never a `NEFF` tag |
| **Container writer** | `NeffFileWriter::writeArchiveFile` @ `0x153df90` (D-S01 cites `0x153e030`, the per-member loop body) |
| **Packager driver** | `NeffPackager::writePackageFile` @ `0x15200e0`; `NeffPackager::run` @ `0x15307e0` (pass `neff_packager`) |
| **Header builder** | `NeffFileWriter::initializeNeffHeader` @ `0x1540a00` → `neff_header` POD (≥556 B), lands in `info.json` |
| **BOM** | `std::map<boost::filesystem::path, std::string>` @ `writer+144`; `addToBom` @ `0x153fb80` |
| **Member alignment** | libarchive default **512-byte** tar record (no `set_bytes_per_block` override present) |
| **Identity hash** | MD5 "IR signature" over the artifact-subset members (boost/llvm MD5, 16-byte digest) |
| **ELF-writer symbols** | **zero** — `nm -DC | rg 'Elf_Ehdr\|Elf_Shdr\|MCObjectWriter\|e_shoff\|writeELF'` = 0 hits |
| **Shipped consumer** | `analyze_neff_artifacts.py` (md5 `125d8537cdc2d034ba1d171cd82d0138`, identical cp310/311/312) |

---

## 1. The ELF Refutation

This is the headline, so it goes first and is made airtight before anything else is described.

> **CORRECTION (S2-10 §4a) —** The earlier "NEFF *ELF* container byte-layout" framing — `e_ident`/`e_type`/`e_machine`, a section header table, `sh_type`/`sh_flags`/`sh_offset`, an "Invalid magic" check — rests on a false premise. A NEFF has **none** of these. It is a libarchive PAX tar (gzip by default). The one string that looked like a container magic check, `"invalid BOM; must be 0xEF 0xBB 0xBF if given"`, is the **nlohmann::json UTF-8 byte-order-mark lexer** rejecting a malformed JSON BOM on a *sidecar member* — it is neither the NEFF Bill-Of-Materials nor a container magic. The two distinct things both called "BOM" are disentangled in [§5](#5-the-two-boms).

### What an ELF would require, and why none of it is present

If a NEFF were an object file, a reader would have to: read a 4-byte magic at offset 0, dispatch on `e_type`/`e_machine`, seek to `e_shoff`, walk a section header table, and resolve each section by `sh_offset`+`sh_size`. The producer would have to *emit* that header table. Neither side does any of this.

```text
PROOF 1 — the WRITER calls libarchive, and there is no ELF emitter.
  nm -D libwalrus.so | rg archive_         → 20 symbols (full PAX tar API, see §3)
  nm -DC libwalrus.so | rg -i \
    'Elf_Ehdr|Elf_Shdr|MCObjectWriter|MachO|COFF|e_shoff|writeELF|emitELF'
                                            → 0 hits   [CONFIRMED, this session]

PROOF 2 — the CLI vocabulary is a tar's, not an ELF's.
  strings -a libwalrus.so | rg 'file.neff archive'
   → "Write the compilation output (file.neff archive) in uncompressed format
      which results in faster loading of the archive during model execution."
  The only knob is gzip on/off ("archive", "uncompressed"); never a link/section flag.

PROOF 3 — the CONSUMER parses no header (see §7).
  analyze_neff_artifacts.py imports: re json operator functools argparse os sys math
   — no `struct`, no `tarfile`, no `.seek()`, no magic. It open()s members by NAME.
```

> **GOTCHA —** a substring grep for ELF section fields gives false positives, so verify the hits, never the count. `nm -DC libwalrus.so | rg -i 'sh_offset|elf'` returns 15 symbols — **all 15** are DMA dynamic-offset routines (`handle64BitStaticOffset`, `setStaticOffsetPortion`, `calcOffsets`…) where the regex matched `…StaticOffset…`/`…SetOffset…`. None is an ELF emitter. The precise patterns (`Elf_Ehdr`, `Elf_Shdr`, `MCObjectWriter`, `e_shoff`, `writeELF`) return **zero**. The no-ELF-writer claim survives the adversarial check. [CONFIRMED]

### The byte at offset 0

```text
default (gzip):   1f 8b 08 …            gzip member header (RFC 1952)   [filter CONFIRMED in binary]
--uncompressed:   <tar member header>   ustar\0 ASCII at header byte 0x101
NEVER:            7f 45 4c 46           ELF magic — absent
NEVER:            4e 45 46 46 ("NEFF")  no bespoke container tag at offset 0
```

The gzip filter is binary-confirmed (`archive_write_add_filter_gzip`, [§3](#3-the-write-path)); the `1f 8b` bytes themselves are the gzip member header defined by RFC 1952 — a spec fact about gzip's output, not a literal stored in `libwalrus.so`. The `ustar` magic at offset `0x101` of a tar header is likewise the POSIX tar layout that libarchive's PAX writer emits.

### The answers, in the form the question actually has

| ELF concept asked about | Reality in a NEFF | Anchor |
|---|---|---|
| `e_ident` / magic | gzip `1f 8b` (default) or tar `ustar`@`0x101`; never `7f454c46` | [§3](#3-the-write-path) |
| `e_type` | N/A — container "type" is POSIX PAX tar | `archive_write_set_format_pax` |
| `e_machine` | N/A — Neuron ISA lives **inside** per-engine `.bin` members (0x40-byte bundles) | [§4](#4-the-member-layout) |
| section header table | tar member directory: the in-memory BOM `std::map<path, member>` in key order | `addToBom` @ `0x153fb80` |
| `sh_offset` / `sh_size` | a member's pathname + its file bytes + 512-byte tar block alignment | [§3](#3-the-write-path) |
| `SHF_ALLOC` | none — nothing in a NEFF is a loadable ELF segment; libnrt tar-extracts then maps | — |

---

## 2. Producer Topology

### Purpose

Two C++ classes cooperate to build a NEFF. `NeffPackager` walks the per-core `bir::Module` vector, emits the JSON metadata members (`neff.json`, `def.json`, `ucode_lib.json`, …), and registers every artifact file into the BOM. `NeffFileWriter` owns the header POD and the actual libarchive tar/gzip write. The split matters to a reimplementer: the *manifest* (which files, in which order, under which member names) is `NeffPackager`'s job; the *byte emission* (tar framing, gzip, determinism) is `NeffFileWriter::writeArchiveFile`'s.

### Entry Point

```text
NeffPackager::run(vector<unique_ptr<Module>>&)          @ 0x15307e0   (pass "neff_packager", last)
  │  asserts modules.size() == options.vnc_nc_count     (neff_packager.cpp:113)
  └─ NeffPackager::writePackageFile(NeffFileWriter&, modules)  @ 0x15200e0
       ├─ writeNeffJson   @ 0x152c740   → neff.json   (subgraph table, schema v0.5)
       ├─ writeDefJson    @ 0x152a0e0   → def.json    (per-core definition, schema v0.6)
       │    ├─ writeVarDefinitions       @ 0x1526530  (var/tensor table + const dedup)
       │    ├─ writeDMAQueueDefinitions  @ 0x1524d10
       │    ├─ writeCCInfo               @ 0x1523af0
       │    └─ writeFP8Config            @ 0x1521e70
       ├─ writeNEFFFeatures @ 0x15294b0  → 64-bit feature mask → neff_header+192
       └─ NeffFileWriter::writeFile(outPath, uncompressed)  @ 0x1541040
            ├─ initializeNeffHeader(neff_header&)  @ 0x1540a00   (POD build, §6)
            ├─ writeArchiveFile(hdr, md5, compress) @ 0x153df90  (libarchive PAX writer, §3)
            └─ MD5::final → "IR signature: <hex> for neff artifacts" → dump BOM
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `NeffPackager::run` | `0x15307e0` | Driver; one `bir::Module` per virtual NeuronCore | CONFIRMED |
| `NeffPackager::writePackageFile` | `0x15200e0` | BOM assembler; iterates module vector | CONFIRMED |
| `NeffFileWriter::writeFile` | `0x1541040` | Top-level write driver | CONFIRMED |
| `NeffFileWriter::initializeNeffHeader` | `0x1540a00` | Builds `neff_header` POD on the stack | CONFIRMED |
| `NeffFileWriter::writeArchiveFile` | `0x153df90` | **The libarchive PAX+gzip writer** | CONFIRMED |
| `NeffFileWriter::addToBom` | `0x153fb80` | Upserts one `path → member-name` BOM entry | CONFIRMED |
| `NeffFileWriter::findInfoJson` | `0x153ca20` | Resolves `<baseDir>/info.json` | CONFIRMED |
| `NeffFileWriter::computeOutputPath` | `0x153c7a0` | Output path composition | CONFIRMED |

> **NOTE —** the `0x153df90` entry above is the true function start (where `archive_write_new` → `archive_write_set_format_pax` → `archive_write_open` are called). D-S01 cites `0x153e030`, which is the per-member loop body, ~0xa0 bytes into the function. Both are correct frames of the same routine; this page anchors the *setup* to the entry and the *loop body* to `0x153e030+`. (Two-VA-frame: the `archive_*@plt` thunk addresses are a distinct frame again — see [§3](#3-the-write-path).)

> **NOTE (M2 — verifier frame) —** the `NeffPackager`/`NeffFileWriter` method addresses in the tables above and in [§2](#2-producer-topology) (`writePackageFile 0x15200e0`, `writeDefJson 0x152a0e0`, `writeNeffJson 0x152c740`, `writeNEFFFeatures 0x15294b0`, `initializeNeffHeader 0x1540a00`) are the **`.dynsym` symbol-start / function-entry** addresses on the cp310 binary — each is the prologue (`push %r15`), confirmed against the IDA `functions.json` `addr` field and the demangled `.dynsym` value (both equal). A verifier running `objdump -d --start-address=0x15200e0` lands on the prologue, not mid-body. (Do not confuse these with the *internal* per-symbol frame IDA also reports — e.g. `0x60ed60` for `writePackageFile` — which is the `addToBom`-style second VA frame, ~`0xa0` lower; the entry addresses cited here are the ones to verify against.)

---

## 3. The Write Path

### Purpose

`writeArchiveFile` is where bytes hit disk. It configures one libarchive write-archive object as a PAX tar, optionally adds a gzip filter, then loops the BOM and streams each member. The determinism is engineered here, not incidental.

### Algorithm

```c
// NeffFileWriter::writeArchiveFile(void *hdr, boost::md5 &sig, bool compress)  @ 0x153df90
// compress = (uncompressed_flag ^ 1)  — gzip is ON by default.
function writeArchiveFile(hdr, sig, compress):
    a = archive_write_new()                       // @0x153dfb0  → archive_write_new@plt 0x61daf0
    archive_write_set_format_pax(a)               // @0x153dfbd  → set_format_pax@plt   0x5f68c0
                                                  //   the ONLY format selector; no ustar/gnutar/v7/zip
    if compress:
        archive_write_add_filter_gzip(a)          // @0x153f134  → add_filter_gzip@plt  0x622270
        archive_write_set_filter_option(a, "gzip", "compression-level", "1")  // @0x153f14d
        archive_write_set_filter_option(a, "gzip", "timestamp",         "false") // @0x153f166
    archive_write_open(a, sink, open_cb, write_cb, close_cb)  // @0x153dfe7 → 0x5f1750
                                                  //   custom std::function sink (in-memory/buffered),
                                                  //   NOT archive_write_open_filename — bytes are
                                                  //   handed to computeOutputPath afterwards.

    for entry in BOM(std::map @ writer+144, KEY ORDER):   // sorted by absolute on-disk path
        if stat(entry.path, &st) != 0:            // @0x153e05a → stat@plt 0x5eb270
            NeuronAssert("statResult == 0")       // neff_file_writer.cpp:310
        ae = archive_entry_new()                  // @0x153e067 → 0x61e3d0
        archive_entry_copy_stat(ae, &st)          // @0x153e07a → 0x62ca10
        archive_entry_set_pathname(ae, entry.member_name)  // @0x153e08b → 0x625ec0
        // ----- DETERMINISTIC PACKING (the byte-reproducibility guarantee) -----
        archive_entry_set_uname(ae, "nobody")     // @0x153e09a → 0x5f4960   ("nobody" rodata literal)
        archive_entry_set_gname(ae, "nobody")     // @0x153e0a9 → 0x5f0d90
        archive_entry_set_mtime(ae, 0, 0)         // @0x153e0b5 → 0x6052b0
        archive_entry_set_ctime(ae, 0, 0)         // @0x153e0c1 → 0x606710
        archive_entry_set_atime(ae, 0, 0)         // @0x153e0cd → 0x5efc00
        // ----------------------------------------------------------------------
        fp = fopen(entry.path, "rb")              // else NeuronAssert("fp")  (cpp:322)
                                                  //   + "writeKelp missing file <path>"
        archive_write_header(a, ae)               // @0x153e0fa → 0x615b30
        while (n = fread(buf, 1, 0x2000, fp)):    // 8 KiB streaming chunks
            if entry.member_name in IR-sig set @ writer+216:   // §5 G1
                sig.update(buf, n)                // "Adding <n> bytes of <file> to the IR Signature."
            archive_write_data(a, buf, n)         // @0x153e2b9 → 0x6135c0
        fclose(fp); archive_entry_free(ae)
    archive_write_close(a); archive_write_free(a)
```

### Determinism — why a NEFF is byte-reproducible

Every nondeterministic field a tar normally carries is pinned to a constant:

| Field | Value | Mechanism | Confidence |
|---|---|---|---|
| owner / group name | `"nobody"` | `archive_entry_set_uname`/`set_gname` @ `0x153e09a`/`0x153e0a9` | CONFIRMED |
| mtime / ctime / atime | `0` | `archive_entry_set_mtime`/`ctime`/`atime` @ `0x153e0b5`–`0x153e0cd` | CONFIRMED |
| gzip mtime stamp | suppressed | `set_filter_option(gzip, "timestamp", "false")` @ `0x153f166` | CONFIRMED |
| gzip level | `1` | `set_filter_option(gzip, "compression-level", "1")` @ `0x153f14d` | CONFIRMED |
| member order | path-sorted | BOM is a `std::map`, iterated in key order — not insertion order | CONFIRMED |

> **QUIRK —** member order is **`std::map` key order over absolute on-disk source paths**, *not* `addToBom` call order. A reimplementer who tars members in insertion order produces a different byte stream and breaks the IR-signature comparison. The BOM key is the source path; the value is the in-tar member name. (The two are usually different — e.g. key `…/nc0/sg00/sgLnk/PE.bin`, member name `PE.bin`.)

### Member alignment

libarchive's PAX writer pads each member's data to a **512-byte tar record** boundary, and uses a 10240-byte default blocking factor. `writeArchiveFile` imposes no extra alignment: `nm -D libwalrus.so | rg 'set_bytes_per_block'` returns **zero**, so no override is present and the libarchive defaults stand. The 512-byte record is the only on-NEFF alignment of a member; the *logical* in-DRAM tensor alignment (`const_ap_offset`) is a separate concern computed by `getAlignmentSize` @ `0x1091890`, unrelated to the tar padding. [CONFIRMED]

---

## 4. The Member Layout

### Purpose

"Sections" in a NEFF are tar members. There is no section header table to enumerate them; the member directory *is* the BOM, and a consumer discovers members by extracting the tar (or by opening a known name in an already-extracted directory). The roster below is the member set, with each entry's format and whether its bytes feed the MD5 IR signature.

### Member roster

`Sig?` = member bytes feed the MD5 IR signature (the artifact subset). `Per` = `T` top-level / `C` per-core (under `nc<core>/sg<subgraph>/sgLnk/`).

| Member name | Content | Format | Sig? | Per |
|---|---|---|---|---|
| `info.json` | NEFF header oracle: version major/minor, engine-present bool array, output filename, arch | JSON | Y | T |
| `neff.json` | subgraph table v0.5: node `main`; per-core `sg_coreV1<i>` op `__kelf` → `kelf-<N>.json` | JSON v0.5 | n | T |
| `def.json` | per-core definition v0.6: engine→{`_instr`,`_dbg`,`_asm_dbg`,dma}; var table; DMA queues; CCInfo; FP8; features[] | JSON v0.6 | n | C |
| `tensor_map.json` | per-core unified tensor map (IO binding) | JSON | n | C |
| `PE.bin` `Pool.bin` `Activation.bin` `SP.bin` `DVE.bin` | per-engine ISA instruction streams (0x40-byte bundles, program order; DMA descriptors folded in) | raw bundles | **n** | C |
| `PE.json` `Pool.json` `Activation.json` `SP.json` `DVE.json` | per-engine DMA-descriptor table (`{"dma":[{queue, desc:[{from,to,…}]}]}`) | JSON | n | C |
| `kelf-<N>.json` | per-core KELF index: engine `.bin` filenames + offsets | JSON | n | C |
| `ucode_lib.json` | custom-op micro-code lib (`[{name,opcode,sub_opcode}]`) | JSON | n | C |
| `<const>.bin` / `<w>.npy` | constant/weight backing data, de-dup'd | raw bytes | **n** / *.npy only* | C |
| `debug_info_backend.<eng>.dbg` / `debug_info_asm.<eng>.dbg` | BIR-/ASM-layer `ir_debug_info` protobuf | protobuf | **Y** | C |
| `kernel_debug_info.json` | extended kernel debug info | JSON | n | T |
| `global_metric_store.json` / `hlo_metrics.json` / `metrics.json` / `icMetadata.json` / `loop.json` | metric / autotuner / loop-spec surfaces | JSON | n | T/C |
| `cpu.so` / `cpu.params` | custom-op **host** CPU kernels (if present) | ELF `.so` / bytes | n | T |

> **QUIRK —** `cpu.so` is a *real* ELF shared object — but it is a custom-op host kernel carried **verbatim as a tar member**. It is not the NEFF container and has nothing to do with the (non-existent) NEFF ELF header. A reimplementer scanning for an ELF magic inside a NEFF will find `7f454c46` only at the start of this member's bytes, never at the NEFF's offset 0.

### Engine-name duality

The single most error-prone point. Two spellings of each engine live at different layers:

- **On-disk `.bin`/`.json` basenames** use `bir::EngineInfo2string` = TitleCase: `PE`, `Pool`, `Activation`, `SP`, `DVE`. The shipped analyzer opens exactly these ([§7](#7-the-consumer-proof)).
- **`def.json` `"definition"` key tokens** use the NEFF-local formatter `sub_15248C0` (a standalone engine→string function, called by `writeDefJson`/`writeDMAQueueDefinitions`) = lowercase: `1→"pool"`, `2→"act"`, `3→"pe"`, `4→"dma"`, `5→"dve"`, `6→"sp"`, suffixed `_instr`/`_dbg`/`_asm_dbg`.

So `def.json["definition"]["pe"]["_instr"] == "PE.bin"`; the BOM maps the on-disk `…/PE.bin` path → the in-tar member name. `dma`(4) is a `def.json` token but has **no** `.bin` of its own — DMA descriptors fold into the issuing compute engine's stream. [CONFIRMED — analyzer confirms the TitleCase set; the lowercase map is byte-proven from the jump table in `sub_15248C0` (see CORRECTION below).]

> **CORRECTION (M1 — formatter grounding) —** an earlier note tied this map to `sub_15248C0` as if it were a block of standalone `\0pool\0`/`\0act\0`/`\0dve\0` string literals, and a separate survey misread `sub_15248C0` as sitting *inside* `NeffPackager::findRemoteSBVars`. Both are corrected here. (a) `sub_15248C0` (range `0x15248c0–0x1524d03`) is its **own** function, adjacent to but not part of `findRemoteSBVars` (`0x15244d0–0x1524884`). (b) The six tokens are **not** standalone strings — none of `pool`/`act`/`pe`/`dma`/`dve`/`sp` exists as a `\0token\0` literal in `libwalrus.so`. The map is instead produced by a **switch jump table** at `0x15248f1`: each case loads a token via a substring `lea` into a longer string — case 1 `"pool"`, case 2 `"act"` (offset into a longer name), case 3 `"pe"` (= `"Bad Shape"+7`), case 4 `"dma"` (tail of `enableRemoteSemaphoreDMA`), case 5 `"dve"` (`SyncPoolDve+0xa`), case 6 `"sp"` (= `"Ssp"+1`). The `{1→pool … 6→sp}` mapping is therefore **byte-proven from the jump table**, not from token literals. `[CONFIRMED — jump table @ 0x15248f1 with per-case substring leas resolved.]`

### Per-core directory tree

Members come from a per-core build directory; the paths become the BOM keys.

```text
<build-dir>/
  nc<core>/                      # one per virtual NeuronCore (nc0/nc00/nc01/…)
    sg<subgraph>/                # sg00 …
      sgLnk/                     # bir_linker symlink dir; analyzer --prefix points HERE
        PE.bin Pool.bin SP.bin DVE.bin Activation.bin
        PE.json Pool.json SP.json DVE.json Activation.json
        def.json  tensor_map.json
        debug_info_backend.<eng>.dbg  debug_info_asm.<eng>.dbg
        kelf-0.json  ucode_lib.json  icMetadata.json  <const>.bin
  neff.json  info.json           # top-level members
  global_metric_store.json  hlo_metrics.json  metrics.json  loop.json
  kernel_debug_info.json  cpu.so  cpu.params
```

`NeffPackager::run` asserts `modules.size() == options.vnc_nc_count` (`neff_packager.cpp:113`): one `bir::Module` per virtual NeuronCore → one `sgLnk` subtree per core, indexed by `neff.json`'s `sg_coreV1<i>`. The per-member JSON sidecars and the header POD are covered in **12.2 (NEFF header + BOM writer)** and **12.3 (NEFF JSON sidecars)**.

---

## 5. The Two BOMs

There are two unrelated things called "BOM" in this codebase, and conflating them is what produced the original "ELF magic" misreading.

1. **The NEFF BOM** — `NeffFileWriter`'s `std::map<boost::filesystem::path, std::string>` @ `writer+144`. Key = absolute on-disk source path; value = in-tar member name. Built one entry at a time by `addToBom` @ `0x153fb80`, iterated in key order by `writeArchiveFile` to drive the tar. **This is the "section index" of a NEFF** — an in-memory manifest, not an on-disk record, and not an ELF section header table. Dumped at LogLevel 10 between the literals `The NeffPacakger BOM:` … per-entry … `End of NeffPacakger BOM` (the `Pacakger` typo is in the binary). [CONFIRMED — both literals present]
2. **The "0xEF 0xBB 0xBF" BOM** — the nlohmann::json **UTF-8 byte-order-mark**. The string `"invalid BOM; must be 0xEF 0xBB 0xBF if given"` is the JSON lexer rejecting a malformed BOM at the head of a *sidecar JSON member*. It has nothing to do with the NEFF container or with the manifest above. [CONFIRMED — live string]

The IR-signature subset (`writer+216`) is a third set: the BOM entries whose bytes feed the MD5.

> **CORRECTION (Part-12 reconcile) —** the membership predicate is **byte-proven**, not opaque. 12.4 ([Per-Engine `.bin`](per-engine-bin.md)) and 12.2 ([NEFF header + BOM writer](neff-header-bom-writer.md)) resolve it directly in `addToBom`: a member enters the IR-sig set iff its on-disk basename **equals `".npy"`** (`string::compare==0` @ `0x153fc6f`) **or contains `".dbg"`** (`string::find` @ `0x153fc9b`), plus the constructor pre-seed `"info.json"`. So the signed set is exactly `{info.json} ∪ {*.dbg} ∪ {*.npy}` — the per-engine `.bin` instruction streams and the `<const>.bin` blobs are **not** signed (the roster above is corrected to match). The signature certifies debug-info + weights + header, not the program code.

---

## 6. The Header POD and the UUID CSPRNG

### The `neff_header` POD

`initializeNeffHeader` @ `0x1540a00` builds a flat POD (≥556 B) on the stack. It is consumed by `writeArchiveFile` as metadata and serialized into `info.json` — **it is never emitted as a leading binary record.** Selected fields (full layout in 12.2):

| Offset | Field | Value / source | Confidence |
|---|---|---|---|
| `+0` | NEFF format version | `2` (rodata OWORD; `0200000000000000`) | CONFIRMED |
| `+8` | region / header size | `0x400` (1024) | CONFIRMED |
| `+192` | feature flags | 64-bit mask (`writeNEFFFeatures` @ `0x15294b0`) | CONFIRMED |
| `+204` | UUID | 16-byte RFC-4122 **v4** | CONFIRMED |
| `+220` | filename | `"file.neff"` (override path) or `info.json` value | CONFIRMED |
| `+480` | engine-present bitmap | one byte/engine slot, from `info.json` bool array | CONFIRMED |

The override branch (`internalOverride`, the single-output `file.neff` fast path) sets filename `file.neff`, major=1, minor=1, zeroes the engine array, and skips `info.json`. The `"file.neff"` literal is emitted as `movabs 0x66656e2e656c6966` (`"file.nef"`) + `'f'`. [CONFIRMED]

> **NOTE —** the `def.json` schema string `"0.6"` and the `neff.json` schema string `"0.5"` are *JSON-member* schema versions, distinct from the binary `neff_header` version `2`. Three independent version numbers; do not collapse them.

### The UUID — ChaCha20 seeded from `expand 32-byte k`

The UUID at `neff_header+204` is RFC-4122 version 4 (random). Its randomness comes from a ChaCha20 keystream whose constant block is the canonical seed string `"expand 32-byte k"` — present verbatim in `.rodata`.

```c
// UUID generation (RFC-4122 v4 via ChaCha20 CSPRNG)
// seed constant "expand 32-byte k" @ .rodata fileoff 0x1dd7910 (cp310, the frame this page
//   is pinned to; the cp312 wheel places the same string at 0x1dd5ed0 — a per-wheel rodata delta).
// ".rodata VMA == file-offset", so the VMA equals the file-offset.
function fill_uuid(uuid[16]):
    chacha20_block(state)                 // state[0..3] = "expand 32-byte k" (the 16-byte sigma)
    copy 16 keystream bytes into uuid
    uuid[6] = (uuid[6] & 0x0F) | 0x40     // version nibble  → 4
    uuid[8] = (uuid[8] & 0x3F) | 0x80     // variant bits    → 10xx (RFC-4122)
```

The `"expand 32-byte k"` constant is the ChaCha/Salsa20 sigma — its presence is a binary-confirmed fingerprint of a ChaCha20 keystream generator feeding the UUID. The masking (`&0x0F|0x40`, `&0x3F|0x80`) is the standard RFC-4122-v4 version/variant stamp. [CONFIRMED — string present; STRONG on the ChaCha20→UUID wiring, which is inferred from the sigma constant co-located with the header builder]

---

## 7. The Consumer Proof

### Purpose

The decisive, source-independent confirmation that there is no ELF/NEFF-magic container: the shipped consumer parses no container header at all. `analyze_neff_artifacts.py` (md5 `125d8537cdc2d034ba1d171cd82d0138`, byte-identical across cp310/311/312) is run on a **directory** of already-extracted members and opens them **by name**.

### What it imports — and what it does not

```text
imports:  re  json  operator  functools.reduce  argparse  os  sys  math
absent :  struct      (0)   →  no binary unpacking
          tarfile     (0)   →  it does not even read the tar; it reads extracted files
          .seek(      (0)   →  no offset arithmetic
          magic       (0)   →  no magic check
          e_ident/e_shoff (0) → no ELF parsing
```

### How it reads members

```python
# analyzeDMA(prefix)  — lines 22-41: open the five per-engine DMA sidecars BY NAME
poolFileName = "{}/Pool.json".format(prefix)            # line 24
# … SP.json, DVE.json, PE.json, Activation.json …
for fname in [poolFileName, spFileName, dveFileName, peFileName, actFileName]:
    f = open(fname)                                     # line 32 — open by NAME
    data = json.load(f)                                 # line 37 — parse JSON, no struct

# analyzeBins(prefix)  — line 74-75: stat the five ISA streams BY NAME
for engine in ['Pool', 'SP', 'DVE', 'PE', 'Activation']:
    file_size = os.stat(f'{prefix}/{engine}.bin').st_size   # line 75 — os.stat, no read/seek
```

`analyzeTmpBuf` likewise `json.load`s `def.json` and walks `data['var']` / `data['dma']`. There is **no** magic check, **no** version check, **no** `struct`/`seek`/`unpack` anywhere in the 502-line file. A consumer that needed an ELF would read `e_shoff`/`sh_offset`; this one `open()`s files. That is the behavioural signature of a tar/directory, and it is the cleanest proof on the page that a NEFF is not an object file. [CONFIRMED — full read, md5-pinned]

---

## 8. Reproducing Pack and Unpack

### Pack (reimplementation skeleton)

```c
// Equivalent of NeffFileWriter::writeArchiveFile, standalone.
struct archive *a = archive_write_new();
archive_write_set_format_pax(a);                       // PAX, the only format
if (compress) {
    archive_write_add_filter_gzip(a);
    archive_write_set_filter_option(a, "gzip", "compression-level", "1");
    archive_write_set_filter_option(a, "gzip", "timestamp", "false");   // determinism
}
archive_write_open_filename(a, "model.neff");          // (walrus uses a custom sink)

// members in std::map key order over absolute source paths:
for (const auto &[src_path, member_name] : bom) {      // KEY ORDER, not insertion
    struct stat st; stat(src_path.c_str(), &st);
    struct archive_entry *ae = archive_entry_new();
    archive_entry_copy_stat(ae, &st);
    archive_entry_set_pathname(ae, member_name.c_str());
    archive_entry_set_uname(ae, "nobody");  archive_entry_set_gname(ae, "nobody");
    archive_entry_set_mtime(ae, 0, 0);  archive_entry_set_ctime(ae, 0, 0);
    archive_entry_set_atime(ae, 0, 0);                 // all zeroed
    archive_write_header(a, ae);
    FILE *fp = fopen(src_path.c_str(), "rb");
    char buf[0x2000]; size_t n;
    while ((n = fread(buf, 1, sizeof buf, fp)))         // 8 KiB chunks
        archive_write_data(a, buf, n);
    fclose(fp); archive_entry_free(ae);
}
archive_write_close(a); archive_write_free(a);
```

### Unpack (verification recipe — no sample NEFF ships in the wheel)

```bash
file model.neff                         # → "gzip compressed data" (default) or "POSIX tar archive"
gunzip -c model.neff | tar tvf -        # (gzip default); else: tar tf model.neff
#   expect: info.json neff.json + nc*/sg*/{PE,Pool,Activation,SP,DVE}.{bin,json}
#           def.json tensor_map.json kelf-0.json debug_info_*.dbg ucode_lib.json …
tar xf model.neff -C /tmp/neff
python3 analyze_neff_artifacts.py -p /tmp/neff/nc0/sg00    # the shipped analyzer
jq '.version, .name' /tmp/neff/info.json                   # header major/minor + filename
jq 'keys' /tmp/neff/nc0/sg00/def.json                      # "definition","var","0.6",features…
xxd model.neff | head                   # leading = gzip 1f 8b, OR (uncompressed) ustar @0x101;
                                        # NEVER ELF 7f454c46, NEVER a "NEFF" tag at offset 0.
```

> **NOTE —** no `*.neff` sample ships in any wheel (`fd -e neff` = 0 across all extracted trees) — a NEFF is *compiler output*, not a wheel asset. The recipe above is for a NEFF produced by a real compilation; the producer/consumer behaviour it relies on is fully grounded in the binary.

---

## 9. Re-Verification Ceiling

What this page can and cannot stand behind, honestly:

- **CONFIRMED, this session, on the cp310 binary** (the frame this page is pinned to): the full libarchive PAX+gzip API (20 `archive_*` symbols); zero ELF-writer symbols (the 15 substring hits are all `StaticOffset` DMA false positives); the `archive_write_new`/`set_format_pax`/`open` setup sequence and the per-member `set_uname`/`gname`/`mtime`/`ctime`/`atime` + `add_filter_gzip` + two `set_filter_option` call sites at the cited addresses; the `"nobody"`, `"expand 32-byte k"`, `"file.neff archive"`, `"The NeffPacakger BOM:"`, and `"invalid BOM; must be 0xEF 0xBB 0xBF if given"` strings; the absence of any `set_bytes_per_block` override; the analyzer's import set and by-name member opens (md5-pinned).
- **CONFIRMED (byte-proven elsewhere in Part 12)**: the IR-signature member-subset predicate (`writer+216`) — resolved on the `addToBom` disasm by 12.2/12.4 as `{info.json} ∪ {*.dbg} ∪ {*.npy}` (`.npy`-equals @ `0x153fc6f`, `.dbg`-contains @ `0x153fc9b`); the `.bin` streams are excluded. **STRONG, not byte-proven**: the ChaCha20-keystream→UUID wiring (the sigma constant is confirmed and co-located with the header builder, but the keystream-to-`uuid[16]` copy was not single-stepped).
- **INFERRED / deferred**: the exact `info.json` key names feeding header fields `+168`/`+476`/`+480` (nlohmann `operator[]` inlined — deferred to 12.2/12.3); the gzip member byte `1f 8b` and the tar `ustar`@`0x101` layout are RFC/POSIX spec facts, not literals stored in the binary (the gzip *filter* is binary-confirmed).
- **CORRECTION (frame banner) — addresses are the cp310 frame.** An earlier revision of this page declared a cp312 banner while citing cp310 body addresses throughout (`addToBom@0x153fb80`, the `.npy`/`.dbg` predicate at `0x153fc6f`/`0x153fc9b`, the method table). That was self-contradictory: the cp310/cp312 wheels **share** `.text`/`.rodata` bases but their function *bodies* differ by a per-wheel delta (~`0xa0`), so a cp312 banner over cp310 body addresses cannot be objdump-verified on either binary. The banner is now pinned to **cp310**, matching the cited disassembly and the seven sibling Part-12 pages. The one stray cp312 datum — the `expand 32-byte k` seed at `0x1dd5ed0` — has been corrected to the cp310 address `0x1dd7910` (the cp312 wheel keeps it at `0x1dd5ed0`). The `@plt` thunk addresses form a third VA frame distinct from the `.dynsym` `U` entries and the function-body addresses.

---

## Related Components

| Name | Relationship |
|---|---|
| [12.2 — NEFF header + BOM writer](neff-header-bom-writer.md) | The `neff_header` POD layout and `addToBom`/BOM mechanics in full |
| [12.3 — NEFF JSON sidecars](neff-json-sidecars.md) | `info.json`/`neff.json`/`def.json`/`tensor_map.json` schemas |
| `NeffPackager` driver | Emits the JSON members and assembles the BOM ([§2](#2-producer-topology)) |

## Cross-References

- [Perf-Sim Wiring](../walrus/perf-sim-wiring.md) — `PerfSimPackagePass::createTarball` @ `0x1670ed0` builds `file.perf-sim` with the **same** libarchive PAX+gzip API (`archive_write_set_format_pax` + `archive_write_add_filter_gzip`); that tarball is later appended into the NEFF as the `debug_info_pttf` member.
