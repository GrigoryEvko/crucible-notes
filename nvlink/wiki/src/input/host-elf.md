# Host ELF Embedding

When a CUDA application is compiled with `nvcc`, the host toolchain (gcc, clang, or MSVC) produces `.o`, `.a`, or executable files whose sections contain **both** host CPU code and embedded CUDA device code. Device binaries are deposited into three well-known ELF sections (`.nvFatBinSegment`, `__nv_relfatbin`, `.nv_fatbin`) and an auxiliary module-id table (`__nv_module_id`) so that the CUDA runtime can locate them at program startup via the `__cudaRegisterFatBinary` / `DEFINE_REGISTER_FUNC` glue.

nvlink normally processes only device ELFs (`e_machine == EM_CUDA == 190`). The **host ELF embedding path** is the escape hatch that lets it also accept host relocatable objects: it loads the host ELF, walks its section table for the three known fatbin section names, extracts the `0xBA55ED50` fatbin wrapper from `__nv_relfatbin`, and re-enters the standard fatbin extraction pipeline as if the file had been loaded directly as a `.fatbin`. The host CPU code itself is ignored entirely — only the embedded device payload is consumed.

| | |
|---|---|
| **Host ELF loader (thunk)** | `sub_476E80` at `0x476E80` (7 B, jumps to `sub_43DFC0`) |
| **Host ELF loader (impl)** | `sub_43DFC0` at `0x43DFC0` (344 B) |
| **Host ELF free thunk** | `sub_476EA0` at `0x476EA0` (7 B, jumps to `sub_43D990` -> `arena_free`) |
| **Host ELF type classifier** | `sub_43D9B0` at `0x43D9B0` (42 B) — `e_type == ET_REL` test |
| **ELF magic predicate** | `sub_43D970` at `0x43D970` (19 B) — `0x464C457F` test |
| **ELF class predicate** | `sub_43D9A0` at `0x43D9A0` (18 B) — `EI_CLASS == 2` test |
| **Structural validator** | `sub_43DD30` at `0x43DD30` (536 B) — section bounds sweep |
| **Fatbin section scanner** | `sub_476D90` at `0x476D90` (240 B) |
| **Section-exists predicate** | `sub_476EC0` at `0x476EC0` (71 B) |
| **Section data accessor** | `sub_476F10` at `0x476F10` (79 B) |
| **Section size accessor** | `sub_476F60` at `0x476F60` (79 B) |
| **Elf64 section finder** | `sub_4483B0` at `0x4483B0` (486 B) |
| **Elf32 section finder** | `sub_46B5D0` at `0x46B5D0` (454 B) |
| **Module-id section loader** | `sub_46F0C0` at `0x46F0C0` (186 B) — finds `__nv_module_id` |
| **Module-id parser** | `sub_4298C0` at `0x4298C0` (476 B) — `def <name>\0` entries |
| **Input type reporter** | `sub_4297B0` at `0x4297B0` (263 B) — emits "no device code in" |
| **Trigger** | Input file is ELF with `e_type == ET_REL` and `e_machine != EM_CUDA` |
| **Output** | Fatbin data buffer passed to `sub_42AF40` (fatbin extraction) |

## Why This Path Exists

In the legacy CUDA Runtime API compilation model, `nvcc` splits each `.cu` source into a device side (compiled to fatbin) and a host side (compiled to a host object). Rather than producing two separate artifacts, `nvcc` instructs the CUDA frontend (`cudafe++`) to generate a C++ wrapper that **embeds** the fatbin as a byte array inside the host object, and generates a constructor-registration shim (`__cuda_register_globals`) that passes that byte array to `__cudaRegisterFatBinary` at program load.

The CUDA toolchain emits this byte array into a dedicated ELF section — historically `.nvFatBinSegment`, for whole-program fatbins; `__nv_relfatbin`, for relocatable link-time fatbins (`nvcc -dlink`); or `.nv_fatbin`, for alternate embedding modes. When the user later invokes `nvlink` on the host object directly (for example, to re-link device code from a pre-compiled `.o`), nvlink has to recognise that it is looking at a **host** relocatable, reach inside it, and pull the device payload back out. That is what this page documents.

The path is **not** used for normal device-link flows where nvlink receives cubins or fatbins directly from ptxas/cudafe; those hit the cubin/fatbin branches of `main()` long before the host ELF fallback.

## Trigger Conditions in main()

The host ELF path sits at the bottom of the input file classification cascade in `main()`. The dispatch evaluates, in order:

```text
For each input file:
  1. Read a 56-byte header probe from the start of the file
  2. If extension == ".a"                                     -> archive handler
  3. If *(uint32_t*)probe == 0xBA55ED50                       -> fatbin handler
  4. If *(uint32_t*)probe == 0x464C457F  (ELF magic):
       a. If extension == ".o" AND e_machine == 190 (EM_CUDA) -> cubin handler
       b. If extension == ".so"                               -> skip (shared lib)
       c. If is_host_elf(probe) (i.e., e_type == ET_REL)      -> HOST ELF EMBEDDING  (this page)
  5. Try PTX / NVVM IR / LTO IR / bitcode handlers
```

The host ELF arm is reached only for files that carry the ELF magic, are relocatable (`e_type == ET_REL`), and are **not** already identified as CUDA device ELFs by the class+machine check. In practice this means one of:

- An x86-64 host `.o` produced by gcc/clang from a `.cu` that contains embedded device fatbins;
- An x86-64 host relocatable from `nvcc --device-link` with `-dlink` output that embedded `__nv_relfatbin`;
- A host `.a` member (unwrapped elsewhere) that contains the same.

