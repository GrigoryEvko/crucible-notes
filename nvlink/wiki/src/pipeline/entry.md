# Entry Point & Main

`main()` at `0x409800` is the single entry point for nvlink. It is 57,970 bytes (1,936 lines of decompiled pseudocode) and is declared `__noreturn` -- every execution path terminates with `exit(0)` or `exit(-1)`. The function orchestrates every phase of device linking: option parsing, file type dispatch, LTO compilation, ELF merging, shared memory layout, relocation, finalization, and output. It also handles the host-linker-script generation path, which skips device linking entirely.

## Function Signature

```c
void __fastcall __noreturn main(unsigned int argc, char **argv, char **envp);
```

## Key Globals

| Global | Type | Role |
|---|---|---|
| `dword_2A77DC0` | `int` | **Mode selector.** Set by `--gen-host-linker-script`. Value 0 = device link, 1 = write host linker script only, 2 = generate via `ld --verbose` |
| `qword_2A5F330` | `void *` | **Input file list head.** Singly-linked list of input files. Each node: `[0]=next`, `[8]=filename_string` |
| `qword_2A5F318` | `char *` | **Architecture name string.** The `--arch` value as a string (e.g., `"sm_90"`) |
| `dword_2A5F314` | `int` | **SM version number.** Numeric arch (e.g., 90 for sm_90, 100 for sm_100). Used for Mercury threshold checks (`> 0x59` = sm_90+, `> 0x63` = sm_100+) |
| `byte_2A5F288` | `byte` | **LTO enabled flag.** Set when `--lto` / `-lto` is passed. Controls whether NVVM IR inputs are accepted and whether the LTO compilation pipeline runs |
| `dword_2A5F30C` | `int` | **Machine word size.** Either 32 or 64 (from `--machine`/`-m`) |
| `byte_2A5F225` | `byte` | **Mercury-capable flag.** Set when SM > 99. Enables Mercury (capsule mercury) post-link path |
| `byte_2A5F222` | `byte` | **Mercury mode flag.** Set when SM > 99. Controls Mercury-specific ELF emission and FNLZR invocation |
| `byte_2A5F224` | `byte` | **SM > 72 flag.** Indicates "modern" architecture. Changes ELF class from 7 to 8 |
| `byte_2A5F310` | `byte` | **Debug flag.** Set by `-g` / `--debug` |
| `byte_2A5F2D8` | `byte` | **Verbose flag.** Set by `-v` / `--verbose` |
| `byte_2A5F29B` | `byte` | **Verbose-keep flag.** Set by `--verbose-keep` / `-vkeep`. Dumps intermediate files and prints command lines |
| `byte_2A5F286` | `byte` | **Partial LTO flag.** When LTO produces relocatable output (not whole-program), this is set to 1 |
| `byte_2A5F2C2` | `byte` | **Relocatable link flag.** Set by `--relocatable-link` / `-r` |
| `qword_2A5F290` | `void *` | **Timing context.** Non-NULL when timing is enabled; timestamps are collected per phase |
| `qword_2A5F2E0` | `char *` | **Register-link-binaries output path.** Set by `--register-link-binaries`; triggers `DEFINE_REGISTER_FUNC()` header generation |

## Phase-by-Phase Walkthrough

### Phase 0: Initialization (lines 373--425)

```text
arena_create("nvlink option parser")     --> v338
arena_create("nvlink memory space")      --> v339
arena_snapshot(v339)                     --> v340
timer_init(&v356)
nullsub_1(*argv)                         // no-op (stripped debug hook)
arena_snapshot(v338)                     --> v3
nvlink_parse_options(argc, argv)         // sub_427AE0
```

Two named memory arenas are created. The "option parser" arena holds all option-related allocations and is destroyed at cleanup. The "memory space" arena holds all linker working data. `sub_45CAE0` takes a snapshot of the arena state, enabling later rollback or statistics.

After option parsing, if `dword_2A77DC0` indicates device-link mode (value 0 or > 2), the function proceeds to library resolution (lines 386--424). It creates a library search context via `sub_4622D0`, adds explicit `-L` paths from `qword_2A5F300`, reads `$LIBRARY_PATH` from the environment, and resolves all `-l` library references via `sub_462870` (path search). Resolved library paths are appended to the input file list `qword_2A5F330`.

### Mode Dispatch (line 385)

```c
if ((unsigned int)(dword_2A77DC0 - 1) > 1)
    // device link mode (value 0 or >= 3)
    goto device_link_path;
else
    // host linker script mode (value 1 or 2)
    goto host_linker_script_path;
```

`dword_2A77DC0` controls the top-level mode. Values:
- **0**: Normal device link. Processes input files, merges ELFs, emits device cubin.
- **1**: Write a minimal host linker script containing `.nvFatBinSegment`, `__nv_relfatbin`, and `.nv_fatbin` section directives. No device linking occurs.
- **2**: Generate a host linker script by running `ld --verbose` to extract the default script, then appending the NVIDIA sections. Uses `collect2` detection via shell pipeline.

### Phase 1: ELF Writer Creation (lines 426--593)

