# LTO Overview

Link-Time Optimization (LTO) in nvlink v13.0.88 compiles NVVM IR into SASS machine code at link time instead of at translation-unit compile time. The design follows a delegation model: nvlink orchestrates the pipeline, `libnvvm.so` compiles IR to PTX, and an embedded ptxas backend assembles PTX into SASS. nvlink itself contains zero LLVM infrastructure -- no "LLVM" strings appear anywhere in the 26.2 MB binary. All IR-level optimization is offloaded to libnvvm via its public C API.

This page is the definitive entry point for understanding nvlink's LTO implementation. It documents the complete pipeline end-to-end: input collection, per-module option reconciliation, library injection, compilation dispatch, libnvvm API usage, PTX extraction, ptxas assembly, split-compilation thread pool mechanics, and result merge back into the linking pipeline. Every function address and control-flow decision point is traced from the decompiled binary.

## Architecture: The Delegation Model

```
  nvlink (orchestrator)
    |
    |  1. Collect NVVM IR from inputs
    |  2. dlopen("libnvvm.so")            <-- external shared library
    |  3. nvvmCreateProgram()
    |  4. nvvmAddModuleToProgram()         <-- for each IR module
    |  5. nvvmCompileProgram(opts)         <-- IR -> PTX
    |  6. nvvmGetCompiledResult()          <-- PTX text out
    |  7. nvvmDestroyProgram()
    |
    |  8. Feed PTX to embedded ptxas       <-- ~25 MB of compiler backend
    |     (ISel, regalloc, scheduling,      inside the nvlink binary
    |      encoding, ELF emission)
    |
    v
  Final SASS cubin (device ELF)
```

Three distinct software components participate, but only one binary is involved at runtime:

| Component | Location | Role |
|---|---|---|
| nvlink | The tool binary itself (~1.2 MB of linker code) | Orchestrates pipeline, manages inputs, performs ELF merging, relocation, finalization |
| libnvvm.so | External shared library loaded via `dlopen` | Compiles NVVM IR (LLVM bitcode) into PTX text. Contains the LLVM-based optimizer |
| Embedded ptxas | ~25 MB of compiler backend statically linked into the nvlink binary | Assembles PTX into SASS: parsing, ISel, register allocation, scheduling, encoding, ELF emission |

The key insight is that nvlink does not embed LLVM. It delegates IR optimization to libnvvm (which does contain LLVM), then uses its own embedded ptxas copy for the PTX-to-SASS compilation step. This is the same ptxas backend shared by the standalone `ptxas` tool and `cicc`.

## Complete End-to-End Pipeline

The following diagram traces the LTO pipeline from the first IR input to the final merged ELF output, annotated with exact function addresses and the globals that control each decision point.

