# Input File Loop

After option parsing and library resolution, nvlink enters its central input dispatch loop. This loop iterates a linked list of input file records (rooted at global `qword_2A5F330`), opens each file, reads a 56-byte header probe to classify the file format, extracts the file extension via `sub_462620` (path\_split), and dispatches to one of nine type-specific handlers. The loop is the single point where every input — cubin, PTX, fatbin, NVVM IR, LTO IR, bitcode, archive, host ELF, or unknown file — enters the linking pipeline. It lives inside the 58KB `main()` function at `0x409800`, not in a separate subroutine.

| | |
|---|---|
| **Location** | Inside `main()` at `0x409800`, decompiled lines 595–901 |
| **Input list root** | `qword_2A5F330` — head of the input file linked list |
| **Raw input list** | `qword_2A5F328` — the unprocessed input file list (before library resolution) |
| **Header probe size** | 56 bytes (`0x38`), read via `fread(ptr, 1, 0x38, fp)` |
| **Extension parser** | `sub_462620` (path\_split): splits path into directory, basename, extension |
| **Timing phase** | `"init"` timer running (started at line 593); the `"read"` timer is not started until after the LTO pass at line 1403 |
| **Error gate** | `*(_BYTE *)(sub_44F410(v64) + 1)` — error byte at offset +1 in TLS state; checked after the loop exits |

## Complete Pseudocode

The following pseudocode is a faithful reconstruction from `main_0x409800.c` lines 595–938, preserving all control flow, every branch, and every function call. Variable names match the decompilation where possible; comments explain the logic.