A secondary gate at line 426 checks `qword_2A5F1D0`. When non-NULL (set by `--gen-host-linker-script` with explicit object inputs), execution skips device linking entirely and falls through to the host linker script path at line 1742. When NULL (normal case), execution enters `LABEL_24`.

Reached via `LABEL_24`. Creates the output ELF wrapper (`elfw`) via `sub_4438F0`:

```c
elfw_create(
    type           = (byte_2A5F1E8 == 0) + 1,   // 2=ET_EXEC (default), 1=ET_REL (when -r is set)
    is_64bit       = (dword_2A5F30C == 64),
    elf_class      = (byte_2A5F224 != 0) + 7,    // 7 for legacy, 8 for sm>72
    sm_version     = dword_2A5F314,
    debug_flag     = byte_2A5F310,
    cuda_api_ver   = sub_468560(),
    verbose_flags  = dword_2A5F308,
    merge_flags    = v44,     // bitfield assembled from ~15 option flags
    mercury_flag   = byte_2A5F225
) --> v55 (the elfw object, used throughout)
```

The `merge_flags` bitfield `v44` is assembled from multiple option flags:

| Bit | Source | Meaning |
|---|---|---|
| 0 (and 10, 18) | base | always set on the normal link path -- initial value `0x40401` (corresponds to `elfw+84` "callgraph enabled") |
| 1 | `byte_2A5F2CE` | preserve-relocs (corresponds to `elfw+85`) |
| 2 | `byte_2A5F2CD` | reserve-null-pointer (derived effective flag; corresponds to `elfw+87`) |
| 3 | `byte_2A5F2CC` | allow-undefined-globals (corresponds to `elfw+88`) |
| 4 | `byte_2A5F2AA` | force-rela (corresponds to `elfw+89`, also OR'd with mercury/forced-relocatable) |
| 5 | `byte_2A5F2A9` | no-opt (corresponds to `elfw+90`) |
| 6 | `byte_2A5F299` | suppress-stack-size-warning (corresponds to `elfw+92`) |
| 8 | `byte_2A5F289` | extra-warnings (corresponds to `elfw+93`) |
| 9 | `byte_2A5F226` | `--suppress-debug-info` (corresponds to `elfw+86`). Direct decompile verification at `main_0x409800.c:365-369`: `if (byte_2A5F226) BYTE1(v45) |= 2` sets bit 9. CLI registration confirmed at `sub_427AE0:258` (`register_command_flag(_, "suppress-debug-info", &byte_2A5F226, 1u)`). The `--device-stack-protector` CLI option lives at `byte_2A5F1FE` (registered at `sub_427AE0:268`), consumed by `sub_429BA0:240`, and does **not** feed `merge_flags`. The legacy `stack_protector` semantic label on `elfw+86` is therefore a misnomer — see [elf-writer.md](../structs/elf-writer.md#metadata-and-flags-offsets-64103) for the resolved naming. |
| 11 | `byte_2A5F213` or `byte_2A5F212` | use-host-info or ignore-host-info (corresponds to `elfw+96`). Byte addresses per [cli-options.md global map](cli-options.md#global-variable-map); `byte_2A5F215`/`byte_2A5F216` are the `dump-callgraph-no-demangle` / `dump-callgraph` pair. |
| 12 | `byte_2A5F210` | disable-smem-reservation (the *complement* is stored at `elfw+99` as `std_smem_mode`). Per [cli-options.md](cli-options.md#linking-behavior-options), `byte_2A5F210` is the CLI byte for `--disable-smem-reservation`; the closely-named `--enable-extended-smem` lives at `byte_2A5F1FD` and feeds bit 25. Earlier wiki revisions called this `enable-extended-smem` at this slot, which mixed up the two adjacent smem-related options. |
| 14 | `byte_2A5F2A8` | optimize-data-layout (corresponds to `elfw+91`) |
| 15 | `byte_2A5F224` | sm > 72 flag (corresponds to `elfw+101` BYTE = `is_device_elf` gate) |
| 20 | `byte_2A5F222` | mercury mode (also forces relocatable path) |
| 25 | `byte_2A5F1FD` | enable-extended-smem. Per [cli-options.md](cli-options.md#linking-behavior-options), `byte_2A5F1FD` is the CLI byte for `--enable-extended-smem`; the `fdcmpt` flag lives at `byte_2A5F228`. Earlier wiki revisions labelled this row `fdcmpt`, which conflicted with the cli-options global map. |

Bit attribution is recovered from `main_0x409800.c` lines 338--389 -- the consecutive `if (cli_byte) v41 |= bit;` assembly. Earlier wiki revisions reused a stale table that swapped the bit-1/bit-2/bit-4 trio (preserve-relocs / reserve-null / force-rela) and conflated bit 5/6/14 (no-opt / suppress-stack-warn / optimize-data-layout); the wrong mappings propagated through `structs/elf-writer.md`, `structs/linker-context.md`, and `linker/data-layout-opt.md` and have been corrected.

After ELF creation, Mercury mode sets `elfw[104] = 2`; non-Mercury sets it to 0 or 1 based on `byte_2A5F225`. Additional setup:

- **nvvmpath/libdevice loading** (line 513): If `byte_2A5F288` (LTO enabled), loads `libdevice` from `qword_2A5F278 + "/lib64"` via `sub_4BC470`.
- **Stack canary setup** (line 526): If `dword_2A5F2C8`, calls `sub_4389F0` for device stack protector initialization.
- **maxrregcount** (line 528): Propagates the per-arch register limit.
- **kernels-used / variables-used** (lines 535--538): Calls `sub_43F360` / `sub_43F950` to load the used-symbol lists.
- **UIDX file** (line 541): If `qword_2A5F208` is set, loads the unified index file via `sub_476BF0`.
- **Host info ELF** (line 551): If `qword_2A5F1F0` is set, loads the host info ELF.
- **Mercury version info** (line 560): For SM > 72, writes version string `"Cuda compilation tools, release 13.0, V13.0.88"` and build string into the ELF.
- **Timing trace** (line 592): If verbose timing is enabled (`v55[64] & 0x20`), emits `"init"` trace point.

### Phase 2: Input File Loop (lines 595--1741)

The core input processing loop iterates the linked list `qword_2A5F330`:

```c
v73 = (QWORD *)qword_2A5F330;
if (!qword_2A5F330) goto LABEL_135;   // no inputs, skip to post-input
while (1) {
    filename = v73[1];       // input filename string
    file = fopen(filename, "rb");
    fread(header, 1, 56, file);   // read 56-byte header for identification
    fclose(file);
    // ... dispatch by file type ...
    v73 = (QWORD *)*v73;    // advance to next input
    if (!v73) goto LABEL_135;
}
```

For each input, 56 bytes are read and the file type is determined by extension (stored in `s1` after `sub_462620` splits the path):

#### cubin (lines 639--677)

```c
if s1 == "cubin":
    validate_elf_magic(header)            // sub_43D970, checks ELF
    check_e_machine(header) == 190        // EM_CUDA
    if is_mercury_capable(header):        // sub_43DA40
        read_full_file(filename) --> cubin_data
        validate_and_add(elfw, cubin_data, filename, &is_mercury)
        if sm > 0x59 and needs_mercury_transform:
            post_link_transform(&cubin_data, filename, sm, ...)   // sub_4275C0
        validate_and_add(elfw, cubin_data, filename, NULL)
    else:
        read_full_file(filename) --> cubin_data
        validate_and_add(elfw, cubin_data, filename, 0)
    register_module(filename, cubin_data)     // sub_42A680
```

`sub_426570` (`elfw_validate_arch_and_merge`) validates that the cubin matches the target architecture. `sub_4275C0` (`post_link_transform` / FNLZR) runs the Mercury finalizer for SM >= 90.

#### PTX (lines 679--736)

```c
if s1 == "ptx":
    mmap_file(filename) --> ptx_data
    if timing_enabled: start_timer()
    ptxas_compile(
        &cubin_out, ptx_data, sm_version, debug, is_64bit,
        debug_flag, arch_string, compiler_flags
    )  --> exit_code                       // sub_4BD760
    check_exit_code(exit_code, filename)
    if timing_enabled: stop_timer(); record("nvlink", qword_2A5F318)
    if verbose_keep: dump_cubin(cubin_out)
    if sm > 0x59:
        validate_and_add(elfw, cubin_out, filename, &is_mercury)
        if needs_mercury_transform:
            post_link_transform(&cubin_out, filename, sm, ...)
        validate_and_add(elfw, cubin_out, filename, NULL)
    else:
        validate_and_add(elfw, cubin_out, filename, 0)
    register_module(filename, cubin_out)
```

PTX inputs trigger the embedded ptxas backend (`sub_4BD760`). The compiled cubin is then treated identically to a cubin input for merge purposes.

#### fatbin (lines 737--759)

```c
if s1 == "fatbin":
    validate_magic(header) == 0xBA55ED50
    mmap_file(filename) --> fatbin_data
    extract_and_process_fatbin(fatbin_data, 0, filename, elfw, ...)   // sub_42AF40
```

Fatbin processing is delegated to `sub_42AF40`, which iterates archive members and recursively processes each (cubin, PTX, NVVM IR, or capsule mercury).

#### NVVM IR / LTO IR (lines 761--778)

```c
if s1 == "nvvm" or s1 == "ltoir":
    assert(byte_2A5F288)    // "should only see nvvm files when -lto"
    mmap_file(filename) --> ir_data
    lto_add_module(elfw, ir_data, ir_size, filename)   // sub_427A10
```

NVVM and LTO IR inputs are only accepted when `-lto` is active. They are registered for later LTO compilation.

#### bc (Bitcode) (lines 780--787)

```c
if s1 == "bc":
    fatal_error("should never see bc files")    // always aborts
```

Raw LLVM bitcode is explicitly rejected.

#### Archives (.a) (lines 849--901)

```c
if s1 is an archive:
    for each library-path pattern in qword_2A5F2F0:
        if filename matches pattern: process archive
    open_archive(filename) --> archive_ctx      // sub_4BDAC0
    while get_next_member(archive_ctx) --> member:
        extract_member(archive_ctx) --> member_data  // sub_4BDB60, sub_4BDB70
        extract_and_process_fatbin(member_data, ...)
    close_archive(archive_ctx)                  // sub_4BDB30
```

Archives are iterated member-by-member. Each member is processed through `sub_42AF40` (fatbin extraction), handling nested cubin objects.

Special handling: if no `cudadevrt` object has been seen yet (`v353 == NULL`) and the archive path contains `"cudadevrt"`, the archive is silently ignored (deferred until LTO determines whether it is needed).

#### Host ELF / Shared Object (lines 789--847)

Files with extension `"so"` or unrecognized ELF files that are not device ELFs are silently ignored with a verbose message: `"ignore input %s"`.

### Phase 3: LTO Compilation (lines 910--1367)

After the input loop, if LTO is enabled (`byte_2A5F288`) and IR modules were collected:

```c
if byte_2A5F288 and no_ir_collected:
    warn_and_disable_lto()
    byte_2A5F288 = 0

// Validate LTO option conflicts
check_lineinfo_conflict()          // -lineinfo incompatible with LTO
check_maxrregcount_conflict()      // -maxrregcount validation
check_math_option_conflicts()      // -ftz, -prec-div, -prec-sqrt, -fmad, -split-compile

// NVVM callback setup (verbose-keep mode)
if byte_2A5F29B:
    handle = dlsym(elfw->nvvm_lib, "__nvvmHandle")
    callback_fn = handle(0xBEEF)   // magic cookie
    callback_fn(elfw->nvvm_ctx, sub_4299E0, 0, 0xF00D)  // register callback

// Collect IR modules
lto_collect_ir(linker_state, &module_list, &module_count)  // sub_426CD0

// Compile IR to PTX
lto_compile(
    &ptx_out, &ptx_size, &cubin_out,
    &compile_status, &partial_flag, &error_msg,
    elfw, module_count, ir_modules
) --> exit_code                                            // sub_4BC6F0
```

The LTO compilation has three dispatch paths depending on the result:

1. **Whole-program compile** (`byte_2A5F286 == 0`): A single PTX is produced. `sub_4BD4E0` assembles it into a cubin. The cubin is written to the output file directly.

2. **Single-module relocatable** (`dword_2A5B514 == 1`): `sub_4BD760` compiles the single PTX module into a cubin in relocatable mode.

3. **Split-compile** (multiple modules): A thread pool is created via `sub_43FDB0`. Each PTX module is dispatched to `sub_4264B0` for parallel compilation. The thread pool uses `sub_43FF50` (enqueue), `sub_43FFE0` (wait), and `sub_43FE70` (join). After all threads complete, each compiled cubin is merged into the output ELF.

```c
// Split compile path
thread_pool = create_thread_pool(cpu_count)    // sub_43FDB0
for i in 0..module_count:
    work_item = { output_ptr, ptx_data, sm_version, ... }
    enqueue(thread_pool, sub_4264B0, work_item)
wait_all(thread_pool)                          // sub_43FFE0
join_all(thread_pool)                          // sub_43FE70
for i in 0..module_count:
    validate_and_add(elfw, cubin[i], "lto.cubin", ...)
    if sm > 0x59: post_link_transform(...)
    merge_elf(elfw)
```

After LTO compilation, `libcudadevrt` handling: if whole-program LTO consumed all inputs, the `cudadevrt` archive is removed from the module list (line 1349): `"LTO on everything so remove libcudadevrt from list"`.

### Phase 4: Register-Link-Binaries List Maintenance (lines 1371--1401)

If `--register-link-binaries` is set (`qword_2A5F2E0`), module-ID records from the per-input registration (`sub_42A680`) are matched against the module list `v353`. Matching entries are removed from `v353` to avoid double-registration. The remaining entries are freed.

### Phase 5: Merge Loop (lines 1402--1607)

After all inputs are processed and LTO compilation is complete:

```c
trace("read")
v353 = reverse_list(v353)          // sub_4649E0

// Verbose-keep: print nvlink command reconstruction
if byte_2A5F29B:
    printf("nvlink -link -arch=%s -m%d %s -o %s\n", ...)

// Iterate module list and merge each cubin into the output ELF
for each module in v353:
    // Mercury pre-link transform for SM > 99
    if byte_2A5F221 and byte_2A5F220:
        check_elf_type(module->cubin)
        if needs_transform:
            post_link_transform(&module->cubin, module->name, sm, ...)

    // Skip cudadevrt if not needed
    if !byte_2A5F2C2 and strstr(module->name, "cudadevrt"):
        if !has_device_refs(elfw):    // sub_4448C0
            printf("ignore %s\n", module->name)
            free_module(module)
            continue

    // Merge the cubin into the output ELF
    merge_elf(elfw)                   // sub_45E7D0, 89KB function
    if error: fatal("merge_elf failed")
```

The merge loop calls `sub_45E7D0` (`merge_elf`, 89,156 bytes) for each input cubin. This is the core linking operation that merges sections, resolves symbols, and combines relocations.

### Phase 6: Layout / Relocate / Finalize / Write (lines 1424--1491)

After all inputs are merged:

```c
trace("merge")

// Dead code elimination (optional)
if byte_2A5F214 and (!byte_2A5F288 or byte_2A5F285):
    dead_code_eliminate(elfw, v353)     // sub_426AE0

// Shared memory layout + entry property computation
shared_memory_layout(elfw)             // sub_439830 (65KB)
trace("layout")

// Apply relocations
apply_relocations(elfw)                // sub_469D60 (26KB)
trace("relocate")

// Finalize ELF (final relocation pass + section finalization)
finalize_elf(elfw)                     // sub_445000 (55KB)
trace("finalize")

// Verbose memory stats
if byte_2A5F2D8:
    dump_verbose_stats(elfw)           // sub_43D2A0

// Write output
if no_errors:
    output_file = fopen(filename, "wb")
    if byte_2A5F222:   // Mercury mode
        // Serialize to buffer, then run FNLZR post-link transform
        buf_size = elfw_calc_size(elfw)          // sub_45C980
        buffer = arena_alloc(buf_size)           // sub_4307C0
        elfw_write_to_buffer(buffer, elfw)       // sub_45C950
        post_link_transform(&buffer, filename, sm, &out_size, 1)  // sub_4275C0
        fwrite(buffer, 1, out_size, output_file)
    else:
        elfw_write_to_file(output_file, elfw)    // sub_45C920
    fclose(output_file)
```

The four pipeline stages execute sequentially with timing trace points between them:
1. `sub_439830` -- shared memory layout (65,776 bytes, handles global/extern/local/reserved shared memory allocation, overlap set analysis)
2. `sub_469D60` -- relocation application (26,578 bytes, resolves R_CUDA relocations, handles UDT/UFT unified table relocations)
3. `sub_445000` -- finalization (55,681 bytes, final relocation pass, section content generation)
4. `sub_45C920` / `sub_45C950` -- ELF serialization (13,258 bytes, writes headers, sections, program headers)

For Mercury targets (SM >= 100), the serialized ELF is passed through `sub_4275C0` (the FNLZR finalizer) as a post-link transform before writing to disk. This converts the SASS cubin into the capsule mercury format.

### Phase 7: Cleanup and Exit (lines 1609--1688)

```c
// Cleanup module list
for each module in v353:
    free(module->cubin_data)

// Register-link-binaries output
if qword_2A5F2E0:
    file = fopen(qword_2A5F2E0, "w")
    fprintf(file, "#define NUM_PRELINKED_OBJECTS %d\n", count)
    for each module:
        fprintf(file, "DEFINE_REGISTER_FUNC(%s)\n", module->name)
    fclose(file)

// Callgraph DOT file
if qword_2A5F2D0:
    file = fopen(qword_2A5F2D0, "w")
    callgraph_dump_dot(file)           // sub_44CCF0
    fclose(file)

trace("write")

// Free module list, timer, temp files
free_module_list(v353)                 // sub_464520
timer_cleanup(&v356)                   // sub_43D8E0
if byte_2A5F29C: cleanup_temp()        // sub_468470

// Destroy arenas
arena_destroy(v338, 0)                 // option parser arena
elfw_destroy(elfw)                     // sub_4475B0
arena_snapshot(v340, 0)                // restore memory space snapshot
arena_destroy(v339, 0)                 // memory space arena

// Exit
if has_errors: exit(-1)
else:          exit(0)
```

### Host Linker Script Path (lines 1742--1935)

When `dword_2A77DC0` is 1 or 2, main skips device linking entirely:

**Mode 1** (simple script): Writes a fixed linker script directly to the output file or stdout:
```text
SECTIONS
{
    .nvFatBinSegment : { *(.nvFatBinSegment) }
    __nv_relfatbin : { *(__nv_relfatbin) }
    .nv_fatbin : { *(.nv_fatbin) }
}
```

**Mode 2** (ld-derived script): Constructs a shell pipeline to extract the system linker's default script, then appends the NVIDIA sections:
```bash
gcc -v 2>&1 | grep collect2 | grep -wo -e -pie -e "-z ..." -e "-m ..." | tr "\n" " "
ld --verbose $(flags) | grep -Fvx -e "$(ld -V)" | sed '1,2d;$d' > output_file
ld -T output_file 2>&1 | grep 'no input files' > /dev/null   // validation
```

The `collect2` detection pipeline extracts host linker flags (PIE, machine model, etc.) from GCC's verbose output, then uses `ld --verbose` to dump the default linker script. If the output filename (`::filename`) is NULL, it writes to `/dev/stdout`.

## Overall Pseudocode Structure

```c
main(argc, argv, envp):
    // Phase 0: Init
    option_arena  = arena_create("nvlink option parser")
    memory_arena  = arena_create("nvlink memory space")
    parse_options(argc, argv)

    if mode == DEVICE_LINK:
        resolve_libraries()

        // Phase 1: Create output ELF
        elfw = elfw_create(arch, flags, ...)
        load_libdevice_if_lto()
        setup_stack_canary_if_enabled()
        load_uidx_file_if_set()

        // Phase 2: Input file loop
        for each input_file in file_list:
            header = read_56_bytes(input_file)
            ext = get_extension(input_file)
            switch ext:
                "cubin":  validate_elf -> merge_or_transform -> register
                "ptx":    ptxas_compile -> merge_or_transform -> register
                "fatbin": extract_members -> recurse
                "nvvm":   assert_lto -> add_ir_module
                "ltoir":  assert_lto -> add_ir_module
                "bc":     fatal("should never see bc files")
                archive:  iterate_members -> extract_and_process
                default:  ignore

        // Phase 3: LTO compilation (if enabled)
        if lto_enabled and has_ir_modules:
            validate_option_conflicts()
            ir = collect_ir_modules()
            ptx = lto_compile(ir)
            if whole_program:   cubin = ptxas_whole(ptx)
            elif single_module: cubin = ptxas_reloc(ptx)
            else:               cubins = ptxas_split_parallel(ptx_modules)
            handle_cudadevrt_removal()

        // Phase 4: Module list cleanup
        cleanup_register_link_binaries()

        // Phase 5: Merge
        for each module in module_list:
            maybe_mercury_pre_transform(module)
            maybe_skip_cudadevrt(module)
            merge_elf(elfw, module)

        // Phase 6: Link pipeline
        dead_code_eliminate_if_enabled(elfw)
        shared_memory_layout(elfw)
        apply_relocations(elfw)
        finalize(elfw)
        write_output(elfw)

        // Phase 7: Cleanup
        write_register_binaries_header()
        write_callgraph_dot()
        destroy_arenas()
        exit(0 or -1)

    elif mode == HOST_SCRIPT_SIMPLE:
        write_fixed_linker_script()
        exit(0)

    elif mode == HOST_SCRIPT_LD_DERIVED:
        extract_host_linker_flags()
        run_ld_verbose()
        append_nvidia_sections()
        validate_script()
        exit(0 or -1)
```

## Mercury Path (SM >= 100)

For architectures with SM >= 100 (Blackwell and later), nvlink invokes the FNLZR (Finalizer) via `sub_4275C0` at up to three points:

1. **Per-input cubin** (lines 726--727): After validating and adding a cubin input, if `sm > 0x59` and `byte_2A5F225` is set, the cubin is transformed before the second merge pass.

2. **Per-LTO output** (lines 1267--1269, 1309--1313): Each cubin produced by LTO split compilation is finalized before merging.

3. **Final output** (lines 1481--1482): After the complete ELF is serialized to a buffer, the entire output is passed through FNLZR with `post_link=1` flag. This is the final Mercury transform that converts SASS cubin into capsule mercury format.

The FNLZR prints diagnostic messages when verbose: `"FNLZR: Input ELF: %s"`, `"FNLZR: Post-Link Mode"`, `"FNLZR: Pre-Link Mode"`, `"FNLZR: Starting %s"`.

## Exit Codes

| Code | Condition |
|---|---|
| `0` | Successful completion with no errors |
| `-1` (255) | Any error occurred. Checked via `sub_44F410` which returns error state from the arena metadata byte at offset +1 |

Error state is tracked in the arena metadata. The check `*(_BYTE *)(sub_44F410(ptr) + 1)` reads byte offset 1 of the arena control block, which serves as a global error flag. Any call to `sub_467460` (error emit) with a fatal or error severity sets this flag.

## Timing Trace Points

When `dword_2A5F308 & 0x20` is set (verbose timing), `sub_4279C0` records timestamps at these phase boundaries:

| Trace string | Phase boundary |
|---|---|
| `"init"` | After ELF writer creation, before input loop |
| `"read"` | After input loop and LTO, before merge |
| `"merge"` | After merge loop, before layout |
| `"layout"` | After `sub_439830` (shared memory layout), before relocate |
| `"relocate"` | After `sub_469D60` (relocation), before finalize |
| `"finalize"` | After `sub_445000` (finalization), before output |
| `"write"` | After output is written, before cleanup |
| `"cicc-lto"` | After LTO IR compilation completes |
| `"ptxas-lto"` | After LTO PTX-to-SASS compilation completes |

## Function Call Summary

| Address | Recovered name | Size | Called from line | Role |
|---|---|---|---|---|
| `0x432020` | `arena_create_named` | 2,161 B | 377--378 | Create named memory arena |
| `0x43D8C0` | `timer_init` | ~1 KB | 381 | Initialize timing context |
| `0x45CAE0` | `arena_snapshot` | ~1 KB | 379, 383, 425, 1681 | Snapshot/restore arena state |
| `0x427AE0` | `nvlink_parse_options` | 30,272 B | 384 | Parse all CLI options |
| `0x4622D0` | `library_search_create` | ~2 KB | 387 | Create library search context |
| `0x462500` | `library_search_add_path` | ~1 KB | 394 | Add `-L` path |
| `0x462870` | `path_search_library` | 4,905 B | 405, 408 | Search for library file |
| `0x4438F0` | `elfw_create` | 14,821 B | 485 | Create output ELF wrapper |
| `0x4BC470` | `libdevice_load` | ~1 KB | 519 | Load libdevice for LTO |
| `0x462620` | `path_split` | 3,579 B | 634 | Split filename into dir/base/ext |
| `0x43D970` | `validate_elf_magic` | ~1 KB | 652 | Check ELF magic bytes |
| `0x43DA40` | `is_mercury_capable` | ~1 KB | 656, 726, 824, 1267, 1310 | Check if cubin supports Mercury finalization |
| `0x43E100` | `read_full_file` | ~1 KB | 664, 830 | Read entire file into memory |
| `0x426570` | `elfw_validate_arch_and_merge` | 7,427 B | 666, 724, 832, 1258, 1307 | Validate arch and add cubin |
| `0x42A680` | `register_module` | 11,939 B | 676 | Register module for link |
| `0x4BD760` | `ptxas_compile` | embedded | 699, 1190 | PTX-to-SASS compilation |
| `0x42AF40` | `extract_and_process_fatbin` | 11,143 B | 758, 809, 871 | Process fatbin archive |
| `0x427A10` | `lto_add_module` | ~2 KB | 777, 924 | Register IR module for LTO |
| `0x4BDAC0` | `archive_open` | ~1 KB | 858 | Open .a archive |
| `0x4BDAF0` | `archive_next_member` | ~1 KB | 862 | Get next archive member |
| `0x4BDB60` | `archive_member_name` | ~1 KB | 866 | Get member filename |
| `0x4BDB70` | `archive_extract_member` | ~1 KB | 867 | Extract member data |
| `0x426CD0` | `lto_collect_ir` | 7,040 B | 1010 | Collect IR modules for LTO |
| `0x4BC6F0` | `lto_compile` | embedded | 1014 | Compile IR via libnvvm |
| `0x4BD4E0` | `ptxas_whole_program` | embedded | 1165 | Whole-program PTX assembly |
| `0x43FD90` | `get_cpu_count` | ~1 KB | 1209 | Query available CPU count for thread pool |
| `0x43FDB0` | `thread_pool_create` | ~1 KB | 1210 | Create thread pool for split compile |
| `0x4264B0` | `split_compile_worker` | ~1 KB | 1238 | Per-module PTX-to-SASS worker function |
| `0x43FF50` | `thread_pool_enqueue` | ~1 KB | 1238 | Enqueue work item |
| `0x43FFE0` | `thread_pool_wait` | ~1 KB | 1252 | Wait for all threads |
| `0x43FE70` | `thread_pool_join` | ~1 KB | 1253 | Join thread pool |
| `0x4275C0` | `post_link_transform` | 3,989 B | 727, 835, 1269, 1313, 1481, 1503 | Mercury FNLZR finalizer |
| `0x45E7D0` | `merge_elf` | 89,156 B | 1272, 1586 | Merge input ELF into output |
| `0x426AE0` | `dead_code_eliminate` | 2,178 B | 1428 | DCE wrapper |
| `0x439830` | `shared_memory_layout` | 65,776 B | 1429 | Shared memory allocation |
| `0x469D60` | `apply_relocations` | 26,578 B | 1432 | Apply R_CUDA relocations |
| `0x445000` | `finalize_elf` | 55,681 B | 1436 | Final relocation + finalization |
| `0x45C980` | `elfw_calc_size` | ~1 KB | 1454 | Calculate serialized ELF size |
| `0x45C920` | `elfw_write_to_file` | ~1 KB | 1486 | Write ELF to file stream |
| `0x45C950` | `elfw_write_to_buffer` | ~1 KB | 1462 | Write ELF to memory buffer |
| `0x43D2A0` | `dump_verbose_stats` | ~1 KB | 1445 | Print verbose memory/merge stats |
| `0x476BF0` | `mmap_file` | ~1 KB | 543, 553, 693, 753, 773, 856 | Memory-map file for reading |
| `0x4279C0` | `trace_phase` | ~1 KB | 593, 1403, ... | Record timing trace point |
| `0x43D8E0` | `timer_cleanup` | ~1 KB | 1673 | Destroy timing context |
| `0x468470` | `cleanup_temp_files` | ~1 KB | 1676 | Remove temporary files |
| `0x44CCF0` | `callgraph_dump_dot` | ~2 KB | 1667 | Write callgraph DOT file |
| `0x4475B0` | `elfw_destroy` | 3,023 B | 1680 | Destroy ELF wrapper |
| `0x431C70` | `arena_destroy` | 3,564 B | 1679, 1682 | Destroy memory arena |
| `0x4297B0` | `check_exit_code` | ~1 KB | 709, 805, 859, 863, 868, 882, 1256, 1284 | Check subprocess exit code, fatal on failure |
| `0x464520` | `free_module_list` | ~1 KB | 1400, 1528, 1672 | Free linked list of module records |
| `0x4649E0` | `reverse_list` | ~1 KB | 1404 | Reverse singly-linked list in place |
| `0x44F410` | `get_arena_ctrl` | ~1 KB | 909, 1125, 1447, 1587, 1683 | Get arena control block (error flag at offset +1) |
| `0x467460` | `error_emit` | ~2 KB | throughout | Variadic error/warning emission |

## See Also

### Pipeline Pages

- [Pipeline Overview](overview.md) -- high-level 14-phase pipeline diagram and data flow summary
- [CLI Option Parsing](cli-options.md) -- `nvlink_parse_options` internals and all 68 option registrations
- [Mode Dispatch](mode-dispatch.md) -- the `dword_2A77DC0` mode variable and host linker script paths
- [Library Resolution](library-resolution.md) -- how `-L`/`-l` flags build the input file list
- [Input File Loop](input-loop.md) -- the file type dispatch table inside `main()`
- [Merge Phase](merge.md) -- the 89KB `merge_elf` function called per input object
- [Layout Phase](layout.md) -- shared memory allocation and constant dedup after merge
- [Relocation Phase](relocate.md) -- `apply_relocations` bit-field patching
- [Finalization Phase](finalize.md) -- symbol/section reindexing and ELF header finalization
- [Output Writing](output.md) -- ELF serialization and Mercury FNLZR post-link

### Input Formats

- [File Type Detection](../input/file-type-detection.md) -- 56-byte probe and magic number classification
- [Cubin Loading](../input/cubin-loading.md) -- device ELF validation and architecture matching
- [Fatbin Extraction](../input/fatbin-extraction.md) -- `sub_42AF40` fatbin container processing
- [PTX Input & JIT](../input/ptx-input.md) -- embedded ptxas compilation via `sub_4BD760`
- [NVVM IR / LTO IR Input](../input/nvvm-ir-input.md) -- IR module registration for LTO

### Related Sections

- [LTO Overview](../lto/overview.md) -- the LTO sub-pipeline (collect IR, compile, split-compile)
- [Mercury / FNLZR](../mercury/fnlzr.md) -- FNLZR post-link transform for sm >= 100
- [ELF Writer Structure](../structs/elf-writer.md) -- the `elfw` data structure created in Phase 1
- [Error Reporting](../infra/error-reporting.md) -- `sub_467460` diagnostic system
- [Memory Arenas](../infra/memory-arenas.md) -- arena allocator backing all allocations

### Sibling Tool Wikis

> For ptxas pipeline internals (embedded PTX-to-SASS compilation invoked via `sub_4BD760`), see the [ptxas wiki](../../ptxas/pipeline/overview.html).

> For the cicc CUDA compiler whose libnvvm.so is loaded via `dlopen` for LTO compilation, see the [cicc wiki](../../cicc/pipeline/overview.html).

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `main()` at `0x409800`, 57,970 bytes, 1,936 lines | **HIGH** | Verified: `decompiled/main_0x409800.c` has exactly 1,936 lines |
| `__noreturn` with `exit(0)` / `exit(-1)` | **HIGH** | Both exit paths visible in `main_0x409800.c` at lines 1683 and 1935 |
| Arena creation strings `"nvlink option parser"` / `"nvlink memory space"` | **HIGH** | Exact strings found at lines 377--378 of `main_0x409800.c` |
| `dword_2A77DC0` mode selector (0/1/2) | **HIGH** | Variable referenced throughout `main_0x409800.c`; dispatch at line 385 and 1830 |
| Phase-by-phase line number references (Phase 0 lines 373--425, etc.) | **HIGH** | Cross-verified against `main_0x409800.c` during P031 task |
| 56-byte header read for file classification | **HIGH** | `fread(header, 1, 56, file)` pattern visible in main |
| Cubin: `"cubin not an elf?"` / `"cubin not a device elf?"` strings | **HIGH** | Found at lines 653 and 655 of `main_0x409800.c` |
| PTX: `sub_4BD760` ptxas compilation | **HIGH** | `decompiled/sub_4BD760_0x4bd760.c` exists |
| Fatbin: `"fatbin wrong format?"` at `0x42AF40` | **HIGH** | String at line 751 of main; `sub_42AF40_0x42AF40.c` exists |
| NVVM IR: `"should only see nvvm files when -lto"` | **HIGH** | Found at line 767 of `main_0x409800.c` |
| BC: `"should never see bc files"` | **HIGH** | Found at lines 784--785 of `main_0x409800.c` |
| LTO: `"LTO on everything so remove libcudadevrt from list"` | **HIGH** | Found at line 1350 of `main_0x409800.c` |
| Split-compile thread pool via `sub_43FDB0` | **HIGH** | `decompiled/sub_43FDB0_0x43fdb0.c` exists |
| `merge_elf` at `0x45E7D0`, 89,156 bytes | **HIGH** | `decompiled/sub_45E7D0_0x45e7d0.c` exists with 2,838 lines |
| `"merge_elf failed"` error string | **HIGH** | Found at line 1590 of `main_0x409800.c` |
| FNLZR at `sub_4275C0` for Mercury (sm >= 100) | **HIGH** | `decompiled/sub_4275C0_0x4275c0.c` exists |
| `DEFINE_REGISTER_FUNC` output | **HIGH** | Found at lines 1646 and 1648 of `main_0x409800.c` |
| Host linker script `SECTIONS` block | **HIGH** | Found at lines 1838, 1898, 1926 of `main_0x409800.c` with `.nvFatBinSegment` |
| All 60+ function addresses in the call summary table | **HIGH** | All verified to exist as files in `decompiled/` directory |
| `merge_flags` bitfield assembly (bits 0--25) | **MEDIUM** | Structural match from decompiled code; individual bit positions inferred from flag variable ordering |
| Phase numbering (0--7 vs overview's 1--14) | **MEDIUM** | This page uses a different numbering than overview.md; both are internally consistent but numbering is editorial choice |
| `elfw` field offsets (+8, +16, +48, +64, +104) | **MEDIUM** | Consistent across multiple pages; individual offsets inferred from decompiled pointer arithmetic |