```
 ============================================================================
 PHASE 7: INPUT LOOP  (main @ 0x409800, lines 600-920)
 ============================================================================

  Input file list (qword_2A5F330)
    |
    |  For each input file:
    |
    +-- Extension ".nvvm" or ".ltoir"?
    |     |
    |     |  YES: requires byte_2A5F288 (-lto) or fatal error:
    |     |       "should only see nvvm files when -lto"
    |     |
    |     +-> sub_476BF0(filename)         -- read file into memory
    |     +-> sub_427A10(ctx, data, size,  -- lto_add_module
    |     |              filename)            @ 0x427A10
    |     |     |
    |     |     +-> sub_4BC4A0(...)        -- nvvm_api_wrapper_init
    |     |     |     @ 0x4BC4A0             (dlopen + nvvmCreateProgram)
    |     |     +-> sub_4BD1F0(...)        -- nvvmAddModule + extract options
    |     |     |     @ 0x4BD1F0             (nvvmAddModuleToProgram)
    |     |     +-> ++dword_2A5F280        -- LTO module count
    |     |     +-> printf("nvlink -lto-add-module %s.nvvm\n")
    |     |
    |     +-> sub_42AF40(...)              -- process_input_object
    |           @ 0x42AF40                   (fatbin/ELF members with IR)
    |           |
    |           +-> Extract embedded option strings:
    |           |   "-ftz=", "-prec_div=", "-prec_sqrt=",
    |           |   "-fmad=", "-maxreg ", "-split-compile ",
    |           |   "-generate-line-info", "-inline-info"
    |           |
    |           +-> OPTION CONSENSUS STATE MACHINE
    |           |   (per-option 5-state tracker, see below)
    |           |
    |           +-> sub_42A680(...)        -- register_module
    |                 @ 0x42A680
    |                 |
    |                 +-> If cubin (a3 != 0) + byte_2A5F288:
    |                       byte_2A5F286 = 1  (force partial)
    |                       If NOT cudadevrt:
    |                         byte_2A5F285 = 1
    |                         warning: "requested LTO but '%s' not built
    |                                   for LTO so doing partial LTO"
    |
    +-- Fatbin member type 8 (NVVM IR)?
    |     |
    |     +-> Same path as above via sub_42AF40 -> sub_4BC4A0 -> sub_4BD1F0
    |
    +-- Extension ".bc"?
    |     +-> Fatal: "should never see bc files"
    |
    +-- Is "cudadevrt" in filename AND no LTO modules yet?
          +-> Skip (mark as ignorable)
          +-> If LTO covers everything later, strip from module list

 ============================================================================
 POST-INPUT-LOOP GATE  (main @ 0x409800, lines 911-920)
 ============================================================================

  byte_2A5F288 set AND dword_2A5F280 > 0?
    |
    NO ---> v342 = 1; goto LABEL_311 (skip LTO entirely)
    |
    YES -+
         |
         +-> If v365 (libcudadevrt IR found):
         |     sub_427A10(ctx, v365, v366, "libcudadevrt")
         |     Create 80-byte module node, strcpy "libcudadevrt"
         |     Prepend to module list v353
         |
         +-> fwrite("compile linked lto ir:\n", stderr)  [if verbose]

 ============================================================================
 PHASE 8a: OPTION CONSENSUS VALIDATION  (main, lines 945-982)
 ============================================================================

  For each per-module option (maxrregcount, ftz, prec-div, prec-sqrt,
  fmad, split-compile):
    |
    +-> State == 3 (some modules have, some don't)?
    |     Emit warning via sub_467460(&unk_2A5B5F0/2A5B5E0, "-<name>")
    |     "option present in some modules but not all"
    |
    +-> State == 4 (conflicting values across modules)?
    |     Emit error via sub_467460(&unk_2A5B600, "-<name>")
    |     "option values conflict across modules"
    |
    +-> maxrregcount special: if dword_2A5F22C == 0 (no CLI override):
          if state == 3: warn (some modules have it)
          if state == 4: error (conflicting values)
          dword_2A5F22C = dword_2A5F254  (use discovered value)

 ============================================================================
 PHASE 8b: CALLBACK REGISTRATION  (main, lines 985-1008)
 ============================================================================

  If byte_2A5F29B (--verbose-keep):
    |
    +-> dlsym(__nvvmHandle) from libnvvm.so handle
    |     v274 = __nvvmHandle
    |     If NULL: fatal "could not find __nvvmHandle"
    |
    +-> v279 = v274(0xBEEF)     -- magic cookie: retrieve callback handle
    |     If NULL: fatal "could not find CALLBACK Handle"
    |
    +-> v279(handle, sub_4299E0, 0, 0xF00D)
    |     Register callback: sub_4299E0 @ 0x4299E0
    |     (writes each post-link output file to disk with
    |      printf("nvlink -lto-post-link -o %s\n"))
    |     0xF00D = magic cookie for callback registration
    |     If error: fatal "error in LTO callback"

 ============================================================================
 PHASE 8c: OPTION COLLECTION  (sub_426CD0 @ 0x426CD0, 7040 bytes)
 ============================================================================

  v112 = sub_426CD0(v357, &v358, &v352)
    |
    |  Creates linked list of option strings, converts to char** array
    |
    |  ALWAYS EMITTED:
    |    "-arch=compute_<dword_2A5F314>"
    |    "-link-lto"
    |
    |  CONDITIONAL:
    |    "-split-compile-extended=N"     if dword_2A5B514 != 1
    |    "-split-compile=N"             if dword_2A5B518 != 1
    |    "-Ofast-compile=max|mid|min"   if qword_2A5F258 != NULL
    |    "-maxreg=N"                    if dword_2A5F22C > 0
    |    "-generate-line-info"          if byte_2A5F24C
    |    "-inline-info"                 if byte_2A5F244
    |    "--device-c"                   if byte_2A5F286 (partial mode)
    |    "--force-device-c"             if byte_2A5F285
    |    "-g"                           if byte_2A5F310 (debug)
    |
    |  HOST INFO (if byte_2A5F214 && !byte_2A5F285):
    |    sub_426AE0(ctx, modules)       -- lto_mark_used_symbols
    |    "-has-global-host-info"        if byte_2A5F211
    |
    |  --Xnvvm PASSTHROUGH (qword_2A5F230):
    |    Iterate all --Xnvvm options, split by space
    |    Skip duplicates of already-emitted options:
    |      "-link-lto", "-generate-line-info", "-inline-info",
    |      "--device-c", "--force-device-c", "-g",
    |      "-Ofast-compile=*", "-compile-time",
    |      "-has-global-host-info"
    |    Deduplicate: track seen_ftz, seen_prec, seen_fma flags
    |    If NOT seen via --Xnvvm, emit defaults:
    |      "-ftz=<dword_2A5F274>"
    |      "-prec-div=<dword_2A5B524>"
    |      "-prec-sqrt=<dword_2A5B520>"   (always, even if seen)
    |      "-fma=<dword_2A5B51C>"
    |
    |  Returns: char** array (v112), option count (v352)

 ============================================================================
 PHASE 8d: NVVM COMPILATION  (sub_4BC6F0 @ 0x4BC6F0, 13602 bytes)
 ============================================================================

  v341 = sub_4BC6F0(&src, &v361, &v362, &v351,
                     &byte_2A5F286, &v363,
                     *v357, v352, v112)
    |
    |  RESOLVE API FUNCTIONS (via dlsym from libnvvm.so handle):
    |    nvvmCompileProgram         nvvmGetCompiledResultSize
    |    __nvvmHandle               nvvmGetCompiledResult
    |    nvvmGetErrorString         nvvmGetProgramLogSize
    |    nvvmGetProgramLog          nvvmDestroyProgram
    |
    |  MAGIC COOKIES (via __nvvmHandle):
    |    0xB0BA (45242) -> nvvmGetCompiledResult multi-module variant
    |    0xF00D (61453) -> nvvmGetProgramLogSize/multi-module sizer
    |
    |  BUILD COMPILATION OPTIONS:
    |    Allocate array: 8 * (option_count + 8) bytes
    |    Copy all options from sub_426CD0 output
    |    Scan for "--force-device-c" presence
    |
    |    If host-info enabled (a7 + 97) && --force-device-c NOT present:
    |      Append up to 6 host-reference options:
    |        "-host-ref-ek=<path>"    (extern kernel refs)
    |        "-host-ref-ik=<path>"    (intern kernel refs)
    |        "-host-ref-ec=<path>"    (extern constant refs)
    |        "-host-ref-ic=<path>"    (intern constant refs)
    |        "-host-ref-eg=<path>"    (extern global refs)
    |        "-host-ref-ig=<path>"    (intern global refs)
    |      Paths retrieved via sub_43FBC0 from state offsets 520-560
    |
    |    If a7 + 98 (variables tracking):
    |      Append "-variables"
    |
    |  CALL nvvmCompileProgram(program_handle, option_count, options)
    |    |
    |    +-> Return 100: whole-program succeeded
    |    |   *a5 = 0  (byte_2A5F286 cleared -- whole mode)
    |    |
    |    +-> Return 0:  compilation succeeded, relocatable
    |    |   *a5 = 1  (byte_2A5F286 set -- partial mode)
    |    |
    |    +-> Other: error
    |        v93 = 1; error_string = nvvmGetErrorString(result)
    |
    |  RETRIEVE COMPILATION LOG:
    |    nvvmGetProgramLogSize -> if > 1 byte, allocate + retrieve
    |    Concatenate error string + log if both present
    |
    |  RETRIEVE COMPILED RESULT:
    |    If no error:
    |      nvvmGetCompiledResultSize -> allocate buffer
    |      If multi-module (v351 > 1):
    |        nvvmGetCompiledResult(0xB0BA) -> per-module sizes array
    |      nvvmGetCompiledResult -> PTX text(s)
    |      nvvmDestroyProgram
    |
    |  OUTPUTS:
    |    src     = PTX text (concatenated if multi-module)
    |    v361    = total PTX size
    |    v362    = per-module size array (if split)
    |    v351    = number of output modules
    |    v363    = error message (if any)
    |    v341    = return code (0=ok, 1=error, 8=log-only, 10=missing API)

 ============================================================================
 PHASE 8e: POST-COMPILE DECISION  (main, lines 1024-1075)
 ============================================================================

  If v351 == 1 (single module output):
    dword_2A5B514 = 1  (force single-module path)
    v351 = 0
    If !byte_2A5F285: goto whole/partial dispatch

  Else if dword_2A5B514 == 1:
    If !byte_2A5F285: goto whole/partial dispatch

  Else (multi-module, split-compile):
    v119 = allocate 8 * v351 bytes (per-module PTX pointer array)
    Split concatenated PTX text into v351 individual buffers
    using per-module sizes from v362

  Force-whole override:
    If !byte_2A5F285 && dword_2A5B514 == 1:
      If byte_2A5F284: byte_2A5F286 = 0  (force whole)

  Error handling:
    If v363 (warning): emit via sub_467460(&unk_2A5B560)
    If v341 (error):   emit nvvm error string, fatal

 ============================================================================
 PHASE 8f: TIMING + DEBUG TRACE  (main, lines 1088-1101)
 ============================================================================

  If qword_2A5F290 (timing enabled):
    sub_45CCE0(ptr)        -- stop "cicc-lto" timer
    sub_432340(...)        -- record elapsed time

  If dword_2A5F308 & 0x20 (extended debug):
    sub_4279C0("cicc-lto") -- print timing info

  If byte_2A5F29B (--verbose-keep):
    printf("nvlink -lto-nvvm-compile -m%d", dword_2A5F30C)
    For each option: printf(" %s", option[i])
    printf(" -o %s\n", filename)
    sub_4264E0(filename, ptx_text, ptx_size)  -- write PTX to file

  If byte_2A5F29A (--emit-ptx):
    sub_4264E0(filename, ptx_text, ptx_size)  -- write PTX, stop

 ============================================================================
 PHASE 8g: COMPILATION MODE RESET  (main, line 1154)
 ============================================================================

  dword_2A5B528 = byte_2A5F225 ? 6 : 0
    (Reset compilation mode: 6=SASS if SM>89, else 0=normal)

 ============================================================================
 PHASE 8h: COMPILATION DISPATCH  (main, lines 1155-1288)
 ============================================================================

  BRANCH 1: WHOLE-PROGRAM  (byte_2A5F286 == 0)
  ───────────────────────────────────────────────
    fwrite("whole program compile\n", stderr)  [if verbose]
    v242 = sub_429BA0(...)         -- build ptxas option string
    v341 = sub_4BD4E0(             -- ptxas_compile_whole @ 0x4BD4E0
              &v359, src,            (PTX text)
              dword_2A5F314,         (SM version)
              byte_2A5F2C0,          (optimization level)
              dword_2A5F30C == 64,   (64-bit mode)
              byte_2A5F310,          (debug flag)
              v242,                  (ptxas options)
              dword_2A5B528)         (compilation mode)

    sub_4BD4E0 internally:
      sub_4CDD60(&ctx)            -- create ptxas compilation context
      sub_4CE3B0(ctx, mode)       -- set compilation mode
      sub_4CE2F0(ctx, sm_ver)     -- set target architecture
      sub_4CE380(ctx)             -- set optimization (if a4)
      sub_4CE640(ctx, 1)          -- set 64-bit mode (if a5)
      sub_4CE3E0(ctx, opts)       -- pass option string
      sub_4CE070(ctx, ptx_text)   -- set input PTX
      sub_4CE8C0(ctx)             -- COMPILE (returns 0=ok, 3=warning)
      sub_4CE670(ctx, &buf, &count, &size)  -- get output
      If count != 1: error
      sub_4BE350(ctx, &data, &size)  -- extract binary
      memcpy output buffer
      sub_4BE400(ctx)             -- destroy context

    If byte_2A5F29B: sub_42A190(v359)  -- dump cubin

  BRANCH 2: RELOCATABLE SINGLE  (byte_2A5F286 == 1, dword_2A5B514 == 1)
  ──────────────────────────────────────────────────────────────────────
    fwrite("relocatable compile\n", stderr)  [if verbose]
    v305 = sub_429BA0(...)         -- build ptxas option string
    v341 = sub_4BD760(             -- ptxas_compile @ 0x4BD760
              &v359, src,
              dword_2A5F314, byte_2A5F2C0,
              dword_2A5F30C == 64, byte_2A5F310,
              v305, dword_2A5B528)

    sub_4BD760 internally:
      Same ptxas context setup as sub_4BD4E0
      sub_4CE3E0(ctx, "-rdc")     -- relocatable device code flag
      sub_4CE3E0(ctx, "-m64"/"-m32")
      If debug: sub_4CE3E0(ctx, 30616008)  -- debug metadata magic
      sub_4BE350 or setjmp-based error recovery
      Returns 0 on success, 7 on compile warning, 5/8 on error

  BRANCH 3: SPLIT-COMPILE  (byte_2A5F286 == 1, dword_2A5B514 > 1)
  ────────────────────────────────────────────────────────────────
    v256 = allocate 40 * v351 bytes  (work item array)
    v257 = sub_429BA0(...)           -- ptxas option string

    Thread pool creation:
      If dword_2A5B514 == 0:
        dword_2A5B514 = sub_43FD90() -- auto-detect thread count
      filenamea = sub_43FDB0(dword_2A5B514)  @ 0x43FDB0
        |
        | sub_43FDB0 internals:
        |   calloc(1, 0xB8)           -- 184-byte pool struct
        |   calloc(nmemb, 0x10)       -- per-thread slot array
        |   pool[21] = nmemb          -- thread count
        |   pool[4] = 0               -- pending count
        |   pthread_mutex_init(pool+24)
        |   pthread_cond_init(pool+64)   -- work available signal
        |   pthread_cond_init(pool+112)  -- work done signal
        |   pool[1] = signal_queue       -- via sub_44DC60
        |   For each thread:
        |     pthread_create(start_routine, pool)
        |     pthread_detach(thread)
        |
        If NULL: fatal "Unable to create thread pool"

    Work dispatch loop (v351 iterations):
      For each module i:
        work_item[0]  = &result_array[i]   (output pointer slot)
        work_item[8]  = ptx_pointer[i]     (per-module PTX text)
        work_item[16] = dword_2A5F314      (SM version)
        work_item[20] = byte_2A5F2C0 != 0  (optimization)
        work_item[21] = dword_2A5F30C == 64 (64-bit)
        work_item[22] = byte_2A5F310 != 0  (debug)
        work_item[24] = v257               (ptxas options)
        work_item[32] = dword_2A5B528      (compilation mode)

        sub_43FF50(pool, sub_4264B0, work_item)  @ 0x43FF50
          |
          | Enqueue work: malloc(0x18), set fn+arg+next
          | pthread_mutex_lock(pool+24)
          | Append to work queue
          | ++pool[4]  (pending count)
          | pthread_cond_broadcast(pool+64)  -- wake workers
          | pthread_mutex_unlock(pool+24)

        sub_4264B0 (worker function) @ 0x4264B0:
          Unpacks 40-byte work item struct
          Calls sub_4BD760 with unpacked fields
          Stores return code at work_item[36]

        If enqueue fails: fatal "Call to ptxjit failed in
                                  extended split compile mode"

    Wait for completion:
      sub_43FFE0(pool)  @ 0x43FFE0  -- wait_for_all
        |
        | pthread_mutex_lock(pool+24)
        | while (pending > 0 || queue non-empty):
        |   pthread_cond_wait(pool+112, pool+24)
        | pthread_mutex_unlock(pool+24)

    Teardown:
      sub_43FE70(pool)  @ 0x43FE70  -- destroy_pool
        |
        | pthread_mutex_lock(pool+24)
        | Clear work queue via sub_44DC40
        | Set pool[176] = 1  (shutdown flag)
        | pthread_cond_broadcast(pool+64)  -- wake all workers
        | pthread_mutex_unlock(pool+24)
        | Wait for all threads to exit
        | Destroy mutex, conds, free memory

    Result collection (v351 iterations):
      For each module i:
        sub_4297B0(work_item[i].retcode, "<lto ptx>")  -- check errors
        s1 = result_array[i]  (compiled cubin)
        sub_426570(ctx, s1, "lto.cubin", &v350)  -- validate cubin
          If fails: fatal "Ptxjit compilation failed in
                           extended split compile mode"

        FNLZR post-link (if SM > 89):
          If dword_2A5F314 > 0x59
             && (!byte_2A5F225 || sub_43DA40(s1))
             && !v350:
            sub_4275C0(&s1, "lto.cubin", dword_2A5F314, &v367, 0)
            result_array[i] = s1  (replace with finalized cubin)

        sub_45E7D0(v357[0])  -- merge_elf: merge cubin into output
        Free per-module PTX buffer

 ============================================================================
 PHASE 8i: TIMING "ptxas-lto"  (main, lines 1279-1286)
 ============================================================================

  If qword_2A5F290:
    sub_45CCE0(ptr)           -- stop "ptxas-lto" timer
    sub_432340(...)           -- record elapsed

  sub_4297B0(v341, "<lto ptx>")  -- check compile error

  If dword_2A5F308 & 0x20:
    sub_4279C0("ptxas-lto")   -- print timing

 ============================================================================
 PHASE 8j: RESULT INTEGRATION  (main, lines 1302-1367)
 ============================================================================

  RELOCATABLE PATH (byte_2A5F286 == 1, dword_2A5B514 == 1):
    sub_426570(ctx, cubin, "lto.cubin", &s1)  -- validate
    If SM > 89 && FNLZR conditions:
      sub_4275C0(&v367, "lto,cubin", dword_2A5F314, ptr, 0)
    Walk module list v353 to find last node
    Attach cubin to last node's slot [2]
    Set filename to "lto.cubin"
    v342 = 1  (mark as processed)

  WHOLE-PROGRAM PATH (byte_2A5F286 == 0):
    fopen(filename, "wb")       -- write cubin to output file
    v291 = sub_43DA80(v367)     -- get cubin size
    fwrite(v367, 1, v291, file)
    fclose(file)
    sub_43D990(v367)            -- free cubin

    LIBCUDADEVRT REMOVAL:
      If !byte_2A5F2C2 (not relocatable link) && v353 (module list):
        fwrite("LTO on everything so remove libcudadevrt from list\n")
        Verify: strstr(module_name, "cudadevrt")
          If not: fatal "expected libcudadevrt object"
        Free cudadevrt's cubin data, name, node
        Remove from module list

    If byte_2A5F288 && !byte_2A5F286:
      goto LABEL_252  (skip to final link, no merge needed)

 ============================================================================
 PHASE 9: MERGE  (continues at LABEL_311 / LABEL_191)
 ============================================================================

  For relocatable LTO:
    Compiled cubins now in the module list alongside pre-compiled cubins
    Normal merge_elf pipeline at sub_45E7D0 handles merging all cubins
    into the output ELF

  For whole-program LTO:
    Single cubin already written to output file
    Skips merge phase entirely
```