```c
// ============================================================
// INPUT FILE LOOP -- main() at 0x409800, lines 595-938
// ============================================================

// Accumulators (initialized before the loop at lines 373-376):
//   v353 = module_list_head     (linked list of registered device modules)
//   v354 = module_id_list_head  (linked list of host module-id records)
//   v365 = cudadevrt_ir_buf     (deferred libcudadevrt IR pointer)
//   v366 = cudadevrt_ir_size    (deferred libcudadevrt IR buffer size)

v73 = (QWORD *)qword_2A5F330;     // head of resolved input file list
if (!v73)
    goto LABEL_135;                // no inputs -- skip to post-loop

while (1) {
    // ---- Per-iteration reset ----
    LOBYTE(v361) = 0;              // mercury_flag for this input
    v74 = (char *)v73[1];          // file path (node offset +8)
    v362 = 0;                      // cubin_buf (output of load/compile)
    v367 = 0;                      // file_buf (raw file content)
    v368 = 0;                      // file_buf_size
    s1  = 0;                       // extension string (set by path_split)

    // ---- Verbose: announce input ----
    if (v55[64] & 1)               // verbose flag (bit 0 of elfw flags byte 64)
        fprintf(stderr, "link input %s\n", v74);

    // ---- Phase 1: Open file and read 56-byte probe ----
    v79 = fopen(v74, "rb");
    if (!v79)
        diag_emit(&unk_2A5B730, v74);     // fatal: "cannot open '%s'"

    v80 = fread(ptr, 1, 0x38, v79);       // ptr = stack buffer for header probe
    if (v80) {
        fclose(v79);
        if (v80 == 56)
            goto LABEL_94;                 // full read -- proceed to extension check
    } else {
        if (!ferror(v79)) {
            fclose(v79);
            goto LABEL_131;                // empty file -- "ignore input"
        }
        fclose(v79);
    }
    // Short read (0 < v80 < 56) or error: check if it is an archive
    if (!is_archive(ptr, 56))              // sub_487A90: checks "!<arch>\n"
        diag_emit(&unk_2A5B730, v74);      // fatal: cannot read header

LABEL_94:
    // ---- Phase 2: Parse extension ----
    v64 = v74;
    path_split(v74);                       // sub_462620 -> sets s1 = extension
    v84 = s1;

    if (!s1)
        goto LABEL_131;                    // no extension, no magic match -> ignore

    // ============================================================
    // Phase 3: Extension-first dispatch chain
    // ============================================================

    // ---- 3a. Extension == "cubin" ----
    if (strcmp_inline(s1, "cubin") == 0) {
        if (!is_elf(ptr))                  // sub_43D970: check 0x7F454C46
            diag_emit(&unk_2A5B670, "cubin not an elf?");
        if (get_elf_header(ptr)->e_machine != 190)
            diag_emit(&unk_2A5B670, "cubin not a device elf?");

        if (is_sass_cubin(ptr)) {          // sub_43DA40: SASS flag in e_flags
            // --- SASS cubin: Mercury post-link path (LABEL_178) ---
            arena_free(s1);                // sub_431000: free extension string
            v362 = load_cubin_from_file(v74);     // sub_43E100
            if (!validate_arch_and_merge(v55, v362, v74, &v361))  // sub_426570
                goto LABEL_185;            // validation failed -> cleanup
            if (!v361)                     // v361 = mercury_needed flag
                fnlzr_post_link(&v362, v74, dword_2A5F314, ptr, 0);  // sub_4275C0
            // LABEL_181:
            if (!validate_arch_and_merge(v55, v362, v74, 0))       // sub_426570
                goto LABEL_185;
            goto LABEL_182;                // register module
        }
        // --- Non-SASS cubin: direct merge (LABEL_184) ---
        arena_free(s1);                    // sub_431000
        v362 = load_cubin_from_file(v74);  // sub_43E100
        if (!validate_arch_and_merge(v55, v362, v74, 0))   // sub_426570
            goto LABEL_185;

    LABEL_182:
        register_module(&v353, v74, v362, 0);  // sub_42A680
        goto LABEL_133;
    }

    // ---- 3b. Extension == "ptx" ----
    if (s1[0]=='p' && s1[1]=='t' && s1[2]=='x' && s1[3]=='\0') {
        arena_free(s1);                    // sub_431000
        v367 = load_file(v74, 1);          // sub_476BF0: null-terminated read
        v368 = <file_size>;

        if (qword_2A5F290)                 // profiling enabled
            timing_start(ptr);             // sub_45CCD0

        v92 = dword_2A5B528;              // ptxas options table base
        v93 = build_ptxas_argv();          // sub_429BA0: serialize -Xptxas flags
        v94 = ptxas_jit_compile(           // sub_4BD760
                &v362,                     //   output cubin buffer
                v367,                      //   PTX source buffer
                dword_2A5F314,             //   target SM version
                byte_2A5F2C0,              //   optimization level
                dword_2A5F30C == 64,       //   64-bit address mode
                byte_2A5F310,              //   debug flag
                v93,                       //   extra ptxas options
                v92);                      //   options table
        check_elflink_error(v94, v74);     // sub_4297B0

        if (qword_2A5F290) {              // profiling: stop timer, emit CSV
            timing_stop(ptr);              // sub_45CCE0
            csv_write(qword_2A5F290, qword_2A5F318);  // sub_432340
        }
        if (byte_2A5F29B)                 // --verbose-keep: save cubin to disk
            save_to_disk(v362);            // sub_42A190

        // Mercury post-link for sm > 89
        if (dword_2A5F314 > 0x59)         // sm_version > 89
        {
            if (!validate_arch_and_merge(v55, v362, v74, &v361))
                goto LABEL_185;
            if ((!byte_2A5F225 || is_sass_cubin(v362)) && !v361)
                fnlzr_post_link(&v362, v74, dword_2A5F314, &s1, 0);
            // LABEL_181:
            if (!validate_arch_and_merge(v55, v362, v74, 0))
                goto LABEL_185;
            goto LABEL_182;
        }
        // Legacy path (sm <= 89)
        goto LABEL_185;                    // (actually checks sub_426570 below)
        // Correction: for sm <= 89, falls through to LABEL_185 which frees
        // v362 on failure, or to LABEL_182 which registers it
    }

    // ---- 3c. Extension == "fatbin" ----
    if (strcmp_inline(s1, "fatbin") == 0) {
        if ((int32_t)ptr[0] != -1168773808)   // 0xBA55ED50 as signed i32
            diag_emit(&unk_2A5B670, "fatbin wrong format?");
        arena_free(s1);                    // sub_431000
        v367 = load_file(v74, 0);          // sub_476BF0: binary read
        v368 = <file_size>;
        extract_and_process_fatbin(         // sub_42AF40
            v367,                           //   fatbin buffer
            0,                              //   member data (NULL = from file)
            v74,                            //   filename
            v55,                            //   elfw context
            0, 0, 0,                        //   archive flags (not from archive)
            &v353,                           //   module list accumulator
            &v354);                          //   module-id list accumulator
        goto LABEL_133;
    }

    // ---- 3d. Extension == "nvvm" or "ltoir" ----
    if (strcmp(s1, "nvvm") == 0 || strcmp(v84, "ltoir") == 0) {
        arena_free(v84);                   // sub_431000
        if (!byte_2A5F288)                 // -lto flag not set?
            diag_emit(&unk_2A5B670, "should only see nvvm files when -lto");
        v367 = load_file(v74, 0);          // sub_476BF0: binary read
        v368 = <file_size>;
        register_ir_module(v55, v367, v368, v74);  // sub_427A10
        goto LABEL_133;
    }

    // ---- 3e. Extension == "bc" ----
    if (s1[0]=='b' && s1[1]=='c' && s1[2]=='\0') {
        arena_free(v84);                   // sub_431000
        diag_emit(&unk_2A5B670, "should never see bc files");
        goto LABEL_133;
    }

    // ============================================================
    // Phase 4: Content-based fallback (extension did not match)
    // ============================================================

    // ---- 4a. Check archive magic ----
    v192 = is_archive(ptr, 56);            // sub_487A90: "!<arch>\n" or "!<thin>\n"

    if (!v192) {
        // Not an archive. Check for .so extension (skip silently).
        if (s1[0]=='s' && s1[1]=='o' && s1[2]=='\0')
            goto ignore_and_free;          // .so -> LABEL_131

        // Check if file is relocatable ELF
        if (is_relocatable_elf(ptr)) {     // sub_43D9B0: e_type == ET_REL
            // Check for .o extension with e_machine == 190 (device .o)
            if (s1[0]=='o' && s1[1]=='\0'
                && is_elf(ptr)
                && get_elf_header(ptr)->e_machine == 190)
            {
                // Device relocatable object -- treat as cubin
                // (identical to LABEL_178 / LABEL_184 path)
                if (is_sass_cubin(ptr))
                    goto LABEL_178;        // SASS: Mercury post-link path
                else
                    goto LABEL_184;        // Non-SASS: direct merge
            }

            // Non-device ELF .o -- host object or fatbin-in-.o
            arena_free(s1);                // sub_431000
            v194 = load_host_elf(v74);     // sub_476E80 -> sub_43DFC0
            v195 = classify_member(ptr, v194, v74);  // sub_4BDB70
            check_elflink_error(v195, v74);

            if (v367) {
                // Embedded fatbin found inside host object
                extract_and_process_fatbin(
                    v367, v194, v74, v55,
                    0, 0, 0, &v353, &v354);
            } else {
                // Pure host ELF -- extract module IDs if --register-link-binaries
                if (qword_2A5F2E0 && v194)
                    extract_module_ids(v194, v74, &v354);  // sub_4298C0
                byte_2A5F212 = 1;          // host_object_seen flag
            }
            free_host_elf(v194);           // sub_476EA0
            goto LABEL_133;
        }

    ignore_and_free:
        arena_free(s1);                    // sub_431000
    LABEL_131:
        if (v55[64] & 1)                  // verbose flag
            fprintf(stderr, "ignore input %s\n", v74);
        goto LABEL_133;
    }

    // ---- 4b. File IS an archive (.a) ----
    arena_free(s1);                        // sub_431000

    // Walk the already-processed archives set to find a match
    for (j = set_begin(qword_2A5F2F0); ; j = set_next(j)) {
                                           // sub_464A80, sub_464AA0
        if (set_is_end(j)) {              // sub_464A90
            // ---- cudadevrt deferral check ----
            if (!v353 && strstr(v74, "cudadevrt"))
                goto LABEL_131;            // defer cudadevrt if no modules yet

            // ---- Process this archive ----
            v367 = load_file(v74, 0);      // sub_476BF0
            v368 = <file_size>;
            v314 = archive_open(&v363, v367, v368, v74);  // sub_4BDAC0
            check_elflink_error(v314, v74);

            // ---- Iterate all members (WHOLE-ARCHIVE semantics) ----
            while (1) {
                v315 = archive_next(&s1, v363);  // sub_4BDAF0
                check_elflink_error(v315, v74);
                if (!s1)                   // no more members
                    break;

                v316 = archive_get_path(v363);   // sub_4BDB60
                v317 = classify_member(ptr, s1, v316);  // sub_4BDB70
                check_elflink_error(v317, v316);

                if (ptr[0]) {
                    // Member has recognizable content -> fatbin dispatch
                    extract_and_process_fatbin(
                        ptr[0],            //   member content
                        s1,                //   raw member data
                        v316,              //   "archive:member" path
                        v55,               //   elfw context
                        1,                 //   from_archive = 1
                        &v365,             //   cudadevrt_ir_buf accumulator
                        &v355,             //   cudadevrt_module_id
                        &v353,             //   module list
                        &v354);            //   module-id list
                    arena_free(ptr[0]);    // sub_431000
                } else {
                    // Unrecognized member -- host ELF or plain data
                    if (qword_2A5F2E0 && s1)
                        extract_module_ids(s1, v316, &v354);
                    byte_2A5F212 = 1;      // host_object_seen flag
                }
            }

            v318 = archive_close(v363);    // sub_4BDB30
            check_elflink_error(v318, v74);
            set_insert(v74, &qword_2A5F2F0);  // sub_4644C0: record as processed
            goto LABEL_133;
        }

        // Check if this archive was already processed
        v57 = set_get_value(j);            // sub_464AC0
        if (path_matches(v74, v57))        // sub_4632F0
            break;                         // already processed -> skip
    }
    // Fell out of set walk via break -> archive already processed
    if (!v353 && strstr(v74, "cudadevrt"))
        goto LABEL_131;                    // still defer cudadevrt

    // ============================================================
    // LABEL_133: End of per-input processing
    // ============================================================
LABEL_133:
    if (!v367) {                           // no file buffer to free
        v73 = (QWORD *)*v73;              // advance to next node
        if (v73)
            continue;                      // next iteration
        // Fall through to LABEL_135 (loop exit)
    }
    // (If v367 is set, the file buffer ownership was transferred
    //  to the fatbin/archive handler; no free needed here.)

    // ============================================================
    // LABEL_135: Post-loop -- error check and LTO entry
    // ============================================================
LABEL_135:
    if (*(_BYTE *)(sub_44F410(v64) + 1))   // error byte set?
        exit(-1);                          // LABEL_271: fatal exit

    // LTO validation
    if (byte_2A5F288 && !dword_2A5F280)    // -lto set but no IR modules?
    {
        diag_emit(&unk_2A5B5D0, ...);     // warning: -lto but nothing to compile
        byte_2A5F288 = 0;                  // disable LTO
    }

    if (!byte_2A5F288) {
        v342 = 1;                          // skip LTO, go to LABEL_311 (merge)
        goto LABEL_311;
    }

    // ---- libcudadevrt deferred injection (lines 922-938) ----
    if (v365) {                            // cudadevrt IR was captured from archive
        register_ir_module(v55, v365, v366, "libcudadevrt");  // sub_427A10

        // Create an 80-byte module record for libcudadevrt
        v108 = (char **)arena_alloc(80);   // sub_426AA0
        memset(v108, 0, 80);              // zero-init all 20 dwords
        v111 = (char *)arena_alloc(13);    // sub_426AA0
        strcpy(v111, "libcudadevrt");
        v108[0] = v111;                    // module name = "libcudadevrt"
        v108[1] = v355;                    // module-id from archive extraction
        set_insert(v108, &v353);           // sub_4644C0: add to module list
    }

    if (v55[64] & 1)                      // verbose
        fwrite("compile linked lto ir:\n", 1, 0x17, stderr);

    // ... LTO compilation begins (lines 942+) ...
}
```