### Decompiled main() Branch

Reconstructed from the input-loop section of `main()`:

```c
// main() input-loop body -- after fatbin magic check has already failed.
// s1         = file extension ("o", "so", "a", "", ...)
// ptr        = 56-byte header probe read from the file
// v74        = full filename (argv[i])
// current_module, cubin_list, cubin_count = per-input output state
v192 = is_archive_magic(ptr, 56);                         // sub_487A90
if (!v192) {                                              // not an archive
    // Skip shared libraries -- they cannot contain relocatable device code
    if (*s1 != 's' || s1[1] != 'o' || s1[2] != 0) {
        v193 = is_host_elf(ptr);                          // sub_43D9B0: e_type == ET_REL
        if (v193) {
            // Guard: an "o" file that is *also* a CUDA device ELF must take
            // the cubin path, not this one. The sub_43D970/sub_43D9A0 chain
            // and e_machine byte at offset 0x12 disambiguate.
            if (s1[0] != 'o' || s1[1] != 0
                || !is_elf(ptr)                           // sub_43D970: magic test
                || *(uint16_t *)(get_ehdr(ptr) + 0x12) != 190) {

                // --- Host ELF fallback ---
                arena_free(s1);                            // sub_431000: release extension copy

                host_elf_buf = load_host_elf(v74);         // sub_476E80 -> sub_43DFC0
                if (fatbin_runtime_unavailable())          // sub_4BDB70: !sub_D9A2C0()
                    fatal("libnvfatbin missing");          // (elided)

                fatbin_data = search_fatbin_sections(      // sub_476D90
                                  host_elf_buf, v74);

                report_input_type(fatbin_data, v74);       // sub_4297B0
                                                            // emits "no device code in <file>"
                                                            // when fatbin_data == NULL

                if (fatbin_data) {
                    // Extracted payload -> feed the standard fatbin pipeline.
                    fatbin_extract(fatbin_data, host_elf_buf, v74,
                                   current_module, 0, 0, 0,
                                   &cubin_list, &cubin_count);   // sub_42AF40
                } else if (register_link_binaries_path && host_elf_buf) {
                    // No fatbin -- but user passed --register-link-binaries,
                    // so scavenge module-id table instead.
                    extract_module_ids(host_elf_buf, v74,
                                       &module_list);             // sub_4298C0
                }

                byte_2A5F212 = 1;                          // remember that a host object
                                                            // was seen -- affects final
                                                            // output phase.

                arena_free(host_elf_buf);                  // sub_476EA0
            }
            // else: cubin path already handled this file above
        }
        // else: not a relocatable ELF -- continue to PTX/NVVM/IR probes
    }
}
```

The extension comparisons are open-coded single-byte loads: `*s1 == 's' && s1[1] == 'o'` tests for `.so`, `s1[0] == 'o' && s1[1] == 0` tests for `.o`. The byte at offset `0x12` is the Elf64 `e_machine` field (offset 18 from the ELF header base), read as a little-endian 16-bit word.

## Host ELF Detection: x86-64 Host vs Pure Device ELF

nvlink relies on three independent byte tests to separate a host relocatable from a device cubin, all implemented in 18–42-byte predicate functions so they can be inlined cheaply at the `main()` dispatch:

| Predicate | Function | Byte offset | Test | Meaning |
|---|---|---|---|---|
| ELF magic | `sub_43D970` | `0x00` | `*(uint32_t*)buf == 0x464C457F` | File has `"\x7fELF"` signature |
| ELF class | `sub_43D9A0` | `0x04` | `buf[4] == 2` | Elf64 (otherwise Elf32) |
| Relocatable | `sub_43D9B0` | `0x10` | `*(uint16_t*)(ehdr + 0x10) == 1` | `e_type == ET_REL` |
| CUDA device | `main()` inline | `0x12` | `*(uint16_t*)(ehdr + 0x12) == 190` | `e_machine == EM_CUDA (0xBE)` |

`sub_43D9B0` is the key classifier. It is not simply `*(uint16_t*)(buf + 0x10)` — it first dispatches on the class byte so the read lands at the right offset for Elf32 vs Elf64 (both happen to be 0x10 for `e_type`, but the function is written defensively):

```c
// sub_43D9B0 -- is_host_elf(buf)
bool is_host_elf(void *buf) {
    if (!buf) return false;
    if (buf[4] == 2)                                // ELFCLASS64
        return *(uint16_t *)(sub_448360(buf) + 16) == 1;   // e_type == ET_REL
    else                                            // ELFCLASS32 (or invalid)
        return *(uint16_t *)(sub_46B590(buf) + 16) == 1;
}
```

Both `sub_448360` (Elf64) and `sub_46B590` (Elf32) are identity accessors returning the buffer pointer unchanged — they exist so that future class-specific ELF header adjustments (byte-swap, offset skew) could be slotted in without touching every call site. Today they are pure no-ops.

### Decision Matrix

Combining the four tests, the dispatch routes as follows:

| ELF magic | `e_type` | `e_machine` | Extension | Handler |
|---|---|---|---|---|
| yes | any (`ET_REL`, `ET_EXEC`, `0xFF00`) | `190` (EM_CUDA) | `.o` | **cubin handler** (`sub_476BF0` path) — `e_type == ET_EXEC` inputs are accepted here and then rejected by `sub_426570` |
| yes | `ET_REL` (1) | not 190 | `.o` | **host ELF embedding** (this page) |
| yes | any | any | `.so` | skipped (shared library) |
| yes | `ET_EXEC` / `ET_DYN` | not 190 | any non-`.o` | falls through to PTX/NVVM probes (all fail) |
| yes | `ET_REL` (1) | any | no extension | **host ELF embedding** (this page) |
| no | — | — | — | PTX / NVVM / archive / fatbin |

The device-cubin vs host-ELF disambiguation hinges on the simultaneous `e_machine == 190` check. A host x86-64 object has `e_machine == 62` (`EM_X86_64`); a CUDA device ELF has the NVIDIA-assigned value `190`. nvlink never attempts to decode x86-64 relocations or process host code — it only cares about the embedded fatbin sections.

## Host ELF Loading: sub_43DFC0

`sub_43DFC0` reads the host ELF from disk and validates it structurally before handing the buffer to the section scanner. It is reachable via the thunk `sub_476E80`. Unlike the device-ELF loader `sub_476BF0`, which treats open/read failures as fatal, the host-ELF loader returns `NULL` silently — nvlink must be able to probe files that turn out not to be host ELFs without tearing down the whole run.

```c
// sub_43DFC0 -- load_host_elf(filename)
// Returns: arena-allocated buffer containing the ELF image, or 0 on failure.
uint64_t load_host_elf(const char *filename) {
    FILE *fp = fopen(filename, "rb");
    if (!fp) return 0;                             // silent failure

    if (fseek(fp, 0, SEEK_END) == -1)              // get size
        goto fail_close;

    int64_t size = ftell(fp);
    if (size == -1 || fseek(fp, 0, SEEK_SET) == -1 || size <= 52) {
        // size <= 52 rejects anything smaller than an Elf32 header (52 B).
        // An Elf64 header is 64 B, but 52 is the lower bound that lets us
        // reject obviously-too-small files for either class.
        fclose(fp);
        return 0;
    }

    void *tls = get_thread_arena(fp);              // sub_44F410
    void *arena = *(void **)(tls + 24);            // tls->current_arena
    void *buf = arena_alloc(arena, size);          // sub_4307C0
    if (!buf) {
        alloc_fail_handler(arena, size);           // sub_45CAC0 -- aborts
        fclose(fp);
        return 0;
    }

    size_t got = fread(buf, 1, size, fp);
    fclose(fp);

    if (got != size) goto fail_free;

    // Validate ELF magic & endianness (no byte-swapping supported).
    uint8_t *ehdr = (uint8_t *)sub_46B590(buf);    // identity accessor
    if (ehdr[5] != 1) goto fail_free;              // EI_DATA == ELFDATA2LSB
    if (*(uint32_t *)ehdr != 0x464C457F) goto fail_free;  // "\x7fELF"

    // Section/program header bounds sweep.
    if (!validate_elf_structure(buf, size))        // sub_43DD30
        goto fail_free;

    return (uint64_t)buf;

fail_free:
    arena_free(buf, size);                         // sub_431000
    return 0;

fail_close:
    fclose(fp);
    return 0;
}
```

### Validation Details

- **Minimum size 52 bytes.** The lower bound is the size of an Elf32 header (52 B). An Elf64 header is 64 B, but the check is deliberately permissive so both classes share one guard. Anything at or below 52 bytes is rejected before any byte dereference.

- **Little-endian only (`EI_DATA == 1`).** nvlink refuses big-endian files outright. This is safe: both x86 hosts and CUDA device code are little-endian.

- **Magic `0x464C457F`.** The 4-byte magic `"\x7fELF"` stored as a little-endian `uint32_t`. nvlink checks it after loading the whole file, not just the probe header, to guard against a race where the probe reads valid magic but the full file differs.

- **Structural sweep (`sub_43DD30`).** 536-byte validator that dispatches on the class byte and performs exhaustive bounds checking of every section (and program) header against the file size:

    - **Elf64 path:** requires `e_shentsize == 64`; if `e_phnum != 0`, requires `e_phentsize == 56`; then iterates `e_shnum` entries and confirms `sh_offset + sh_size <= file_size` for each.
    - **Elf32 path:** requires `e_shentsize == 40`; if `e_phnum != 0`, requires `e_phentsize == 32`; then iterates `e_shnum` entries with 32-bit offset/size fields.

    Sections exempt from the `sh_offset + sh_size` check (they have no file backing):

    | `sh_type` | Meaning |
    |---|---|
    | `8` (`SHT_NOBITS`) | `.bss`, uninitialized data |
    | `0x70000007` (`SHT_CUDA_GLOBAL`) | NVIDIA-specific, bitmask `0x400D` bit 0 |
    | `0x70000009` (`SHT_CUDA_LOCAL`) | NVIDIA-specific, bitmask bit 2 |
    | `0x7000000A` (`SHT_CUDA_SHARED`) | NVIDIA-specific, bitmask bit 3 |
    | `0x70000015` (`SHT_CUDA_SHARED_RESERVED`) | NVIDIA-specific, bitmask bit 14 |

    The validator packs the exemption set as `0x400DuLL >> (sh_type - 0x70000007)`, skipping the bounds check when the resulting low bit is 1.

- **Buffer/arena ownership.** The loaded buffer is allocated from the calling thread's current arena via `sub_44F410` -> `sub_4307C0`. It is the caller's responsibility to release it with `sub_476EA0` (which routes to `arena_free`) after the scanner is done.