## The 5-State Option Consensus Machine

When multiple NVVM IR modules are linked, each module carries its own embedded compilation options (extracted from the `-inline-info`, `-ftz=`, `-prec_div=`, etc. strings baked into the IR by cicc at compile time). nvlink must reconcile these per-module options into a single consistent set before passing them to libnvvm. This reconciliation uses a 5-state finite automaton, applied independently to each of 8 tracked options.

The state machine is implemented in `sub_42AF40` at `0x42AF40` and runs once per IR module during the input loop (Phase 7). Each option has a pair of globals: a **state** variable (type `int`, one of states 0--4) and a **value** variable (the actual option value, either `int` or `int` count).

### State Definitions

| State | Name | Meaning |
|---|---|---|
| 0 | `UNSEEN` | No module has been processed yet. Initial state for all options |
| 1 | `ABSENT` | First module processed did NOT contain this option |
| 2 | `PRESENT` | First module processed DID contain this option, value recorded |
| 3 | `MIXED` | Some modules have the option and some do not (presence mismatch) |
| 4 | `CONFLICT` | Multiple modules provide the option but with different values |

### Transition Table

For each module processed, the state machine receives one of two inputs: **HAS(value)** (the module's IR contains the option with a specific value) or **ABSENT** (the option is not present in this module's IR).