### Key Structural Points

1. **The loop is `while(1)` with manual `continue`**. The loop variable `v73` is advanced at LABEL\_133 (`v73 = *v73`) and the `continue` restarts the iteration. There is no `for` header; the decompiler shows a `while(1)` because the linked list traversal pattern is pointer-chasing, not counter-based.

2. **Extension check comes before magic number check**. The decompiled code calls `sub_462620` (path\_split) to extract the extension, then compares the extension string against known values (`"cubin"`, `"ptx"`, `"fatbin"`, `"nvvm"`, `"ltoir"`, `"bc"`) before falling through to content-based detection (`sub_487A90` for archive magic, `sub_43D9B0` for relocatable ELF). The magic number probe in `ptr` is a secondary signal, not the primary dispatch key.

3. **Extension comparison is hand-inlined**. The `"cubin"` and `"fatbin"` comparisons are 6/7-byte inline memcmp loops (visible at lines 639–650 and 737–748). The `"ptx"` check is three individual character tests (lines 681–690). Only `"nvvm"` and `"ltoir"` use `strcmp`.

4. **Dispatch is a linear if-else chain, not a switch**. The order is: cubin -> ptx -> fatbin -> nvvm/ltoir -> bc -> archive-probe -> host-ELF/ignore.

## Input File Linked List

Input files are collected during option parsing and library resolution into a singly-linked list rooted at `qword_2A5F330`. Each node is at least a two-word structure:

| Offset | Size | Content |
|---|---|---|
| +0 | 8 bytes | Pointer to the next node (NULL = end of list) |
| +8 | 8 bytes | Pointer to the file path string |

The loop accesses these fields directly:
- `v73[0]` is the next pointer (dereferenced as `*v73` at line 905)
- `v73[1]` is the file path (read at line 601)

The list is not modified during iteration. All inputs are processed sequentially in the order they appear on the command line (after library resolution has expanded `-l` flags into resolved paths). This ordering is semantically significant: archive processing defers `libcudadevrt` until at least one device module (`v353 != 0`) has been seen, so placing `-lcudadevrt` before any device inputs causes it to be silently ignored.

## 56-Byte Header Probe

Before consulting the file extension, nvlink reads the first 56 bytes of every input file. This probe buffer is large enough to contain:

- The 16-byte ELF identification block (`e_ident[16]`) plus the first 40 bytes of the ELF header (covering `e_type`, `e_machine`, `e_version`, `e_entry`, `e_phoff`, `e_shoff` in both ELF32 and ELF64 formats)
- The 8-byte archive magic string `"!<arch>\n"` or `"!<thin>\n"`
- The 4-byte fatbin magic `0xBA55ED50`
- The 4-byte NVVM IR magic `0x1EE55A01`
- Enough bytes to detect a `.version` directive at the start of a PTX file

The probe is read with `fread(ptr, 1, 0x38, fp)` (line 612). If the file is shorter than 56 bytes, `v80` (`nread`) reflects the actual size. The behavior on short reads:

| v80 value | Behavior |
|---|---|
| 0 (and no ferror) | Empty file — falls through to LABEL\_131 ("ignore input") |
| 0 (with ferror) | Read error — `sub_487A90` archive check; fatal if not an archive |
| 1–55 | Short read — `sub_487A90` archive check; fatal if not an archive |
| 56 | Full read — proceeds to extension parsing at LABEL\_94 |

### Magic Number Table

| Magic value | Byte representation | Format | Notes |
|---|---|---|---|
| `0x464C457F` | `7F 45 4C 46` | ELF | Standard ELF magic (`\x7fELF`). Further classified by `e_machine` field |
| `0xBA55ED50` | `50 ED 55 BA` | Fatbin | NVIDIA fatbin container. Stored as signed int32 `-1168773808` in decompilation |
| `0x1EE55A01` | `01 5A E5 1E` | NVVM IR | NVIDIA's NVVM bitcode wrapper format |
| `"!<arch>\n"` | `21 3C 61 72 63 68 3E 0A` | Archive (.a) | Standard Unix archive magic (8 bytes) |
| `"!<thin>\n"` | `21 3C 74 68 69 6E 3E 0A` | Thin archive | Thin archive variant (members by external path) |
| `".version"` | `2E 76 65 72 73 69 6F 6E` | PTX | PTX assembly files start with a `.version` directive |

### ELF Sub-Classification

When the extension is `.cubin` or `.o` and the 4-byte magic matches ELF (`0x464C457F`), nvlink reads the `e_machine` field from the ELF header to distinguish device ELF from host ELF:

| `e_machine` value | Meaning | Handler |
|---|---|---|
| 190 (`0xBE`) | `EM_CUDA` — NVIDIA CUDA device ELF | Cubin handler |
| Any other value | Host ELF (x86-64, ARM, etc.) | Host ELF / "ignore input" path |

The constant 190 is NVIDIA's registered ELF machine type for CUDA device code (`EM_CUDA`). The check is at line 654: `*(_WORD *)(sub_448360(ptr) + 18) != 190`. A secondary check at line 799 applies the same test for extensionless `.o` files that fall through to the content-based detection path.

## Extension Parsing: sub\_462620 (path\_split)

After the header probe, nvlink calls `sub_462620` to decompose the file path into its directory, basename, and extension components.

| | |
|---|---|
| **Address** | `0x462620` |
| **Size** | 3,579 bytes / 157 lines |
| **Signature** | `path_split(const char *path, char **dir_out, char **base_out, char **ext_out)` |
| **Method** | Uses `strrchr` to find the last `'/'` (directory separator) and last `'.'` (extension separator) |
| **Allocation** | Output strings allocated via `sub_4307C0` (arena allocator) |
| **Extension output** | Stored in local variable `s1` at line 607 |

The extracted extension is the **primary classification signal** — the dispatch chain tests it first, before any magic-number comparison. Magic is used as a **validation** within each extension branch (e.g., `.cubin` must pass the ELF magic check, `.fatbin` must match `0xBA55ED50`), not as the initial dispatch key.

Extension strings recognized by the dispatch logic:

| Extension | Format | Magic validation | Decompiled line |
|---|---|---|---|
| `.cubin` | CUDA device ELF | `0x464C457F` + `e_machine == 190` | 639–677 |
| `.ptx` | PTX assembly | None (extension is sufficient) | 679–735 |
| `.fatbin` | Fatbin container | `0xBA55ED50` (signed `-1168773808`) | 737–759 |
| `.nvvm` | NVVM IR | None (extension + `-lto` gate) | 761–778 |
| `.ltoir` | LTO IR | None (extension + `-lto` gate) | 761–778 |
| `.bc` | LLVM bitcode | None (always fatal) | 780–786 |
| `.so` | Shared object (host) | None (always ignored) | 793 |
| `.o` | Object file | `0x464C457F` + `e_machine` check | 799 |
| `.a` | Static archive | `"!<arch>\n"` or `"!<thin>\n"` via `sub_487A90` | 789, 849–901 |
| _(none)_ | Falls through | Content-based via `sub_487A90`, `sub_43D9B0` | 788–847 |

## Complete Dispatch Table

The dispatch logic inside main combines the file extension, the magic number probe, the `e_machine` field (for ELF), and the SASS flag to route each input to the appropriate handler. The following table documents every dispatch path in the order the code tests them.

### 1. Cubin (`.cubin` extension)

| | |
|---|---|
| **Detection** | Extension `"cubin"` (inline 6-byte memcmp at lines 639–650) |
| **Validation** | `sub_43D970` (is\_elf) verifies `0x7F454C46` magic; `e_machine == 190` at offset +18 |
| **SASS path** | `sub_43DA40` (is\_sass\_cubin) checks SASS flag in `e_flags` |
| **SASS handler** | `sub_43E100` (load\_cubin\_from\_file) -> `sub_426570` (validate\_arch\_and\_merge) -> `sub_4275C0` (fnlzr\_post\_link) -> `sub_426570` again -> `sub_42A680` (register\_module) |
| **Non-SASS handler** | `sub_43E100` -> `sub_426570` -> `sub_42A680` |
| **Error strings** | `"cubin not an elf?"`, `"cubin not a device elf?"` |

The SASS cubin path runs the FNLZR (Finalizer) post-link transform via `sub_4275C0` for Mercury architectures. After FNLZR, `sub_426570` is called a second time to re-validate the transformed cubin. The non-SASS path skips FNLZR entirely.

### 2. PTX Assembly (`.ptx` extension)

| | |
|---|---|
| **Detection** | Extension `"ptx"` (three-byte character test at lines 681–690) |
| **Loader** | `sub_476BF0(v74, 1)` — load file with null termination |
| **Handler** | `sub_4BD760` (ptxas JIT compilation) |
| **Timing** | If `qword_2A5F290`: `sub_45CCD0` (start) -> ptxas -> `sub_45CCE0` (stop) -> `sub_432340` (CSV row) |
| **Verbose-keep** | If `byte_2A5F29B`: `sub_42A190` writes compiled cubin to disk |
| **Mercury path** | For `dword_2A5F314 > 0x59` (sm > 89): `sub_426570` -> optional `sub_4275C0` (FNLZR) -> `sub_426570` again |
| **Legacy path** | For `dword_2A5F314 <= 0x59` (sm <= 89): `sub_426570` -> `sub_42A680` |
| **Arguments to sub\_4BD760** | `(&v362, v367, sm_version, opt_level, is_64bit, debug, ptxas_argv, options_table)` |

The `sub_429BA0` call (line 698) serializes the accumulated `-Xptxas` option list into a space-separated string. The `sub_4BD760` call produces a cubin in `v362`. After compilation, the FNLZR condition is: `dword_2A5F314 > 0x59` AND ((`!byte_2A5F225` OR `is_sass_cubin(v362)`) AND `!v361`).

### 3. Fatbin Container (`.fatbin` extension)

| | |
|---|---|
| **Detection** | Extension `"fatbin"` (inline 7-byte memcmp at lines 737–748) |
| **Validation** | `ptr[0] == -1168773808` (i.e., first 4 bytes == `0xBA55ED50`) |
| **Loader** | `sub_476BF0(v74, 0)` — load file in binary mode |
| **Handler** | `sub_42AF40` (extract\_and\_process\_fatbin\_member) |
| **Call signature** | `sub_42AF40(buf, 0, path, elfw, 0, 0, 0, &v353, &v354)` |
| **Error string** | `"fatbin wrong format?"` |

The fatbin handler `sub_42AF40` (11,143 bytes / 521 lines) is the most complex dispatch path. It iterates over embedded members using `sub_4BD0A0` (fatbin extract). Each member is classified by its internal type field: type 1 = PTX, type 8 = NVVM, type 16 = mercury/capmerc, default = cubin. Each extracted member recurses back into the type dispatch. Supports LZ4-compressed members.

When called from the archive path (see section 7), the 5th parameter is `1` (from\_archive flag) and extra accumulator pointers `&v365` (cudadevrt IR) and `&v355` (cudadevrt module-id) are passed.

### 4. NVVM IR (`.nvvm` extension)

| | |
|---|---|
| **Detection** | `strcmp(s1, "nvvm") == 0` at line 761 |
| **Prerequisite** | `byte_2A5F288` (LTO flag) must be set; otherwise fatal: `"should only see nvvm files when -lto"` |
| **Loader** | `sub_476BF0(v74, 0)` — binary read |
| **Handler** | `sub_427A10` (register\_ir\_module) |
| **Call signature** | `sub_427A10(elfw, buf, size, path)` |
| **Verbose output** | `"nvlink -lto-add-module %s.nvvm"` (inside sub\_427A10) |

### 5. LTO IR (`.ltoir` extension)

| | |
|---|---|
| **Detection** | `strcmp(v84, "ltoir") == 0` at line 761 (same condition as NVVM) |
| **Handler** | `sub_427A10` — identical to NVVM IR |
| **Behavior** | LTO IR is NVIDIA's name for NVVM IR modules produced by cicc with `-dlto` or `-lto` during separate compilation. The `.ltoir` extension is a convention; the content is NVVM bitcode. |

Both `.nvvm` and `.ltoir` share the same code path (lines 761–778). The `strcmp` checks are ORed together.

### 6. LLVM Bitcode (`.bc` extension)

| | |
|---|---|
| **Detection** | `s1[0]=='b' && s1[1]=='c' && s1[2]=='\0'` at line 780 |
| **Handler** | None (fatal error) |
| **Behavior** | Emits `"should never see bc files"` via `sub_467460` with descriptor `unk_2A5B670`. Raw `.bc` files should have been compiled to NVVM IR or cubin by cicc before reaching the linker. |

### 7. Static Archive (`.a` / detected by content)

| | |
|---|---|
| **Detection** | `sub_487A90(ptr, 56)` returns true — content matches `"!<arch>\n"` or `"!<thin>\n"` magic |
| **Duplicate check** | Walks `qword_2A5F2F0` (set of already-processed archive paths) via `sub_464A80`/`sub_464A90`/`sub_464AA0`/`sub_464AC0` and `sub_4632F0` (path match) |
| **cudadevrt deferral** | If `v353 == 0` (no modules registered yet) and `strstr(v74, "cudadevrt")` matches, the archive is skipped (line 854, 895–900) |
| **Loader** | `sub_476BF0(v74, 0)` — binary read of entire archive |
| **Open** | `sub_4BDAC0(&v363, v367, v368, v74)` -> `sub_487C20` |
| **Iterate** | `while(1)`: `sub_4BDAF0(&s1, v363)` -> `sub_487E10`; break when `s1 == NULL` |
| **Per-member classify** | `sub_4BDB70(ptr, s1, v316)` — detects fatbin/cubin/host content |
| **Per-member dispatch** | `sub_42AF40(ptr[0], s1, v316, v55, 1, &v365, &v355, &v353, &v354)` — note `from_archive = 1` |
| **Close** | `sub_4BDB30(v363)` -> `sub_488200` |
| **Record processed** | `sub_4644C0(v74, &qword_2A5F2F0)` |