## Fatbin Section Scanning: sub_476D90

Once the host ELF is loaded and validated, `sub_476D90` searches it for embedded fatbin data. It probes three section names in a fixed priority order but — crucially — only **one** of them actually supplies the data. The other two are presence indicators:

| Priority | Section name | Naming style | Role |
|---|---|---|---|
| 1 (gate) | `.nvFatBinSegment` | dotted, standard ELF | Must exist or the whole probe bails out |
| 2 (payload) | `__nv_relfatbin` | non-dotted, linker-symbol style | Actually extracted; contains `0xBA55ED50` wrapper |
| 3 (fallback gate) | `.nv_fatbin` | dotted, standard ELF | Alternate indicator if `__nv_relfatbin` is absent |

The fall-through logic: if `.nvFatBinSegment` is missing entirely, the search gives up. If `.nvFatBinSegment` is present but `__nv_relfatbin` is not, the function checks whether `.nv_fatbin` exists — if yes it still returns `NULL` (nothing to extract) but suppresses the error; if no it emits an error. Only when `__nv_relfatbin` is present does actual extraction happen.

```c
// sub_476D90 -- search_fatbin_sections(elf_buf, filename)
// Returns: arena-allocated copy of the fatbin payload, or NULL.
void *search_fatbin_sections(void *elf_buf, const char *filename) {
    if (!elf_buf) goto error_emit;

    // Gate 1: .nvFatBinSegment must exist
    if (!section_exists(elf_buf, ".nvFatBinSegment"))
        return NULL;                               // silent: no CUDA host object

    // Gate 2: __nv_relfatbin must exist
    if (!section_exists(elf_buf, "__nv_relfatbin")) {
        if (!section_exists(elf_buf, ".nv_fatbin"))
            goto error_emit;                       // error: malformed embedding
        return NULL;                               // .nv_fatbin present but not
                                                    // in the extractable form
    }

    // Extraction
    void *section = get_section_data(elf_buf, "__nv_relfatbin");  // sub_476F10
    if (!section) goto error_emit;

    // Validate the fatbin wrapper magic at the section start
    if (*(uint32_t *)section != 0xBA55ED50)        // -1168773808 signed
        goto error_emit;

    // Wrapper layout (see fatbin-extraction.md):
    //   [0..3]  = 0xBA55ED50          magic
    //   [4..5]  = version
    //   [6..7]  = header_size (=16)
    //   [8..15] = data_size
    int64_t copy_size = *(uint64_t *)(section + 8) + 16;

    // Arena-allocate a fresh buffer and copy the wrapper+payload.
    // The copy is necessary: the host ELF buffer is freed as soon as
    // main() returns from this branch, but the fatbin pipeline holds
    // onto the payload across phases.
    void *tls = sub_44F410(elf_buf, "__nv_relfatbin");
    void *arena = *((void **)tls + 3);             // tls->persistent_arena
    void *out = arena_alloc(arena, copy_size);     // sub_4307C0
    if (!out) alloc_fail_handler(...);             // sub_45CAC0
    memcpy(out, section, copy_size);
    return out;

error_emit:
    error_emit(dword_2A5BDB0, 30672788, filename); // sub_467460
    return NULL;
}
```

### Section Name Semantics

The three names correspond to different places in the CUDA compilation pipeline where a fatbin can be embedded:

- **`.nvFatBinSegment`**: The "whole-program" fatbin section. `nvcc` drops it into the host object when compiling a `.cu` file end-to-end. It wraps the fatbin wrapper with a small segment descriptor that the host-side `__cudaRegisterFatBinary` shim references. This section exists in virtually every CUDA-enabled host `.o`; its presence is what distinguishes "a host object produced by nvcc" from "an arbitrary x86-64 host object that happens to be ELF". nvlink uses it only as a gate — it does **not** actually parse the segment descriptor here (the CUDA runtime does, at program startup).

- **`__nv_relfatbin`**: The "relocatable fatbin" section created during device-link (`nvcc --device-link`, `nvcc -dlink`). This is where the bare `0xBA55ED50` wrapper lives, directly addressable as a section payload. The leading double-underscore and absence of a dot mark it as following linker-symbol naming rather than ELF-section naming — the section name also serves as a symbol that the generated `DEFINE_REGISTER_FUNC` code references. **This is the only section nvlink actually extracts from.**

- **`.nv_fatbin`**: An alternate embedding location used in certain compilation modes (for example, when the host object was compiled with a different `-rdc` setting or when a CUDA runtime version emits fatbins via a separate path). Its presence without `__nv_relfatbin` indicates that a different tool produced the embedding; nvlink does not extract from it directly but tolerates its presence.

### Magic Validation

After locating `__nv_relfatbin`, `sub_476D90` validates the 4-byte fatbin wrapper magic `0xBA55ED50` at offset 0 of the section data. The check is performed as a signed 32-bit comparison against `-1168773808` (which is `0xBA55ED50` re-interpreted as `int32_t`):

```c
if (*(int32_t *)section_data != -1168773808)      // 0xBA55ED50 signed
    goto error_emit;
```

This is the same magic the top-level `main()` dispatch uses when it receives a standalone `.fatbin` file, guaranteeing that the extracted section can re-enter the fatbin pipeline unmodified.

### Copy Sizing

The payload size is computed from the wrapper header: `data_size` at offset 8 gives the total size of the container data that follows the 16-byte header, so `copy_size = data_size + 16` covers the whole wrapper. The 16-byte header layout is the standard fatbin wrapper documented in [Fatbin Extraction](fatbin-extraction.md).