```
Current State     Input HAS(v)              Input ABSENT
─────────────     ────────────              ────────────
0 (UNSEEN)        -> 2 (PRESENT), save v    -> 1 (ABSENT)
1 (ABSENT)        -> 3 (MIXED), save v      -> 1 (no change)
2 (PRESENT)       if v == saved: 2          -> 3 (MIXED)
                  if v != saved: 4 (CONFLICT)
3 (MIXED)         if v == saved: 3          -> 3 (no change)
                  if v != saved: 4 (CONFLICT)
4 (CONFLICT)      -> 4 (terminal)           -> 4 (terminal)
```

### Terminal States and Diagnostic Action

After all modules have been processed (post-input-loop, `main` lines 945--982), nvlink checks each option's final state:

| Final State | Action |
|---|---|
| 0 (UNSEEN) | No modules processed -- LTO not active |
| 1 (ABSENT) | Option not present in any module -- use default |
| 2 (PRESENT) | All modules agree -- use the common value |
| 3 (MIXED) | **Warning**: `sub_467460(&unk_2A5B5F0, "-<name>")` -- option present in some modules but not all. The discovered value is used for modules that had it; default for those that did not |
| 4 (CONFLICT) | **Error**: `sub_467460(&unk_2A5B600, "-<name>")` -- conflicting values across modules. This is a fatal diagnostic for `-ftz`, `-prec-div`, `-prec-sqrt`, `-fmad`, `-split-compile` |

### Tracked Options

| Option String (in IR) | State Global | Value Global | Description |
|---|---|---|---|
| `-ftz=N` | `dword_2A5F270` | `dword_2A5F274` | Flush-to-zero mode |
| `-prec_div=N` | `dword_2A5F26C` | `dword_2A5B524` | Precise division |
| `-prec_sqrt=N` | `dword_2A5F268` | `dword_2A5B520` | Precise square root |
| `-fmad=N` | `dword_2A5F264` | `dword_2A5B51C` | Fused multiply-add |
| `-maxreg N` | `dword_2A5F250` | `dword_2A5F254` | Maximum register count |
| `-split-compile N` | `dword_2A5F260` | `dword_2A5B518` | Split-compile thread count |
| `-generate-line-info` | `dword_2A5F248` | `byte_2A5F24C` | Line info generation (presence-only) |
| `-inline-info` | `dword_2A5F240` | `byte_2A5F244` | Inline info generation (presence-only) |