**Whole-archive semantics**: nvlink processes **every member** of every archive unconditionally. There is no symbol-directed extraction. The `while(1)` loop at lines 860–880 iterates until `s1 == NULL` (no more members), calling `sub_42AF40` on each one. This is equivalent to GNU ld's `--whole-archive` behavior, and it is the **only** behavior nvlink implements. There is no `--whole-archive` / `--no-whole-archive` flag because the behavior is always on.

### 8. Host ELF (`.o` / `.so` / extensionless)

| | |
|---|---|
| **Detection (`.so`)** | `s1[0]=='s' && s1[1]=='o' && s1[2]=='\0'` at line 793 — immediately ignored |
| **Detection (`.o`)** | Passes `sub_43D9B0` (is\_relocatable\_elf) AND `s1[0]=='o' && s1[1]=='\0'` AND `e_machine == 190` routes to cubin handler. Otherwise routes to host ELF path |
| **Host ELF loader** | `sub_476E80` (thunk -> `sub_43DFC0`) at `0x476E80` |
| **Content probe** | `sub_4BDB70(ptr, v194, v74)` — checks for embedded fatbin sections |
| **If fatbin found** | `sub_42AF40(v367, v194, v74, v55, 0, 0, 0, &v353, &v354)` |
| **If pure host** | `sub_4298C0(v194, v74, &v354)` (extract module IDs, only when `qword_2A5F2E0` is set) |
| **Cleanup** | `sub_476EA0(v194)` (free host ELF buffer) |
| **Side effect** | `byte_2A5F212 = 1` (host\_object\_seen flag) |

Host `.o` objects with `e_machine == 190` are treated as device cubins, not host ELFs. This handles the case where ptxas output files have a `.o` extension but are actually device ELFs.

### 9. Unknown / Unrecognized

| | |
|---|---|
| **Detection** | No extension match, `sub_487A90` returns false (not archive), `sub_43D9B0` returns false (not relocatable ELF) |
| **Handler** | LABEL\_131 (lines 840–847) |
| **Behavior** | If verbose: `fprintf(stderr, "ignore input %s\n", v74)`. File is skipped. |
| **Not fatal** | nvlink tolerates unknown files on the command line |

## Error Accumulation Across Inputs

Errors during input processing are handled by the diagnostic infrastructure (`sub_467460`), not by a per-file return code. The accumulation works as follows:

1. **Fatal errors** (descriptor `unk_2A5B730`, severity 6): Trigger `longjmp` to the recovery point, which jumps to `LABEL_271` -> `exit(-1)`. Example: `"cannot open '%s'"`. The loop never continues after a fatal.

2. **Hard errors** (descriptor `unk_2A5B670`, severity 5): Emit the error message and set the error byte in TLS state (`*(_BYTE *)(sub_44F410(v64) + 1) = 1`). The loop **does continue** processing subsequent inputs. The error byte is checked after the loop at LABEL\_135 (line 909): `if (*(_BYTE *)(sub_44F410(v64) + 1)) goto LABEL_271` (exit). This means nvlink reports errors for all inputs before dying, rather than stopping at the first.

3. **elfLink error codes**: Every archive/member operation returns an integer status that is checked by `sub_4297B0` (check\_elflink\_error). Non-zero codes are translated through `dword_1D48A50` and may trigger a fatal diagnostic.

4. **Validation failures**: `sub_426570` (validate\_arch\_and\_merge) returns a boolean. On failure (`return 0`), the cubin is freed via `sub_43D990` at LABEL\_185 and processing continues to the next input. This is a soft failure — an arch mismatch on one input does not block other inputs from being processed.

## QUIRK: `--whole-archive` Is Not a Flag (It Is the Only Mode)

nvlink does not implement `--whole-archive` as a command-line option because whole-archive is the **only** archive loading mode. The token `--whole-archive` does not appear in nvlink's option table at all, and neither does `--no-whole-archive` or `--start-group`/`--end-group`. Every archive member is unconditionally loaded and processed in a single pass. Specifically:

- There is no `--no-whole-archive` flag.
- The symbol table (`/` member) is structurally detected and skipped, but its contents are never read. nvlink has no code to do symbol-directed member selection.
- The `qword_2A5F2F0` set tracks already-processed archives to prevent double-loading when the same archive appears multiple times (e.g., via multiple `-l` flags resolving to the same path).

The only conditional archive behavior is **cudadevrt deferral** (see below).

## The libcudadevrt Deferred Injection (Lines 922–938)

When nvlink encounters an archive containing `"cudadevrt"` in its path, and LTO mode is active, the archive member iteration path in `sub_42AF40` captures the libcudadevrt IR buffer into `v365`/`v366` instead of registering it immediately. This deferral has a specific reason: libcudadevrt's IR must be compiled together with user IR during the LTO batch, not separately.

After the input loop exits and LTO is confirmed active (`byte_2A5F288 != 0`), lines 922–938 execute:

```c
if (v365) {
    // Register the deferred IR with the LTO engine
    sub_427A10(v55, v365, v366, "libcudadevrt");

    // Create a module record (80 bytes, zero-initialized)
    v108 = (char **)sub_426AA0(80);       // arena_alloc(80)
    memset(v108, 0, 80);                  // 20 dwords = 80 bytes
    v111 = (char *)sub_426AA0(13);        // arena_alloc(13) for "libcudadevrt\0"
    strcpy(v111, "libcudadevrt");
    v108[0] = v111;                       // offset +0: module name
    v108[1] = v355;                       // offset +8: module-id pointer
    sub_4644C0(v108, &v353);              // append to module list
}
```

This ensures:
1. The libcudadevrt IR is registered for LTO compilation (`sub_427A10`)
2. A placeholder module record is created with the name `"libcudadevrt"` so the merge phase knows this module exists
3. The module-id from the archive extraction (`v355`) is preserved for `--register-link-binaries` output

The cudadevrt deferral also has a front gate: at lines 854 and 895–900, if no device modules have been registered yet (`v353 == 0`) and the archive path contains `"cudadevrt"`, the archive is skipped entirely via `goto LABEL_131`. This prevents loading cudadevrt when there is no user device code to link against.

## Verbose Output During Input Processing

The verbose flag is `v55[64] & 1` (bit 0 of the elfw flags byte at offset 64). This corresponds to the `--verbose` / `-v` CLI flag. The following messages are emitted during the input loop:

| Line | Condition | Output |
|---|---|---|
| 606 | `v55[64] & 1` | `"link input %s\n"` — printed for every input file |
| 844–845 | `v55[64] & 1` | `"ignore input %s\n"` — printed when a file is skipped |
| 941 | `v55[64] & 1` | `"compile linked lto ir:\n"` — printed at LTO entry after loop |

Inside the per-format handlers, additional verbose output is controlled by the same flag:
- Fatbin extraction (`sub_42AF40`): `"nvlink -extract %s -m%d -arch=%s -o %s"`
- NVVM IR registration (`sub_427A10`): `"nvlink -lto-add-module %s.nvvm"`
- PTX compilation: profiling timer start/stop if `qword_2A5F290` is set