### Detection Flow Diagram

```text
                   [host .o file]
                         |
                         v
             +---------------------+
             | main() input loop   |
             | read 56-byte probe  |
             +---------------------+
                         |
            magic==ELF & e_type==ET_REL
            & e_machine!=EM_CUDA & ext!=".so"
                         |
                         v
             +---------------------+
             | load_host_elf       |  sub_43DFC0
             |  - fopen/fread      |
             |  - size > 52        |
             |  - ELFDATA2LSB      |
             |  - magic 0x464C457F |
             |  - validate headers |  sub_43DD30
             +---------------------+
                         | buf
                         v
             +---------------------+
             | search_fatbin_...   |  sub_476D90
             +----------+----------+
                        |
           probe ".nvFatBinSegment"  ---no--> return NULL (silent)
                        | yes
                        v
           probe "__nv_relfatbin"    ---no--> probe ".nv_fatbin"
                        | yes                    |
                        v                        +-yes-> return NULL (silent)
           get_section_data              +-no-> error_emit
                        |
                        v
           validate magic 0xBA55ED50
                        |
                        v
           copy_size = data_size + 16
                        |
                        v
           arena_alloc + memcpy
                        |
                        v  fatbin_data
             +---------------------+
             | fatbin_extract      |  sub_42AF40
             |  - wrapper parse    |
             |  - container iter   |
             |  - arch match       |
             |  - cubin emit       |
             +---------------------+
                        |
                        v
             cubins enter merge phase
```

## Section Lookup Dispatch: sub_476EC0 / sub_476F10 / sub_476F60

Three small dispatch functions wrap the Elf32/Elf64 section accessors behind a class-neutral API used by `sub_476D90` and other input-processing code:

```c
// sub_476EC0 -- section_exists(elf_buf, name)
bool section_exists(void *elf_buf, const char *name) {
    if (is_elf64(elf_buf))                         // sub_43D9A0
        return sub_4483B0(elf_buf, name) != NULL;  // Elf64 find
    else
        return sub_46B5D0(elf_buf, name) != NULL;  // Elf32 find
}

// sub_476F10 -- get_section_data(elf_buf, name)
void *get_section_data(void *elf_buf, const char *name) {
    if (is_elf64(elf_buf)) {
        Elf64_Shdr *s = sub_4483B0(elf_buf, name);
        return sub_448560(elf_buf, s);             // buf + s->sh_offset
    } else {
        Elf32_Shdr *s = sub_46B5D0(elf_buf, name);
        return sub_46B770(elf_buf, s);             // buf + s->sh_offset
    }
}

// sub_476F60 -- get_section_size(elf_buf, name)
uint64_t get_section_size(void *elf_buf, const char *name) {
    if (is_elf64(elf_buf)) {
        Elf64_Shdr *s = sub_4483B0(elf_buf, name);
        return sub_448580(elf_buf, s);             // s->sh_size (Elf64: offset 32)
    } else {
        Elf32_Shdr *s = sub_46B5D0(elf_buf, name);
        return sub_46B790(elf_buf, s);             // s->sh_size (Elf32: offset 20)
    }
}
```

The three-way split (exists / data pointer / size) exists because the scanner needs different subsets in different branches: `sub_476D90` only needs existence and data; `sub_46F0C0` (the module-id loader) needs both data and size; other call sites want just one.

### Elf64 Section Finder: sub_4483B0

`sub_4483B0` iterates the Elf64 section header table, resolving each entry's name from the section header string table (`SHT_STRTAB` at index `e_shstrndx`) and comparing it against the target with `strcmp`. Relevant Elf64 field offsets:

| Field | Offset (Ehdr) | Offset (Shdr) |
|---|---|---|
| `e_shoff` / `sh_offset` | 40 | 24 |
| `e_shentsize` | 58 | — |
| `e_shnum` | 60 | — |
| `e_shstrndx` | 62 | — |
| `sh_name` | — | 0 |
| `sh_type` | — | 4 |
| `sh_size` | — | 32 |

The function handles the `SHN_XINDEX` extension: if `e_shstrndx == 0xFFFF`, the real index lives in the `sh_link` field of section header entry 0. The data accessor `sub_448560` then returns `elf_buf + shdr->sh_offset` (18-byte body).

### Elf32 Section Finder: sub_46B5D0

Structurally identical to `sub_4483B0` but with Elf32 offsets:

| Field | Offset (Ehdr) | Offset (Shdr) |
|---|---|---|
| `e_shoff` / `sh_offset` | 32 | 16 |
| `e_shentsize` | 46 | — |
| `e_shnum` | 48 | — |
| `e_shstrndx` | 50 | — |
| `sh_size` | — | 20 |

The corresponding accessors `sub_46B770` / `sub_46B790` return the 32-bit offset and size fields.

## Re-Entry into the Fatbin Pipeline

Once `sub_476D90` hands back a non-NULL buffer, `main()` forwards it to `sub_42AF40`, the same fatbin extraction entry point that processes standalone `.fatbin` files:

```c
if (fatbin_data) {
    fatbin_extract(fatbin_data,       // arena copy of wrapper+payload
                   host_elf_buf,       // still live for back-references
                   filename,           // for diagnostics
                   current_module,     // per-input module handle
                   0, 0, 0,            // unused flags
                   &cubin_list,        // output: extracted cubins
                   &cubin_count);      // sub_42AF40
}
```