The `-maxrregcount` option has special handling: if the CLI provides `--maxrregcount` (setting `dword_2A5F22C > 0`), the per-module consensus value is ignored entirely. Only when the CLI does not provide a value does the consensus result matter, and in that case the state-3 (MIXED) and state-4 (CONFLICT) diagnostics fire.

## libcudadevrt Handling

`libcudadevrt` is the CUDA device runtime library. Its handling during LTO is unusual because it contains both pre-compiled SASS cubins and NVVM IR, and nvlink must decide whether to include or strip it based on the LTO mode.

### Collection Phase

During the input loop, when an archive member matches `"cudadevrt"` (via `strstr`), `sub_42A680` at `0x42A680` sets `byte_2A5F286 = 1` (partial mode) but does NOT set `byte_2A5F285 = 1` and does NOT emit the partial-LTO warning. This is the cudadevrt exception: it triggers partial mode silently, which can be overridden by `--force-whole-lto`.

When the input loop finds IR for libcudadevrt specifically (`v365` in main, line 922), it calls `sub_427A10` to register the IR and creates an 80-byte module node named `"libcudadevrt"`, prepending it to the module list `v353`.

### Stripping Phase

After whole-program LTO compilation succeeds (main lines 1346--1366), if the LTO compiled everything (`byte_2A5F288 && !byte_2A5F286`), nvlink strips libcudadevrt from the module list entirely:

```
fwrite("LTO on everything so remove libcudadevrt from list\n")
verify: strstr(module_name, "cudadevrt")  // else fatal
free cubin data, module name, module node
remove node from linked list
```

This is safe because whole-program LTO has already incorporated all device runtime functions from libcudadevrt's IR into the monolithic compiled output. Keeping the pre-compiled cubin would cause duplicate symbol errors during the merge phase.

## Compilation Dispatch Decision Tree

The choice between whole-program, relocatable, and split-compile paths is determined by a cascade of flag checks after `sub_4BC6F0` returns.

```
sub_4BC6F0 returns:
  v351 = number of output modules from nvvm
  byte_2A5F286 = 0 (whole) or 1 (partial)
  v341 = error code

  v351 == 1?
    |
    YES -> dword_2A5B514 = 1; v351 = 0
    |      (force single-module, disable split)
    |
    |      byte_2A5F285 (force-partial)?
    |        YES -> goto relocatable dispatch
    |        NO  -> goto force-whole check
    |
    NO --> dword_2A5B514 == 1?
             |
             YES -> byte_2A5F285? -> relocatable dispatch
             |      NO  -> goto force-whole check
             |
             NO --> SPLIT PATH (multi-module)
                    Split PTX into v351 separate buffers
                    Proceed to split-compile branch

  Force-whole check:
    !byte_2A5F285 && dword_2A5B514 == 1?
      YES -> if byte_2A5F284: byte_2A5F286 = 0
      (This is where --force-whole-lto takes effect,
       but ONLY if register_module hasn't set byte_2A5F285)

  Final dispatch:
    byte_2A5F286 == 0?
      -> WHOLE: sub_4BD4E0 (monolithic ptxas)
      -> dword_2A5B514 == 1?
           YES -> RELOCATABLE: sub_4BD760 (single relocatable ptxas)
           NO  -> SPLIT: thread pool + per-module sub_4BD760
```

### CLI Flags That Trigger Each Path

| Path | Required Flags | Forbidden Flags | Auto-Trigger |
|---|---|---|---|
| Whole-program | `-lto` | `--force-partial-lto`, `-r` | All inputs are LTO IR (including cudadevrt) |
| Relocatable single | `-lto` | `--force-whole-lto` | Any non-cudadevrt SASS cubin in inputs |
| Split-compile | `-lto`, `--split-compile-extended=N` (N > 1) | -- | nvvmCompileProgram returns multiple output modules |
| No LTO | (no `-lto`) | -- | Default when no IR inputs present |

## libnvvm API Call Sequence

The complete sequence of libnvvm API calls made by nvlink during a successful LTO compilation. Each call is annotated with the function that makes it and the error handling path.

```
=== Phase 1: Library Loading (sub_4BC4A0 @ 0x4BC4A0) ===

  dlopen(nvvmpath, RTLD_LAZY)             -- load libnvvm.so
  dlsym(handle, "__nvvmHandle")           -- get meta-API entry point
  __nvvmHandle(0x2080)                    -- magic 0x2080: get creation fn
  creation_fn(program_handle, ir_data, ir_size, filename)
                                          -- creates program + adds first module
  If more IR modules:
    nvvmAddModuleToProgram(handle, data, size, name)  [per additional module]

=== Phase 2: Callback Registration (main, lines 985-1008) ===
  (Only if --verbose-keep)

  dlsym(handle, "__nvvmHandle")
  __nvvmHandle(0xBEEF)                   -- get callback handle
  callback_handle(nvvm_state, sub_4299E0, 0, 0xF00D)
                                          -- register post-link file writer

=== Phase 3: Compilation (sub_4BC6F0 @ 0x4BC6F0) ===

  dlsym(handle, "nvvmCompileProgram")
  dlsym(handle, "nvvmGetCompiledResultSize")
  dlsym(handle, "__nvvmHandle")
  __nvvmHandle(0xB0BA)                   -- multi-module result accessor
  __nvvmHandle(0xF00D)                   -- multi-module size accessor
  dlsym(handle, "nvvmGetCompiledResult")
  dlsym(handle, "nvvmGetErrorString")
  dlsym(handle, "nvvmGetProgramLogSize")
  dlsym(handle, "nvvmGetProgramLog")
  dlsym(handle, "nvvmDestroyProgram")

  nvvmCompileProgram(program, option_count, options)
    -> returns 0 (partial), 100 (whole), or error code

  nvvmGetProgramLogSize(program, &log_size)
    -> if log_size > 1: allocate + nvvmGetProgramLog(program, buffer)

  nvvmGetCompiledResultSize(program, &result_size)
  If multi-module (via 0xF00D):
    multi_sizer(program, &module_count)
    -> module_count > 1: allocate per-module size array
    multi_result(program, &module_count, size_array)
  nvvmGetCompiledResult(program, buffer)

  nvvmDestroyProgram(&program)

=== Phase 4: Cleanup ===

  No dlclose() -- libnvvm.so stays loaded for the process lifetime
```

### Magic Cookie Reference