The timing trace flag is `v55[64] & 0x20` (bit 5). When set (via `--verbose 0x20` or equivalent), it triggers `sub_4279C0` (phase\_timer) calls at phase boundaries (lines 590–593 for `"init"`). The input loop itself does not emit timing trace markers; the next timing marker after `"init"` is `"cicc-lto"` at line 1100.

## How Input Order Affects Symbol Resolution

Input order matters in three ways:

1. **First-definition wins for strong symbols**. When `sub_42A680` (register\_module) processes a cubin, it calls into the symbol resolver. The first cubin providing a strong definition of a given symbol establishes the canonical definition. Later cubins with the same strong symbol trigger a multiple-definition error (see [Symbol Resolution](../linker/symbol-resolution.md)).

2. **cudadevrt deferral is order-dependent**. If the archive containing `"cudadevrt"` appears on the command line before any device module (`v353 == 0`), it is silently skipped. This means `-lcudadevrt` must appear after at least one device object. The CUDA driver `nvcc` always places `-lcudadevrt` last, so this is not normally user-visible.

3. **Weak-vs-strong priority is insertion-order-dependent**. When multiple cubins provide the same weak symbol, the **first** one registered becomes the initial definition. A later strong definition overrides it. But two weak definitions for the same symbol are resolved in favor of the first one seen. This matches standard ELF linker behavior.

4. **Archive deduplication is path-based**. The `qword_2A5F2F0` set records archive paths as they are processed. If the same archive appears twice on the command line (via two `-l` flags resolving to the same file), the second occurrence is skipped entirely. The path match function `sub_4632F0` performs the comparison.

## Post-Dispatch: Module Registration

After type-specific processing produces a cubin (either directly or via compilation), nvlink registers the resulting module via `sub_42A680` (register\_module\_for\_linking) at LABEL\_182 (line 674–677). This function:

1. Allocates an 80-byte module record
2. Extracts the module\_id from the cubin's ELF metadata via `sub_46F0C0`
3. Handles `"def "` prefix stripping from module identifiers
4. Checks if the module was built for LTO (and warns if not: `"requested LTO but '%s' not built for LTO so doing partial LTO"`)
5. Links the module into the list `v353` (module list head) that will enter the merge phase

The call signature is: `sub_42A680(&v353, v74, v362, 0)` where `v353` is the module list head, `v74` is the input file path, `v362` is the cubin buffer, and the fourth parameter (0) indicates this is not from LTO.

The diagnostic string `"no module_id for %s"` fires when a cubin lacks the expected registration metadata, and `"extra module_id = %s"` appears in verbose mode when additional module IDs are discovered.

## Fatbin Member Extraction Detail

The fatbin handler `sub_42AF40` (11,143 bytes / 521 lines) is the most complex dispatch path. It deserves special attention because fatbin is the most common nvlink input format in practice — `nvcc` packages compiled objects into fatbin containers by default.

The internal type field in fatbin member headers maps to:

| Type code | Meaning | Handler within sub\_42AF40 |
|---|---|---|
| 1 | PTX source | `sub_4BD240` (ptxas compilation variant) |
| 8 | NVVM IR | `sub_427A10` (LTO IR registration) |
| 16 | Mercury / capmerc | Cubin path with Mercury flags |
| default | Cubin (SASS ELF) | `sub_426570` (validate and merge) |

Additional behaviors:

- **libdevice detection**: The handler checks for the substring `"libdevice"` in member names to identify NVIDIA's math library
- **LTO IR sniffing**: Checks for option strings like `"-inline-info"`, `"-ftz="`, `"-prec_div="` embedded in IR module metadata
- **Verbose-keep mode**: When `--verbose-keep` (`-vkeep`) is active, extracted members are written to disk with constructed filenames for inspection
- **Archive origin**: When `from_archive` is 1 (5th parameter), the handler accumulates cudadevrt IR into `v365`/`v366` instead of registering it immediately

## Flow Diagram

```text
                    +-----------------------+
                    | qword_2A5F330 (head)  |
                    +----------+------------+
                               |
                    +----------v-----------+
                    | v73 = list head      |
                    | if (!v73) goto exit  |
                    +----------+-----------+
                               |
                  +------------v-------------+
                  | while (1) {              |
                  |   v74 = v73[1] (path)    |
                  |   verbose: "link input"  |
                  |   fopen + fread 56 B     |
                  |   path_split -> ext s1   |
                  +------------+-------------+
                               |
             +-----------------v------------------+
             | Extension dispatch chain            |
             +----+------+------+-----+-----+-----+
                  |      |      |     |     |
             "cubin" "ptx" "fatbin" "nvvm" "bc"
                  |      |      |   "ltoir" |
                  |      |      |     |     |
             +----v-+  +-v--+  +--v--+ +-v-+ +--v------+
             |check |  |JIT |  |mag- | |LTO| |fatal:   |
             |is_elf|  |via |  |ic   | |reg| |"should  |
             |e_mach|  |4BD7|  |check| |427| |never    |
             |=190  |  |60  |  |42AF | |A10| |see bc"  |
             +--+---+  +--+-+  |40   | +---+ +---------+
                |         |    +--+--+
            +---v---+  +--v---+   |
            |SASS?  |  |sm>89?|   |
            +--+--+-+  +--+--++   |
               |  |       |  |    |
              yes no     yes no   |
               |  |       |  |    |
            +--v–v--+ +--v–v-+  |
            |FNLZR + | |direct |  |
            |re-valid| |merge  |  |
            +--------+ +------+  |
                                  |
             +-----+---------+---v---+
             |     Extension didn't match     |
             +-----+---------+---------+
                   |                   |
            !is_archive          is_archive
                   |                   |
              +----v-----+     +-------v--------+
              | .so? skip|     | dup check via  |
              | .o+190?  |     | qword_2A5F2F0  |
              |  cubin   |     | cudadevrt defer|
              | else:    |     +-------+--------+
              | host ELF |             |
              +----+-----+     +-------v--------+
                   |           | while(1):      |
              +----v-----+     | archive_next   |
              | 4BDB70:  |     | classify_member|
              | fatbin?  |     | sub_42AF40 per |
              | host?    |     | member         |
              +----------+     +-------+--------+
                                       |
                                archive_close
                                set_insert

              No match at all:
              +-----------------------+
              | "ignore input %s\n"  |
              | goto LABEL_133       |
              +-----------------------+

    LABEL_133: v73 = *v73 (next node)
               if (v73) continue
               else -> LABEL_135

    LABEL_135: error check -> exit(-1) if errors
               LTO gate -> if no LTO: merge phase
               libcudadevrt injection (lines 922-938)
               -> LTO compilation
```

## Diagnostic Strings