Inside `sub_42AF40` the extracted buffer is indistinguishable from a `.fatbin` loaded directly: wrapper parsing, container iteration, architecture matching, member extraction, and cubin delivery all follow the normal pipeline documented in [Fatbin Extraction](fatbin-extraction.md). The host ELF itself is not referenced again after the memcpy in `sub_476D90`, and its buffer is released by `sub_476EA0` (arena free) immediately after `fatbin_extract` returns — the lifetime of the host envelope is strictly shorter than that of the cubins extracted from it.

## CUID and Module-ID Handling

CUDA uses two different identifier mechanisms to bind device code to its host-side registration machinery:

| Identifier | Where produced | Where consumed | Does nvlink process it? |
|---|---|---|---|
| **`CUDA_STATIC_CUID`** (CUDA Unique ID) | `cudafe++` emits a per-TU 128-bit identifier into the host C++ wrapper | CUDA Runtime `__cudaRegisterFatBinary` / `__cudaRegisterLinkedBinary` at program startup | **No.** nvlink never references the string `CUDA_STATIC_CUID`; it does not appear anywhere in the v13.0 binary's string table. CUIDs are consumed by the **host** side only. |
| **`__nv_module_id`** (Module ID table) | `cudafe++` emits an ELF section of `def <name>\0` entries, one per device module | nvlink (`sub_46F0C0` -> `sub_4298C0`) when `--register-link-binaries` is passed | **Yes.** This is what the `extract_module_ids` fallback path loads when no fatbin is present. |

`nvlink_strings.json` confirms that CUID is **not** a nvlink concept: a full scan for `CUID`, `cuid`, or `CUDA_STATIC_CUID` returns zero matches in the v13.0.88 binary. Any documentation that claims nvlink "handles CUIDs" is confusing the toolchain roles — CUIDs live in the **host** wrapper produced by `cudafe++` and are referenced by the CUDA runtime at load time, not by nvlink at link time. See the [cudafe++ wiki: Host Reference Arrays](../../cudafe++/output/host-reference-arrays.html) for the host side.

What nvlink **does** handle is the `__nv_module_id` section. When `--register-link-binaries <path>` is set and the host ELF either has no extractable fatbin (all three section probes fail) or one that has already been consumed, nvlink calls:

```c
// sub_46F0C0 -- load_module_id_section(elf_buf, filename, size_out)
void *load_module_id_section(void *elf_buf, const char *filename,
                              uint32_t *size_out) {
    *size_out = 0;
    if (!elf_buf) {
        error_emit(..., "__nv_module_id", filename);   // sub_467460
        return NULL;
    }
    if (!section_exists(elf_buf, "__nv_module_id"))    // sub_476EC0
        return NULL;

    void *data = get_section_data(elf_buf, "__nv_module_id");  // sub_476F10
    *size_out = get_section_size(elf_buf, "__nv_module_id");   // sub_476F60
    return data;
}
```

`sub_4298C0` then parses the section as a sequence of `def <name>\0` entries:

```c
// sub_4298C0 -- extract_module_ids(elf_buf, filename, module_list)
void extract_module_ids(void *elf_buf, const char *filename, void *list) {
    uint32_t size;
    const char *p = load_module_id_section(elf_buf, filename, &size);   // sub_46F0C0
    if (!p) return;

    const char *end = p + size - 1;
    while (p < end) {
        if (memcmp(p, "def ", 4) != 0) {
            // Between entries we expect a single NUL separator; anything
            // else is a malformed table.
            if (*p != '\0') {
                error_emit(..., "unexpected data in module_ids", ...);
                return;
            }
            p++;
            continue;
        }

        // Copy the name that follows "def "
        size_t name_len = strlen(p + 4);
        void *tls = sub_44F410(p + 4);
        char *copy = arena_alloc(*(void **)(tls + 24), name_len + 1);
        if (!copy) alloc_fail_handler();             // sub_45CAC0
        strcpy(copy, p + 4);

        append_module_id(copy, list);                 // sub_4644C0

        // Advance past the whole `def <name>\0` entry
        p += strlen(p) + 1;
    }
}
```

Each entry's format is:

```text
"def " <module-name> "\0"
 4 B    variable       1 B
```

The parser scans forward one entry at a time, advances past the `NUL` terminator after each name, and stops when it reaches `end` (section data + size - 1). Malformed entries (anything that is neither `"def "` nor a `NUL` byte in the separator slot) trigger the diagnostic `"unexpected data in module_ids"` — literal string 5182 in `nvlink_strings.json`.

The collected names end up in the module-id list that `--register-link-binaries` later writes out in the `DEFINE_REGISTER_FUNC(%s)\n` format (strings.json entry 6175), giving the host side a way to locate each re-linked module at runtime without an intermediate fatbin envelope.

## Error Path: 30672788

`sub_476D90` reports a single error code to the diagnostic subsystem via `sub_467460`:

```c
error_emit(dword_2A5BDB0, 30672788, filename);
```

The numeric literal `30672788` is an address of an error descriptor in the `.rodata` section (around `0x1D43254`). It describes the "no device code in <file>" warning that the input-type reporter (`sub_4297B0`) also uses when `fatbin_data == NULL` and the `--register-link-binaries` fallback does not apply. The error is emitted when:

1. `elf_buf == NULL` (host ELF load failed earlier); **or**
2. `.nvFatBinSegment` exists but neither `__nv_relfatbin` nor `.nv_fatbin` is present (malformed embedding); **or**
3. `__nv_relfatbin` exists but its data does not start with `0xBA55ED50` (corrupt wrapper); **or**
4. The section data accessor returned `NULL` (section header present but offset out of file).