| Value | Hex | Name | Purpose |
|---|---|---|---|
| 8320 | `0x2080` | Program creator | Used in `sub_4BC4A0`: `__nvvmHandle(0x2080)` returns the function that creates a program and adds the first IR module in one call |
| 45242 | `0xB0BA` | Multi-result accessor | Used in `sub_4BC6F0`: `__nvvmHandle(0xB0BA)` returns a function to retrieve per-module compiled results when nvvm produces multiple output modules |
| 48879 | `0xBEEF` | Callback handle | Used in main: `__nvvmHandle(0xBEEF)` returns a handle for registering output callbacks |
| 61453 | `0xF00D` | Callback/sizer registration | Used in main: passed as 4th arg to the callback handle to register the file-writer callback. Also used in `sub_4BC6F0` for the multi-module size query function |

## Embedded ptxas Compilation API

Both `sub_4BD4E0` (whole-program) and `sub_4BD760` (relocatable) use the same embedded ptxas API, which mirrors the standalone ptxas tool's internal interface:

| Function | Address | Purpose |
|---|---|---|
| `sub_4CDD60` | `0x4CDD60` | Create compilation context (allocates state) |
| `sub_4CE3B0` | `0x4CE3B0` | Set compilation mode (0/2/4/6) |
| `sub_4CE2F0` | `0x4CE2F0` | Set target SM version |
| `sub_4CE380` | `0x4CE380` | Enable optimizations |
| `sub_4CE640` | `0x4CE640` | Set 64-bit mode |
| `sub_4CE3E0` | `0x4CE3E0` | Pass additional option string |
| `sub_4CE070` | `0x4CE070` | Set input PTX text |
| `sub_4CE8C0` | `0x4CE8C0` | Execute compilation (returns 0/3/error) |
| `sub_4CE670` | `0x4CE670` | Get output metadata (buffer, count, size) |
| `sub_4BE350` | `0x4BE350` | Extract compiled binary |
| `sub_4BE3D0` | `0x4BE3D0` | Get error log |
| `sub_4BE400` | `0x4BE400` | Destroy compilation context |

The difference between the two paths:

- **`sub_4BD4E0` (whole)**: Expects `count == 1` from `sub_4CE670`. If count != 1, returns error code 1. Does not set `-rdc` flag. Sets `-m32` or `-m64` based on word size.
- **`sub_4BD760` (relocatable)**: Passes additional flags (`-rdc` equivalent via magic constant `30614221`, debug via `30616008`). Uses `setjmp`/`longjmp` for error recovery when the ptxas backend signals a non-fatal issue. Can handle `count != 1` by falling through to the copy path.

## Split Compilation Thread Pool

The thread pool used for split compilation follows a classic producer-consumer pattern with POSIX threads.

### Pool Structure (184 bytes, allocated by `sub_43FDB0`)

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 8 | `thread_slots` | Pointer to `nmemb * 16` byte array (pthread_t + padding per thread) |
| 8 | 8 | `signal_queue` | Work queue signal (via `sub_44DC60`) |
| 16 | 4 | `pending_count` | Number of outstanding work items |
| 24 | 40 | `mutex` | `pthread_mutex_t` for all pool operations |
| 64 | 48 | `work_available` | `pthread_cond_t` broadcast when new work arrives |
| 112 | 48 | `work_done` | `pthread_cond_t` broadcast when a worker completes |
| 160 | 8 | `active_threads` | Count of running threads (non-shutdown mode) |
| 168 | 8 | `active_threads_alt` | Count of running threads (shutdown mode) |
| 176 | 1 | `shutdown_flag` | Set to 1 during `sub_43FE70` to signal threads to exit |

### Work Item Structure (40 bytes)

| Offset | Size | Field | Source |
|---|---|---|---|
| 0 | 8 | `result_ptr` | Pointer to slot in result array |
| 8 | 8 | `ptx_text` | Per-module PTX string |
| 16 | 4 | `sm_version` | `dword_2A5F314` |
| 20 | 1 | `optimize` | `byte_2A5F2C0 != 0` |
| 21 | 1 | `is_64bit` | `dword_2A5F30C == 64` |
| 22 | 1 | `debug` | `byte_2A5F310 != 0` |
| 24 | 8 | `ptxas_opts` | Output of `sub_429BA0` |
| 32 | 4 | `comp_mode` | `dword_2A5B528` |
| 36 | 4 | `return_code` | Filled by worker (`sub_4264B0`) |

### Lifecycle

1. **Create**: `sub_43FDB0(N)` -- allocates pool, creates N detached worker threads. Each thread runs `start_routine` which blocks on `work_available` condition.

2. **Submit**: `sub_43FF50(pool, fn, arg)` -- allocates 24-byte queue node, enqueues work, increments pending count, broadcasts `work_available`.

3. **Worker**: Wakes on broadcast, dequeues work item, calls `fn(arg)` (which is `sub_4264B0`), which calls `sub_4BD760` with the unpacked work item fields, stores return code at offset 36.

4. **Wait**: `sub_43FFE0(pool)` -- caller blocks on `work_done` condition until pending count reaches 0 and work queue is empty.

5. **Destroy**: `sub_43FE70(pool)` -- sets shutdown flag, broadcasts `work_available` to wake all workers, waits for all threads to exit, destroys synchronization primitives, frees memory.

## FNLZR Post-Link Transform

For Mercury targets (SM >= 100), each compiled cubin passes through the FNLZR (Finalizer) post-link transform at `sub_4275C0` (`0x4275C0`). This step runs after ptxas compilation but before merge, under the following conditions:

```c
if (dword_2A5F314 > 0x59        // SM > 89  (sm_90+)
    && (!byte_2A5F225            // NOT in SASS-only mode
        || sub_43DA40(cubin))    //   OR cubin has Mercury markers
    && !v350)                    // No legacy ELF class detected
{
    sub_4275C0(&cubin, "lto.cubin", dword_2A5F314, &state, 0);
}
```

In the split-compile path, this runs per-module after each worker's result is collected. In the relocatable single-module path, it runs once after validation. The FNLZR transform performs Mercury-specific binary modifications documented in [Mercury Finalizer](../mercury/fnlzr.md).

## When LTO Activates

LTO activation depends on both explicit flags and implicit architecture thresholds:

| Condition | Effect |
|---|---|
| `--lto` / `-lto` passed | Sets `byte_2A5F288`. Enables IR input acceptance and LTO compilation pipeline |
| `--dlto` passed | Sets `byte_2A5F287`. Distributed LTO mode (IR modules compiled on remote workers). Also sets `byte_2A5F288` |
| SM > 89 | Sets `byte_2A5F225` (SASS mode). Compilation mode (`dword_2A5B528`) becomes 6. Targets from sm_90 onward require SASS output, which means the embedded compiler backend always runs |
| SM > 99 | Sets `byte_2A5F222` (Mercury mode). Adds FNLZR post-link step to the pipeline |
| No IR inputs present | LTO pipeline skipped even if `-lto` is set. The flag only enables IR acceptance |

The compilation mode global `dword_2A5B528` encodes the active mode:

| Value | Mode | Description |
|---|---|---|
| 0 | Normal | Standard linking, no embedded compilation |
| 2 | Passthrough | Archive pass-through mode |
| 4 | LTO | Link-time optimization via libnvvm + embedded ptxas |
| 6 | SASS | Direct SASS output (SM > 89). Implies embedded ptxas is active |