| String | Line | Context |
|---|---|---|
| `"link input %s\n"` | 606 | Verbose: announcing each input file |
| `"cubin not an elf?"` | 653 | `.cubin` extension but ELF magic `0x7F454C46` not found |
| `"cubin not a device elf?"` | 655 | ELF is valid but `e_machine != 190` |
| `"fatbin wrong format?"` | 751 | `.fatbin` extension but magic != `0xBA55ED50` |
| `"should only see nvvm files when -lto"` | 767 | `.nvvm`/`.ltoir` file without `-lto` flag |
| `"should never see bc files"` | 785 | `.bc` file encountered (should not reach nvlink) |
| `"ignore input %s\n"` | 845 | Verbose: file type not recognized, skipping |
| `"compile linked lto ir:\n"` | 941 | Verbose: LTO compilation starting after input loop |
| `"LTO on everything so remove libcudadevrt from list"` | inside sub\_42AF40 | libcudadevrt filtered out during full LTO |
| `"unexpected object after cudadevrt"` | inside sub\_42AF40 | Ordering violation in input list |
| `"requested LTO but '%s' not built for LTO so doing partial LTO"` | inside sub\_42A680 | Module lacks LTO IR; falls back to partial LTO |
| `"no module_id for %s"` | inside sub\_42A680 | Module registration metadata missing |
| `"extra module_id = %s"` | inside sub\_42A680 | Verbose: additional module IDs discovered |
| `"found IR for libcudadevrt"` | inside sub\_42AF40 | libcudadevrt IR detected in fatbin extraction |
| `"don't uplift %s"` | inside sub\_42AF40 | Module excluded from LTO uplift |
| `"nvlink -extract %s -m%d -arch=%s -o %s"` | inside sub\_42AF40 | Verbose: fatbin member extraction command |
| `"nvlink -lto-add-module %s.nvvm"` | inside sub\_427A10 | Verbose: NVVM IR module registration |
| `"could not find __nvvmHandle"` | 991 | LTO: dlsym failed for nvvm handle |

## Key Function Map

| Address | Size | Identity | Role in input loop | Decompiled line(s) |
|---|---|---|---|---|
| `0x409800` | 57,970 B | `main` | Contains the input loop inline | 595–938 |
| `0x462620` | 3,579 B | `path_split` | Decomposes file path into dir/base/ext | 634 |
| `0x462C10` | < 2 KB | path helper | Auxiliary path operation | 567 |
| `0x462550` | — | path helper | Secondary path utility | — |
| `0x43D970` | 19 B | `is_elf` | Checks 4-byte ELF magic `0x7F454C46` | 652, 799 |
| `0x43D9B0` | 42 B | `is_relocatable_elf` | Tests `e_type == ET_REL` | 795 |
| `0x43DA40` | 52 B | `is_sass_cubin` | Checks SASS flag in `e_flags` | 656, 726, 824 |
| `0x43D990` | — | `arena_free_elf` | Frees cubin buffer | 670 |
| `0x43E100` | 232 B | `load_cubin_from_file` | Elf32 file loader | 664, 830 |
| `0x448360` | — | `get_elf_header` | Returns pointer to ELF header from probe | 654, 799 |
| `0x426570` | 7,427 B | `validate_arch_and_merge` | Validates cubin arch, creates elfw, begins merge | 666, 724, 730, 832 |
| `0x42AF40` | 11,143 B | `extract_and_process_fatbin` | Fatbin container extraction and per-member dispatch | 758, 809, 871 |
| `0x42A680` | 11,939 B | `register_module_for_linking` | Post-dispatch module registration | 676 |
| `0x42A190` | — | `save_cubin_to_disk` | Writes cubin to file (verbose-keep mode) | 719 |
| `0x427A10` | — | `register_ir_module` | Registers NVVM/LTO IR for batch LTO compilation | 777, 924 |
| `0x4275C0` | 3,989 B | `fnlzr_post_link` | FNLZR (Finalizer) entry for Mercury/SASS | 727, 835 |
| `0x429BA0` | — | `build_ptxas_argv` | Serializes `-Xptxas` option list | 698 |
| `0x4BD760` | — | `ptxas_jit_compile` | Compiles PTX to SASS cubin via embedded ptxas | 699 |
| `0x4BD0A0` | — | `fatbin_extract_member` | Extracts individual object from fatbin container | inside sub\_42AF40 |
| `0x4BD240` | — | `ptxas_compile_fatbin_variant` | PTX compilation from within fatbin extraction | inside sub\_42AF40 |
| `0x476BF0` | 384 B | `load_file` | Opens file, reads entire content into arena buffer | 693, 753, 773, 856 |
| `0x476E80` | 7 B | `load_host_elf` | Thunk -> `sub_43DFC0` | 802 |
| `0x476EA0` | 7 B | `free_host_elf` | Thunk -> `sub_43D990` (arena\_free) | 821 |
| `0x487A90` | — | `is_archive` | Tests `"!<arch>\n"` / `"!<thin>\n"` magic | 629, 789 |
| `0x4BDAC0` | 48 B | `archive_open` | Opens `.a` archive, allocates iterator context | 858 |
| `0x4BDAF0` | 48 B | `archive_next` | Advances to next member in archive | 862 |
| `0x4BDB30` | 48 B | `archive_close` | Closes archive, destroys context | 881 |
| `0x4BDB60` | 8 B | `archive_get_path` | Returns current member's `"archive:member"` path | 866 |
| `0x4BDB70` | — | `classify_member` | Content probe for archive/host-ELF members | 803, 867 |
| `0x4298C0` | 476 B | `extract_module_ids` | Parses `"def <name>\0"` entries from host ELF | 816, 877 |
| `0x4297B0` | — | `check_elflink_error` | Tests elfLink return code, emits fatal on error | 709, 805, 859, etc. |
| `0x4644C0` | — | `list_append` / `set_insert` | Appends node to linked list / set | 885, 938 |
| `0x464A80` | — | `set_begin` | Returns iterator to first element of set | 850 |
| `0x464A90` | — | `set_is_end` | Tests if iterator is past end | 852 |
| `0x464AA0` | — | `set_next` | Advances set iterator | 850 |
| `0x464AC0` | — | `set_get_value` | Returns value at current iterator position | 891 |
| `0x4632F0` | — | `path_matches` | Compares two file paths | 892 |
| `0x431000` | 4.7 KB | `arena_free` | Arena deallocator | 692, 752, 763, 782, 801, 839, 849, 872 |
| `0x426AA0` | — | `arena_alloc` | Arena allocator | 925, 934 |
| `0x44F410` | ~2 KB | `tls_get_state` | TLS state block (error byte at offset +1) | 909 |
| `0x467460` | 1,552 B | `diag_emit` | Diagnostic emission entry point | 610, 653, 655, 751, 767, 785, 913 |
| `0x45CCD0` | 12 B | `timing_start` | Begin profiling timer | 696, 984 |
| `0x45CCE0` | 52 B | `timing_stop` | Stop timer, compute elapsed | 712 |
| `0x432340` | 255 B | `csv_write` | Write profiling CSV row | 714 |

## Global Variables

| Address | Type | Name | Role |
|---|---|---|---|
| `qword_2A5F330` | `void *` | input file list head | Root of the linked list iterated by the input loop |
| `qword_2A5F328` | `void *` | raw input file list | Unprocessed input list (before library expansion) |
| `qword_2A5F2F0` | `void *` | processed archives set | Set of archive paths already processed (dedup) |
| `qword_2A5F290` | `void *` | profiling context | Non-null when profiling/CSV timing is enabled |
| `qword_2A5F318` | `void *` | profiling aux data | Second parameter to CSV writer |
| `qword_2A5F2E0` | `char *` | register-link-binaries path | Non-null when `--register-link-binaries` is set |
| `byte_2A5F288` | `uint8_t` | lto flag | Set by `-lto`; gates NVVM IR and LTO IR acceptance |
| `byte_2A5F212` | `uint8_t` | host\_object\_seen | Set to 1 when any host ELF object is encountered |
| `byte_2A5F222` | `uint8_t` | mercury mode | Set when sm >= 100; affects fatbin member type dispatch |
| `byte_2A5F225` | `uint8_t` | sass/capmerc mode | Set when sm > 89; gates FNLZR post-link transform |
| `byte_2A5F29B` | `uint8_t` | verbose-keep | When set, compiled cubins written to disk |
| `byte_2A5F2C0` | `uint8_t` | optimization level | Passed to ptxas JIT: -O0/-O1/-O2/-O3 |
| `byte_2A5F310` | `uint8_t` | debug flag | Passed to ptxas JIT: -g |
| `dword_2A5F314` | `uint32_t` | sm version | Target architecture; used for arch validation |
| `dword_2A5F30C` | `uint32_t` | address size | 32 or 64; passed to ptxas as `dword_2A5F30C == 64` |
| `dword_2A5B528` | `uint32_t` | ptxas options table | Base of ptxas option table passed to JIT |
| `dword_2A5F280` | `uint32_t` | ir module count | Number of IR modules registered; checked post-loop |