In all other paths (`.nvFatBinSegment` missing, or `.nv_fatbin` present as a sentinel) the function returns `NULL` silently and the host object is quietly skipped.

## Host Linker Script Generation

On the output side, nvlink can emit a host linker script that teaches `ld` how to collect all three fatbin sections into the final host executable. The feature is triggered by `--gen-host-linker-script` / `-ghls` and controlled by the mode variable `dword_2A77DC0`:

| `dword_2A77DC0` | Meaning |
|---|---|
| `0` | Normal mode — no linker script emitted |
| `1` | Generate-only mode — script is the sole output |
| `2` | Augmented mode — script is appended to an existing output and test-run through `ld` |

The script body is a single 130-byte (`0x82`) literal stored as strings.json entry 6233:

```ld
SECTIONS
{
	.nvFatBinSegment : { *(.nvFatBinSegment) }
	__nv_relfatbin : { *(__nv_relfatbin) } 
	.nv_fatbin : { *(.nv_fatbin) }
}
```

Destinations depending on mode:

- Output file opened with `"w"` in generate-only mode;
- Output file opened with `"a"` in augmented mode, followed by a test invocation `ld -T <script> 2>&1 | grep 'no input files' > /dev/null` to verify that `ld` can parse the script;
- `stdout` as a fallback when no output file is specified.

The three-section collection rule is symmetric to the extraction rule: at output time, nvlink asks the host linker to gather everything it would have extracted at input time, ensuring round-trip consistency between host objects that nvlink consumed and the host executable it helped build.

## Host ELF Memory Management

All buffers on the host ELF path are arena-allocated:

| Buffer | Allocated by | Freed by | Lifetime |
|---|---|---|---|
| Host ELF image | `sub_43DFC0` -> `sub_4307C0` | `sub_476EA0` -> `arena_free` | Spans `load_host_elf` to end of this input's main() branch |
| Fatbin copy | `sub_476D90` -> `sub_4307C0` | Arena teardown at end of run | Spans the whole fatbin pipeline, outlives the host ELF image |
| Extension copy | `sub_462620` -> `arena_strdup` | `sub_431000` explicitly | Freed before `load_host_elf` is called |
| Module-id name | `sub_4298C0` -> `sub_4307C0` | Arena teardown | Persists to the register-link-binaries output phase |

The critical detail is that the fatbin copy is **separately** arena-allocated and outlives the host ELF image. This is why `sub_476D90` performs an explicit `memcpy` instead of returning a pointer into the host ELF buffer — the host buffer is released immediately after the fatbin pipeline consumes its input, but the pipeline keeps references to the wrapper (and its containers) for much longer.

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| `sub_43DFC0` is the host ELF loader | HIGH | Standard fopen/fread/fseek pattern, ELF magic validation, returns NULL on any failure, matches main() usage |
| `sub_476D90` scans three fatbin sections | HIGH | All three strings present verbatim in strings.json (entries 17030, 17047, 17064); function probes them in source order |
| Only `__nv_relfatbin` is actually extracted | HIGH | Direct control-flow trace: only the `__nv_relfatbin` branch calls `get_section_data` and `memcpy` |
| Fatbin wrapper magic is `0xBA55ED50` | HIGH | Signed int32 `-1168773808` hardcoded in `sub_476D90`; matches main() fatbin dispatch magic |
| `extract_size = data_size + 16` | HIGH | `*(_QWORD *)(v2 + 8) + 16LL` in decompiled function; matches documented fatbin wrapper layout |
| `is_host_elf` is `e_type == ET_REL` | HIGH | `sub_43D9B0` reads 16-bit word at offset 16 and compares with 1; matches standard Elf32/64 `e_type` layout |
| `e_machine == 190` disambiguates device ELFs | HIGH | `main()` reads 16-bit word at offset 18 (`e_machine`) and compares with 190 (EM_CUDA) |
| Minimum file size is 52 bytes | HIGH | Literal `52` in `sub_43DFC0`; matches Elf32 header size |
| Exempt `sh_type` codes use bitmask `0x400D` | HIGH | Literal hex `0x400DuLL` in `sub_43DD30` |
| nvlink does NOT reference `CUDA_STATIC_CUID` | HIGH | Full string-table scan returns zero matches for CUID |
| `__nv_module_id` holds `def <name>\0` entries | HIGH | Literal `"def "` 4-byte compare in `sub_4298C0`; `strlen` advance after each entry |
| Error code `30672788` is "no device code in" | MEDIUM | Same descriptor passed to `sub_467460` in both `sub_476D90` and `sub_4297B0`; textual label inferred from context |
| `sub_4BDB70` is a fatbin-runtime availability check | MEDIUM | Returns `!sub_D9A2C0()`; `sub_D9A2C0` is a library-probe function. Exact meaning (dlopen check?) not fully traced. |
| Arena ownership split between host ELF and fatbin copy | HIGH | Two distinct `sub_44F410` / `sub_4307C0` calls in `sub_43DFC0` and `sub_476D90` |
| Linker script is 130 bytes | HIGH | Literal string in strings.json entry 6233 is `0x82` bytes |

Overall confidence: **HIGH**. Every function in the host ELF path has been decompiled and its control flow traced end-to-end. The only uncertain area is the exact semantics of `sub_4BDB70` / `sub_D9A2C0` (fatbin runtime availability) and the precise textual form of error descriptor `30672788`.