For architectures SM 90 and above (Hopper, Blackwell, and beyond), the SASS output mode is mandatory. This means the embedded compiler backend is always involved for these targets, regardless of whether `-lto` is explicitly passed. The `-lto` flag controls whether IR-level whole-program optimization through libnvvm occurs.

## LTO-Specific CLI Options

| Option | Short | Type | Global | Description |
|---|---|---|---|---|
| `--link-time-opt` | `-lto` | bool | `byte_2A5F288` | Enable LTO. Required for IR inputs |
| `--dlto` | -- | bool | `byte_2A5F287` | Distributed LTO mode |
| `--force-partial-lto` | -- | bool | `byte_2A5F285` | Force partial LTO even when whole-program is possible |
| `--force-whole-lto` | -- | bool | `byte_2A5F284` | Force whole-program LTO. Only effective when `byte_2A5F285` is not set by `register_module` |
| `--nvvmpath` | -- | string | `qword_2A5F278` | Path to `libnvvm.so`. Required with `-lto` |
| `--emit-ptx` | -- | bool | `byte_2A5F29A` | Emit intermediate PTX instead of SASS |
| `--split-compile` | -- | int | `dword_2A5F260` | Split compilation mode |
| `--split-compile-extended` | -- | int | `dword_2A5B514` | Extended split-compile thread count |
| `--Xnvvm` | -- | string (multi) | `qword_2A5F230` | Pass-through options to libnvvm/cicc |
| `--Xptxas` | -- | string (multi) | `qword_2A5F238` | Pass-through options to embedded ptxas |
| `--maxrregcount` | -- | int | `dword_2A5F22C` | Maximum register count per thread |
| `--Ofast-compile` | `-Ofc` | string | `qword_2A5F258` | Compilation speed vs quality tradeoff. Values: `"0"`, `"min"`, `"mid"`, `"max"` |
| `--verbose-keep` | `-vkeep` | bool | `byte_2A5F29B` | Dump intermediate files (PTX, cubin) and print command-line reconstructions |
| `-g` / `--debug` | -- | bool | `byte_2A5F310` | Enable debug info generation in compiled output |
| `--use-host-info` | -- | bool | `byte_2A5F214` | Enable host-side symbol usage information for cross-module DCE |

## Key Functions

| Address | Size | Name | Role |
|---|---|---|---|
| `0x409800` | 57,970 B | `main` | Top-level orchestrator. LTO pipeline occupies lines 920--1370 |
| `0x42AF40` | ~4,500 B | `process_input_object` | Processes each input: calls nvvm_api_init, adds IR module, runs option consensus state machine |
| `0x427A10` | ~200 B | `lto_add_module` | Wrapper: validates `-lto` flag, calls `nvvm_api_wrapper_init` + `nvvmAddModule`, counts modules |
| `0x42A680` | ~2,000 B | `register_module` | Creates 80-byte module node, sets partial-LTO flag if non-IR cubin found (with cudadevrt exception) |
| `0x426AE0` | 2,178 B | `lto_mark_used_symbols` | Marks reachable symbols for cross-module DCE (called from `sub_426CD0` when host-info active) |
| `0x426CD0` | 7,040 B | `lto_collect_ir_modules` | Builds cicc/NVVM option list: architecture, split-compile, Ofast, maxreg, debug, --Xnvvm passthrough, math-mode consensus values |
| `0x4BC4A0` | 2,548 B | `nvvm_api_wrapper_init` | Loads `libnvvm.so` via `dlopen`, resolves `__nvvmHandle(0x2080)`, creates program, adds first IR module |
| `0x4BC6F0` | 13,602 B | `nvvm_compile_and_extract` | Resolves all 8 libnvvm API functions via `dlsym`, builds option array with host-refs, calls `nvvmCompileProgram`, extracts PTX and per-module sizes, retrieves compilation log |
| `0x4BD1F0` | ~100 B | `nvvm_add_module` | Thin wrapper: `nvvmAddModuleToProgram` + name extraction. Called from `sub_42AF40` |
| `0x429BA0` | 6,699 B | `ptxas_option_builder` | Builds the space-separated ptxas option string from `--Xptxas` and internal flags |
| `0x4BD4E0` | ~600 B | `ptxas_compile_whole` | Whole-program PTX-to-SASS compilation. Expects single output. |
| `0x4BD760` | ~800 B | `ptxas_compile_relocatable` | Relocatable PTX-to-SASS compilation. Handles `-rdc`, `setjmp`-based error recovery |
| `0x4264B0` | ~50 B | `split_compile_worker` | Thread pool worker: unpacks 40-byte work item, calls `ptxas_compile_relocatable` |
| `0x4299E0` | ~150 B | `lto_post_link_callback` | Callback registered via `0xBEEF`/`0xF00D`: writes intermediate files during `--verbose-keep` |
| `0x43FDB0` | ~200 B | `thread_pool_create` | Creates pthread-based thread pool: allocates 184-byte struct, spawns N detached workers |
| `0x43FF50` | ~100 B | `thread_pool_submit` | Enqueues work item: malloc(24), append to queue, broadcast `work_available` |
| `0x43FFE0` | ~100 B | `thread_pool_wait` | Blocks until all pending work completes (monitors `pending_count` and queue) |
| `0x43FE70` | ~200 B | `thread_pool_destroy` | Sets shutdown flag, wakes all workers, waits for exit, destroys mutex/conds, frees memory |
| `0x43FD90` | varies | `auto_detect_threads` | Returns system thread count for split-compile when user doesn't specify |
| `0x4275C0` | varies | `fnlzr_transform` | FNLZR post-link transform for Mercury targets (SM >= 100) |
| `0x426570` | ~1,200 B | `validate_cubin` | Validates compiled cubin: checks ELF format, architecture match, CUDA API version, word size |
| `0x45E7D0` | varies | `merge_elf` | Merges compiled cubin into output ELF (normal merge pipeline) |
| `0x1406B40` | 6,725 B | `lto_create_compilation_context` | Allocates 272-byte context: SM version, debug flags, optimization level |
| `0x1407FC0` | 26,791 B | `lto_compile_function` | Per-function compilation driver (ISel, regalloc, emission) |
| `0x14091C0` | 23,593 B | `lto_link_and_emit` | Links compiled functions, emits final ELF sections |
| `0x140A1C0` | 5,270 B | `lto_finalize_output` | Finalizes LTO compilation output |
| `0x140A6B0` | 5,462 B | `lto_report_resource_usage` | Prints register/memory/barrier statistics per kernel |

## Key Globals