## Cross-References

- [File Type Detection](../input/file-type-detection.md) — Detailed coverage of the 56-byte probe, magic number classification, and the extension-vs-content dispatch priority
- [Cubin Loading](../input/cubin-loading.md) — `sub_43D970` (is\_elf), `sub_43DA40` (is\_sass\_cubin), `sub_43E100` (load\_cubin\_from\_file), `sub_43D9B0` (is\_relocatable)
- [Fatbin Extraction](../input/fatbin-extraction.md) — Deep dive into `sub_42AF40` and fatbin container format, member type codes, LZ4 decompression
- [Archive Processing](../input/archives.md) — Archive member iteration (`sub_4BDAC0`/`sub_4BDAF0`/`sub_4BDB30`/`sub_4BDB60`), whole-archive semantics, thin archive support
- [PTX Input & JIT](../input/ptx-input.md) — The embedded ptxas compilation path via `sub_4BD760`, null-terminated loading, profiling
- [NVVM IR / LTO IR Input](../input/nvvm-ir-input.md) — IR module registration via `sub_427A10` and LTO prerequisites
- [Host ELF Embedding](../input/host-elf.md) — Host `.o`/`.so` handling, embedded fatbin detection, module-id extraction via `sub_4298C0`
- [ELF Parsing](../input/elf-parsing.md) — Low-level ELF header access functions used by the probe
- [Entry Point & Main](entry.md) — The containing `main()` function and overall pipeline structure
- [Mode Dispatch](mode-dispatch.md) — How the overall compilation mode affects dispatch behavior
- [Library Resolution](library-resolution.md) — How the input list at `qword_2A5F330` is constructed from `-l` flags and search paths
- [CLI Options](cli-options.md) — `--verbose`, `--verbose-keep`, `-lto`, `--register-link-binaries` and other flags affecting input processing
- [Merge Phase](merge.md) — Where cubin objects go after dispatch; the `v353` module list is the merge input
- [LTO Overview](../lto/overview.md) — The batch LTO compilation that consumes registered IR modules after the input loop
- [Mercury / FNLZR](../mercury/fnlzr.md) — `sub_4275C0` post-link binary rewriter invoked for SASS cubins on sm > 89
- [Symbol Resolution](../linker/symbol-resolution.md) — How input order affects first-definition-wins and weak-vs-strong resolution
- [Error Reporting](../infra/error-reporting.md) — `sub_467460` (diag\_emit) and `sub_44F410` (TLS error state)
- [Timing Infrastructure](../infra/timing.md) — `sub_45CCD0`/`sub_45CCE0`/`sub_432340` profiling calls during PTX compilation
- [Memory Arenas](../infra/memory-arenas.md) — `sub_431000` (arena\_free) and `sub_426AA0`/`sub_4307C0` (arena\_alloc) used throughout

> For ptxas pipeline internals (the embedded PTX-to-SASS JIT compiler invoked for `.ptx` inputs via `sub_4BD760`), see the [ptxas wiki](../../ptxas/pipeline/overview.html).

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| Input loop location: `main()` at `0x409800`, lines 595–901 | **HIGH** | Direct decompiled source read; loop entry at line 595 (`v73 = qword_2A5F330`), loop body 598–901, exit at LABEL\_135 |
| Extension-first dispatch (not magic-first) | **HIGH** | Decompiled lines 639–786 test extension strings before the `sub_487A90` archive content check at line 789 |
| Inline extension comparisons for "cubin" (6-byte), "fatbin" (7-byte), "ptx" (3-char) | **HIGH** | Visible as hand-coded memcmp/character-test loops at lines 639–650, 737–748, 681–690 |
| strcmp for "nvvm" and "ltoir" at line 761 | **HIGH** | `!strcmp(s1, "nvvm") \|\| !strcmp(v84, "ltoir")` directly visible |
| Linked list node: +0 = next, +8 = path | **HIGH** | `v73[1]` at line 601 (path), `*v73` at line 905 (next) |
| Whole-archive semantics (unconditional member iteration) | **HIGH** | Lines 860–880: `while(1) { archive_next; if (!s1) break; sub_42AF40(...); }` — no symbol check |
| cudadevrt deferral at lines 854 and 895–900 | **HIGH** | `if (v353 \|\| !strstr(v74, "cudadevrt"))` visible at line 854; `if (!v353) { if (strstr(...)) goto LABEL_131; }` at 895–900 |
| libcudadevrt IR injection at lines 922–938 | **HIGH** | `sub_427A10(v55, v365, v366, "libcudadevrt")` at 924; 80-byte alloc, `strcpy(v111, "libcudadevrt")`, `sub_4644C0(v108, &v353)` at 925–938 |
| Error accumulation: error byte at TLS+1, checked at line 909 | **HIGH** | `*(_BYTE *)(sub_44F410(v64) + 1)` at line 909 -> `goto LABEL_271` -> `exit(-1)` at line 1685 |
| `sub_4297B0` is elfLink error checker | **HIGH** | Called after every archive/member operation with return value and filename; pattern matches error-check-and-emit |
| Mercury FNLZR condition: sm > 89 at line 721 | **HIGH** | `if ((unsigned int)dword_2A5F314 <= 0x59) goto LABEL_185` — `0x59` = 89 decimal |
| Archive dedup set at `qword_2A5F2F0` | **HIGH** | `sub_464A80(qword_2A5F2F0)` at line 850, `sub_4644C0(v74, &qword_2A5F2F0)` at 885 |
| `v55[64] & 1` is verbose flag | **HIGH** | Controls `fprintf(stderr, ...)` at lines 606, 841, 940; matches `--verbose` CLI option |
| `v55[64] & 0x20` is timing trace flag | **HIGH** | Controls `sub_4279C0` calls at lines 590, 1402, 1425 etc.; documented in timing.md |
| 80-byte module record for libcudadevrt | **HIGH** | `sub_426AA0(80)` at line 925, followed by 20-dword zeroing loop |
| `.so` extension silently ignored | **HIGH** | Lines 793: `*s1 != 115 \|\| s1[1] != 111 \|\| s1[2]` (ASCII 's','o',NUL) -> skip |
| `.bc` always fatal | **HIGH** | Line 785: `sub_467460(&unk_2A5B670, "should never see bc files")` — unconditional |
| PTX timing via `qword_2A5F290` | **HIGH** | Lines 695–715: `if (qword_2A5F290) timing_start` / `timing_stop` / `csv_write` |
| All function addresses in Key Function Map table | **HIGH** | All verified against decompiled/ directory files |
| Fatbin member type codes (1=PTX, 8=NVVM, 16=mercury) | **MEDIUM** | Structural match from `sub_42AF40` decompiled code; type codes inferred from dispatch branches |
| `sub_4BDB70` as content classifier for archive members | **MEDIUM** | Called at lines 803 and 867 between member extraction and `sub_42AF40`; exact internal logic not fully traced |