## Function Reference

| Address | Reconstructed name | Size | Signature |
|---|---|---|---|
| `0x476D90` | `search_fatbin_sections` | 240 B | `void *(void *elf_buf, const char *filename)` |
| `0x476E80` | `load_host_elf` (thunk) | 7 B | `uint64_t (const char *filename)` |
| `0x43DFC0` | `load_host_elf` (impl) | 344 B | `uint64_t (const char *filename)` |
| `0x476EA0` | `free_host_elf` (thunk) | 7 B | `int (void *buf, uint64_t size)` |
| `0x43D970` | `is_elf` | 19 B | `bool (void *buf)` — magic test |
| `0x43D9A0` | `is_elf64` | 18 B | `bool (void *buf)` — class dispatch |
| `0x43D9B0` | `is_host_elf` | 42 B | `bool (void *buf)` — `e_type == ET_REL` |
| `0x476EC0` | `section_exists` | 71 B | `bool (void *elf_buf, const char *name)` |
| `0x476F10` | `get_section_data` | 79 B | `void *(void *elf_buf, const char *name)` |
| `0x476F60` | `get_section_size` | 79 B | `uint64_t (void *elf_buf, const char *name)` |
| `0x4483B0` | `elf64_find_section` | 486 B | `Elf64_Shdr *(void *elf, const char *name)` |
| `0x46B5D0` | `elf32_find_section` | 454 B | `Elf32_Shdr *(void *elf, const char *name)` |
| `0x448560` | `elf64_section_data` | 18 B | `void *(void *elf, Elf64_Shdr *shdr)` |
| `0x46B770` | `elf32_section_data` | 18 B | `void *(void *elf, Elf32_Shdr *shdr)` |
| `0x448580` | `elf64_section_size` | 18 B | `uint64_t (void *elf, Elf64_Shdr *shdr)` |
| `0x46B790` | `elf32_section_size` | 18 B | `uint32_t (void *elf, Elf32_Shdr *shdr)` |
| `0x43DD30` | `validate_elf_structure` | 536 B | `bool (void *buf, uint64_t size)` |
| `0x46F0C0` | `load_module_id_section` | 186 B | `void *(void *elf, const char *name, uint32_t *size_out)` |
| `0x4298C0` | `extract_module_ids` | 476 B | `void (void *elf, const char *filename, void *list)` |
| `0x4297B0` | `report_input_type` | 263 B | `void (int64_t type, const char *filename)` |
| `0x4644C0` | `append_module_id` | 100 B | `void *(char *name, void *list)` |

## Key Constants

| Constant | Value | Meaning |
|---|---|---|
| `0xBA55ED50` | `3126193488` unsigned / `-1168773808` signed | Fatbin wrapper magic |
| `0x464C457F` | `1179403647` | ELF magic (`"\x7fELF"` as little-endian `uint32_t`) |
| `190` / `0xBE` | — | `EM_CUDA` — CUDA device ELF machine type |
| `62` / `0x3E` | — | `EM_X86_64` — typical host machine type |
| `1` | — | `ET_REL` — relocatable object type |
| `1` | — | `ELFDATA2LSB` — little-endian encoding |
| `2` | — | `ELFCLASS64` — 64-bit ELF |
| `52` | — | Minimum accepted file size (Elf32 header size) |
| `64` | — | Elf64 header size, `e_shentsize` check in validator |
| `40` | — | Elf32 `e_shentsize` check in validator |
| `0x400D` | `16397` | Exempt-section bitmask for `sh_type` range `0x70000007..0x70000015` |
| `0x82` | `130` | Linker script literal string length |
| `30672788` | — | Error descriptor address for "no device code in <file>" |
| `"def "` | `0x20666564` | Module-id entry prefix |

## See Also

- [File Type Detection](file-type-detection.md) — how host ELF is identified as a fallback after failing cubin/archive/fatbin checks, including the full extension + magic dispatch tree
- [Input File Loop](../pipeline/input-loop.md) — the dispatch logic in `main()` that routes to the host ELF handler
- [ELF Parsing](elf-parsing.md) — the Elf32/Elf64 accessor functions (`sub_4483B0`, `sub_46B5D0`, `sub_448560`, `sub_46B770`) used by `sub_476EC0`, `sub_476F10`, and `sub_476F60`
- [Fatbin Extraction](fatbin-extraction.md) — the `sub_42AF40` pipeline that consumes the fatbin buffer returned by `sub_476D90`, including wrapper/container format and arch matching
- [Cubin Loading](cubin-loading.md) — device cubins extracted from host-ELF-embedded fatbins follow this path once `sub_42AF40` emits them
- [Archives](archives.md) — archive members that are themselves host ELFs re-enter this path after being unwrapped
- [Mode Dispatch](../pipeline/mode-dispatch.md) — the `--gen-host-linker-script` mode that generates linker scripts for these sections
- [Output Writing](../pipeline/output.md) — `--register-link-binaries` output consuming the module-id list built by `sub_4298C0`
- [Error Reporting](../infra/error-reporting.md) — the `sub_467460` diagnostic dispatcher and the `30672788` error descriptor

> For how the CUDA frontend (`cudafe++`) embeds device code into host objects in the first place, see the [cudafe++ wiki: Host Reference Arrays](../../cudafe++/output/host-reference-arrays.html) and its CUDA Unique ID (`CUDA_STATIC_CUID`) page. nvlink does not parse CUIDs directly; they live in the host-side registration shim.