| Address | Size | Name | Role |
|---|---|---|---|
| `byte_2A5F288` | 1 | `lto_enabled` | Master LTO enable flag |
| `byte_2A5F287` | 1 | `dlto_enabled` | Distributed LTO flag |
| `byte_2A5F286` | 1 | `relocatable_compile` | 0 = whole-program, 1 = partial/relocatable LTO output |
| `byte_2A5F285` | 1 | `force_partial_lto` | Force partial LTO. Also auto-set by `register_module` on non-cudadevrt SASS input |
| `byte_2A5F284` | 1 | `force_whole_lto` | Force whole-program LTO (only effective when `byte_2A5F285` not set) |
| `byte_2A5F225` | 1 | `is_sass_mode` | SM > 89 flag. SASS output required |
| `byte_2A5F222` | 1 | `is_mercury_mode` | SM > 99 flag. Mercury post-link enabled |
| `dword_2A5B528` | 4 | `compilation_mode` | 0=normal, 2=passthru, 4=lto, 6=sass |
| `dword_2A5B514` | 4 | `split_compile_ext_threads` | Thread count for extended split compile |
| `dword_2A5B518` | 4 | `split_compile_nvvm_threads` | Thread count for nvvm split compile |
| `dword_2A5F280` | 4 | `lto_module_count` | Count of registered LTO IR modules |
| `qword_2A5F278` | 8 | `nvvmpath` | Path to `libnvvm.so` |
| `qword_2A5F230` | 8 | `xnvvm_options` | Forwarded options for libnvvm |
| `qword_2A5F238` | 8 | `xptxas_options` | Forwarded options for embedded ptxas |
| `qword_2A5F258` | 8 | `ofast_compile_level` | Compilation speed tradeoff (`"0"`/`"min"`/`"mid"`/`"max"`) |
| `dword_2A5F270` | 4 | `ftz_consensus_state` | 5-state machine for `-ftz` option |
| `dword_2A5F274` | 4 | `ftz_value` | Discovered `-ftz` value |
| `dword_2A5F26C` | 4 | `prec_div_consensus_state` | 5-state machine for `-prec-div` |
| `dword_2A5B524` | 4 | `prec_div_value` | Discovered `-prec-div` value |
| `dword_2A5F268` | 4 | `prec_sqrt_consensus_state` | 5-state machine for `-prec-sqrt` |
| `dword_2A5B520` | 4 | `prec_sqrt_value` | Discovered `-prec-sqrt` value |
| `dword_2A5F264` | 4 | `fmad_consensus_state` | 5-state machine for `-fmad` |
| `dword_2A5B51C` | 4 | `fmad_value` | Discovered `-fmad` value |
| `dword_2A5F250` | 4 | `maxreg_consensus_state` | 5-state machine for `-maxreg` |
| `dword_2A5F254` | 4 | `maxreg_value` | Discovered `-maxreg` value |
| `dword_2A5F260` | 4 | `split_compile_consensus_state` | 5-state machine for `-split-compile` |

## Timing Trace Points

When timing is enabled (`qword_2A5F290` is non-NULL), the LTO phase records two timing points:

| Phase Name | Description |
|---|---|
| `"cicc-lto"` | Time spent in libnvvm IR compilation (Phase 8d) |
| `"ptxas-lto"` | Time spent in embedded ptxas assembly (Phase 8h) |

These appear in the debug trace alongside the standard phase names: `"init"`, `"read"`, `"merge"`, `"layout"`, `"relocate"`, `"finalize"`, `"write"`.

## Resource Usage Reporting

`lto_report_resource_usage` at `0x140A6B0` prints per-kernel statistics after LTO compilation:

```
Used %d registers, %lld bytes smem, %lld bytes lmem
%lld bytes gmem, %lld bytes cmem[0..17]
%d barriers, %d samplers, %d surfaces, %d textures
%d bytes cumulative stack size
Compile time = %.3f ms
```

Constant memory banks are enumerated from `0x70000004` through `0x70000016` (18 banks). This output appears when verbose mode is active and routes through the diagnostic subsystem at `dword_2A5DC90`.

## Embedded Compiler Backend Layout

The embedded ptxas backend within nvlink spans approximately `0x530000` to `0x1D32172` (~24.7 MB). The LTO-specific compilation engine occupies a 1.5 MB region at `0x12B0000`--`0x1430000`, organized as:

| Range | Size | Subsystem |
|---|---|---|
| `0x12B0000`--`0x12BA000` | 40 KB | PTX operand/type system, special registers, symbol table |
| `0x12BA000`--`0x12D0000` | 88 KB | ISel lowering passes (~200 functions) |
| `0x12D0000`--`0x12D5000` | 20 KB | DWARF debug line info generator |
| `0x12D5000`--`0x1400000` | 11 MB | ISel pattern matchers (parametric clones per SM variant) |
| `0x1400000`--`0x1430000` | 192 KB | Top-level LTO pipeline, ELF emission, MMA lowering |

ISel patterns are instantiated 4--5 times for different architecture targets:
- Base (sm_5x): `0x12BA000`--`0x12D0000`
- sm_8x clone: `0x13D6B10`--`0x13DED20`
- sm_9x clone: `0x13EC1E0`--`0x13FE860`
- sm_10x clone: `0x140AFE0`--`0x1418220`

Each clone set contains 50--60 functions implementing identical lowering logic specialized for the target's instruction set.

## Related Pages

- [libnvvm Integration](libnvvm-integration.md) -- API loading, callback mechanism, error handling, `__nvvmHandle` magic cookies
- [Whole vs Partial LTO](whole-vs-partial.md) -- Decision logic, flag interactions, 14-row mode decision matrix, partial LTO warnings
- [Split Compilation](split-compilation.md) -- Thread pool lifecycle, work item format, synchronization protocol
- [Option Forwarding to cicc](option-forwarding.md) -- How `sub_426CD0` and `sub_429BA0` assemble the option vectors for libnvvm and ptxas
- [LTO IR Format Versions](ir-format-versions.md) -- NVVM IR bitcode detection and version constraints
- [Pipeline Overview](../pipeline/overview.md) -- Full 14-phase pipeline context (LTO is Phase 8)
- [Entry Point & Main](../pipeline/entry.md) -- `main()` walkthrough with line numbers for every phase including LTO
- [Architecture Dispatch](../ptxas/arch-dispatch.md) -- SM-variant vtable selection for ISel clones
- [Merge Phase](../pipeline/merge.md) -- post-LTO merge that integrates compiled cubins into the output ELF
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- linker-level DCE suppressed during full LTO, active during partial LTO
- [Symbol Resolution](../linker/symbol-resolution.md) -- symbol handling for LTO-compiled modules merged into the output
- [Mercury Finalizer](../mercury/fnlzr.md) -- FNLZR post-link transform applied to LTO output for SM >= 100

### Sibling Wiki

- **cicc wiki**: [LTO & Module Optimization](../../cicc/lto/index.html) -- compiler-side LTO pipeline (five-pass IR optimization, inliner cost model, cross-module import). nvlink delegates IR compilation to cicc via libnvvm; this page documents what cicc does with the IR
