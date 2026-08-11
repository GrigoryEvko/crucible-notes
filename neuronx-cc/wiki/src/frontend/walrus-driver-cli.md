# walrus_driver Backend CLI & Pass Vocabulary

> *All symbols, addresses, and strings on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 — specifically the cp310 build of `neuronxcc/starfish/lib/libwalrus.so` (BuildID `92b4d331a42d7e80bb839e03218d2b9b0c23c346`, md5 `1d93972b…`, 64,973,024 B) and the Cython job wrappers `neuronxcc/driver/jobs/WalrusDriver.cpython-310-x86_64-linux-gnu.so` and `…/support/EmbeddedWalrusDriver.cpython-310-…so`. For libwalrus `.text`/`.rodata`, VA == file offset. Other wheels differ; treat every address as version-pinned.*

## Abstract

`walrus_driver` is the **LLVM-CommandLine front-end of the Walrus backend** — the stage that takes BIR (`bir.json`) and drives it through ~150 backend passes down to a packaged NEFF. It is the CLI gateway to everything in [Part 8](../walrus/): the pass roster, the allocators, the schedulers, the DMA legalizers, the BIR simulator. To a reader who already knows the `neuronx_cc` job pipeline (see [3.3](compilecommand-pipeline.md)), this is the one tool the `WalrusDriver` Job spawns, and this page is the catalog of its surface: the **~150 `cl::opt`/`cl::list` flags** it registers, the **150-name `--pass` / `--skip-pass` vocabulary** that names every backend pass, and the two ways the driver is entered — as a forked subprocess or as an in-process `dlopen`.

The architecture is deliberately thin-launcher-over-fat-library. The on-disk ELF `bin/walrus_driver` (708,864 B, stripped) contains almost no logic; **all** of the option registry, the pass registry, and the pipeline builders live in `libwalrus.so`. The driver's entire `cl::opt` surface is registered by a single global static-init constructor, `sub_7C2890` (`0x7c2890`), whose 2,354-line decompile registers every flag — 66 manual `setArgStr`+`addArgument` paths plus 95 templated `cl::opt`/`cl::list` constructor calls. The driver body is exported as `_Z18run_backend_driveriPPc` at `0x8113b0`; that symbol is the seam that makes the subprocess-vs-embedded split possible (the embedded wrapper `dlopen`s libwalrus and calls it directly).

The page is two catalogs and a dispatch model. First the **flag catalog**, grouped by subsystem and grounded in the `cl::desc` help strings interned in the `0x1dc1041..0x1dc40f8` rodata belt. Then the **`--pass` vocabulary** — the 150 `register_generator_<name>__` free functions that are the *only* legal pass names, resolved at run time through `GeneratorRegistration::hasGenerator`. Finally the **CLI dispatch**: how option registration feeds pass-list construction from `--pass`/`--skip-pass`/`--start-pass`, and how the Cython `WalrusDriver` Job chooses the subprocess (`runWalrusDriver`) versus the in-process (`runEmbeddedWalrusDriver`) entry.

For reimplementation, the contract is:

- **The option registry** — that one static constructor `sub_7C2890` builds the entire LLVM-CommandLine option set, and the grouping/types/defaults of the ~150 flags it registers.
- **The pass-name namespace** — that `--pass`/`--skip-pass`/`--start-pass`/`--print-after`/`--enable-checker-after` all draw from the *same* 150-name set, registered as `register_generator_<name>__` free functions and validated by `hasGenerator`, with an unknown name producing `Backend pass: <X> is not registered!`.
- **The two entry paths** — the forked `walrus_driver` ELF and the in-process `EmbeddedWalrusDriver` that re-enters `run_backend_driver` after `dlopen`, gated by the *driver-level* flag `--enable-internal-fork-walrus`.

| | |
|---|---|
| **Driver entry** | `run_backend_driver` (`_Z18run_backend_driveriPPc`) @ `0x8113b0` |
| **Option registry** | `sub_7C2890` @ `0x7c2890` — global static-init ctor; 66 manual + 95 ctor-call flags |
| **Driver flag belt** | rodata `0x1dc1041..0x1dc40f8` (`cl::desc` help strings) |
| **Pass-name validator** | `GeneratorRegistration::hasGenerator` (str `0x1dc5960`); error `0x1dc58c0`/`0x1dc58cf` |
| **Pass vocabulary** | **150** `register_generator_<name>__` free functions (§ The `--pass` Vocabulary) |
| **Subprocess ELF** | `bin/walrus_driver` (708,864 B, stripped thin launcher) |
| **In-process entry** | `EmbeddedWalrusDriver.so` → `dlopen(libwalrus.so)` → `run_backend_driver` |
| **Job argv builder** | `WalrusDriver.cpython-310.so` `runWalrusDriver` @ `0x8ddf0` (Cython) |
| **Source path** | `…/src-3.10.16/neuronxcc/walrus/driver/src/walrus_driver.cpp` |

---

## The Two Entry Paths

### Purpose

The Walrus backend has exactly one body of logic — `run_backend_driver` — but two ways to reach it. The split exists so the Python driver can either isolate the backend in its own address space (a forked `walrus_driver` process, the production default) or run it in-process to avoid fork/exec overhead and serialization. The choice is made on the Python side, not inside libwalrus; the native code is identical either way.

### Entry Point

```text
(a) SUBPROCESS path
    Python WalrusDriver Job.runWalrusDriver (0x8ddf0)
      └─ spawn ELF  bin/walrus_driver         (str "walrus_driver" @ WD 0xa7018)
           └─ main → run_backend_driver(argc, argv)   @ libwalrus 0x8113b0

(b) EMBEDDED path                              (gated by --enable-internal-fork-walrus)
    Python WalrusDriver Job.runEmbeddedWalrusDriver
      └─ EmbeddedWalrusDriver.so
           └─ dlopen("libwalrus.so")           (native_import: "libwalrus.so")
                └─ run_backend_driver(int, char**)   (native_import: _Z18run_backend_driveriPPc)
```

Both paths converge on the same exported symbol: `nm -D libwalrus.so` shows `0000000000811b0 T _Z18run_backend_driveriPPc`, and `EmbeddedWalrusDriver.cpython-310.so`'s interned native-import table contains both `libwalrus.so` and `_Z18run_backend_driveriPPc` (and the demangled `run_backend_driver`). The subprocess ELF is a stripped 708,864-B launcher that re-enters the same body via the dynamic symbol.

### Algorithm

```c
// Python-side dispatch — WalrusDriver.cpython-310.so (Cython), illustrative
function WalrusDriver_run(self, state):       // runOnState / runSingleInput
    argv = build_walrus_argv(self, state)     // runWalrusDriver argv builder @ 0x8ddf0
    if self.enable_internal_fork_walrus:       // driver-level flag, NOT a libwalrus cl::opt
        return runEmbeddedWalrusDriver(argv)   // dlopen libwalrus; call run_backend_driver in-proc
    else:
        return spawn("walrus_driver", argv)    // fork/exec the ELF launcher
    // Native side, both paths: run_backend_driver(argc, argv) @ 0x8113b0
```

> **NOTE — the fork toggle lives on the Job, not on `walrus_driver`.** `--enable-internal-fork-walrus` is one of the driver-level `--internal-*`/`--enable-internal-*` convenience flags the *Job* interprets; it is **not** in libwalrus's `cl::opt` set (`sub_7C2890`). Searching the libwalrus flag belt for it returns nothing. The native tool has no idea whether it was forked or `dlopen`ed.

### Considerations

The Cython `WalrusDriver` job exposes several run modes beyond the two entry paths: `runWalrusDriver`, `runEmbeddedWalrusDriver`, `runMT`, `runVNC`, `runSingleInput`, `runOnState`, `handle_cached_subgraph`, plus working-dir helpers `getLnkDir`/`getSgDir`/`getWorkingDir`. `runMT` builds subgraphs in parallel threads and links them (paired with the native `--enable-mt-backend`); `runVNC` is the virtual-neuron-core / LNC multi-graph link path (paired with `--link-subgraphs`/`--link-dir`). The file-I/O convention, read from the job's interned strings: input BIR is `bir.json` / `walrus_bir.out.json`; per-subgraph working dirs are `sgXX`; the `#!/bin/bash` replay wrapper `replay_walrus.sh` is emitted only when `--enable-internal-walrus-replay-script` is set, and reproduces the exact `walrus_driver` invocation. The replay script is the *only* shell wrapper in the path — `bin/walrus_driver` itself is a stripped ELF, not a shell script.

---

## CLI Dispatch — Option Registration to Pass List

### Purpose

`run_backend_driver` is an LLVM-CommandLine program. By the time `main` runs, every flag has already been registered by the global static-init constructor `sub_7C2890`; `run_backend_driver` parses `argv` against that registry, then chooses *what to run*. The interesting decision is how the resolved pass order is built: an explicit `--pass` list bypasses the optlevel pipeline builders entirely, while the optlevel/allocator flags select a builder that constructs the order for you.

### Entry Point

```text
static-init (before main)
  └─ sub_7C2890 (0x7c2890)        ── registers all ~150 cl::opt / cl::list
                                     (66 manual setArgStr+addArgument, 95 ctor calls)
run_backend_driver (0x8113b0)
  ├─ cl::ParseCommandLineOptions(argc, argv)
  ├─ if --list-passes:  dump getPassNames() and exit          (var listPasses)
  ├─ buildPassOrder():
  │    if --pass given:        order = resolve(--pass list)   ── explicit; skips builders
  │    else:                   order = optlevelPipeline(--optlevel, --allocator, --smt-allocation)
  │    order -= (--skip-pass)                                 ── set subtraction
  │    order  = drop_before(order, --start-pass)              ── skip-all-before
  ├─ if --dry-run:  print order and exit  (no execution)
  └─ for pass_name in order:                                  ── walrus_driver.cpp:2283/:2291
        gen = GeneratorRegistration::getGenerator(pass_name)  ── via hasGenerator (0x1dc5960)
        if !gen:  fatal "Backend pass: " + pass_name + " is not registered!"
        run(gen->create(passOptions))
```

### Algorithm

```c
// Name resolution for every pass-name-bearing flag.
// All of --pass / --skip-pass / --start-pass / --print-after /
// --print-before / --enable-checker-after / --memory-analysis-after draw
// from the SAME namespace: the 150 register_generator_<name>__ passes.
function resolvePassName(name):                  // walrus_driver.cpp:519, :932
    if !GeneratorRegistration::hasGenerator(name):   // str "hasGenerator" @ 0x1dc5960
        // strings @ 0x1dc58c0 + 0x1dc58cf
        fatal("Backend pass: " + name + " is not registered!")
    return GeneratorRegistration::getGenerator(name)

// Pipeline selection when --pass is NOT given. Builder VAs are decompiler labels.
function optlevelPipeline(optlevel, allocator, smt):
    if smt:                          return buildSmtPipeline()      // sub_80D9D0  (≈ opt7)
    switch (optlevel, allocator):                                   // builders:
        // sub_80A6E0 / sub_806F80 / sub_807EF0 / sub_809580
        case allocator == "coloring":           ...
        case allocator == "lsa":                ...
        case allocator == "coloring_with_loop": ...
    // --optlevel default = -1; "Supported value: 0 or 2"
```

> **QUIRK — `--pass` is the bypass, not the addition.** Supplying `--pass a,b,c` does not *append* to the optlevel pipeline; it *replaces* it. The optlevel/allocator/`--smt-allocation` machinery only runs when `--pass` is absent. `--skip-pass` and `--start-pass` are then applied as post-filters on whichever order was produced. This is why the production `WalrusDriver` Job emits an explicit `--pass` list — it wants deterministic control of the order, not an optlevel-derived one.

> **GOTCHA — the same string is a flag value *and* a checker target.** A name like `coloring_allocator_dram_shared` appears both as a `--pass`/`--start-pass` value and as a `--memory-analysis-after`/`--enable-checker-after`/`--print-after` argument. They all validate against the one 150-name set. A reimplementation that gives `--print-after` a separate (looser) namespace will silently accept names `--pass` rejects.

### Considerations

`--dry-run` ("List the passes that wil be run for a given set of inputs" — typo verbatim in the binary) prints the resolved order and exits *without running anything*; it is the way to see the effect of `--pass`/`--skip-pass`/`--start-pass`/optlevel interaction. `--list-passes` (variable `listPasses`, help "list all backend passes.") dumps the entire 150-name registry via `getPassNames()`. The `--bugpoint-throw-after <pass>` and `--run-debugger-after <pass>` flags also take pass names from this set and short-circuit the loop at that pass.

> **NOTE — `--list-passes` hyphenation is convention, not a read literal.** The variable `listPasses` and the help text "list all backend passes." are both interned, but the ArgStr literal is string-deduplicated and never appears as a standalone rodata token. The flag exists; its exact spelling is inferred from the variable name.

---

## The Driver Flag Catalog

All flags below are `cl::opt`/`cl::list` ArgStrings registered by `sub_7C2890`; the CLI form prefixes `--`. Help text is the verbatim `cl::desc` interned in the `0x1dc1041..0x1dc40f8` belt. A default is shown only where the literal initializer byte is present in the binary. The catalog covers the user-facing surface; a handful of single-anchor internal toggles are omitted.

> **NOTE — flag-name casing is not uniform.** Most flags are hyphen-kebab (`--disable-dram-rotation`), but the **VN-splitter family is snake_case** (`--skip_split_vns`, `--min_split_size`, `--vn_limit`, `--lower_ac_only`, `--max_dup_factor`, `--experimental_use_bloom`, …). This reflects the originating translation unit, not a typo; a reimplementer must preserve the underscores or those flags will not parse.

### Input / Output / Top-Level

| Flag | Type / Default | Description (verbatim `cl::desc`) | Conf |
|---|---|---|---|
| *(positional #1)* `inputFileName` | str | "input file name" | CERTAIN |
| *(positional #2)* `outputFileName` | str | "output file name" | CERTAIN |
| `--neff-output-filename` | str | "The name of the neff file to create, defaults to file.neff" | CERTAIN |
| `--pass` | list\<str\> | "Comma separated pass names" — *the §vocabulary* | CERTAIN |
| `--skip-pass` | list\<str\> | "Skip comma separated passes" | CERTAIN |
| `--start-pass` | str | "Skip all passes before the specified pass" | CERTAIN |
| `--optlevel` | int **=-1** | "Optimization level. Supported value: 0 or 2" | CERTAIN |
| `--waoptlevel` | int | "Optimization level visible only to backend  " | CERTAIN |
| `--allocator` | str **=""** | "Storage Allocator. Supported value: coloring or lsa" | CERTAIN |
| `--smt-allocation` | bool **=false** | "Doing smt allocation" → opt7 SMT pipeline | CERTAIN |
| `--list-passes` | bool **=false** | "list all backend passes." (var `listPasses`) | HIGH |
| `--dry-run` | bool **=false** | "List the passes that wil be run for a given set of inputs" | CERTAIN |
| `--execute-repetition` | int **=1** | "Number of times graph will be executed in one iteration, default is 1" | HIGH |
| `--enable_partitioner` | bool **=false** | "Enable Partitioner" | HIGH |
| `--verbose` / `--logfile-verbose` / `--logfile` | int / int / str | verbosity & log file | HIGH |
| `--jobs` | int | "Limit number of simultaneous of processes and threads." | HIGH |

### Partitioner / Tensorizer Hints

| Flag | Type / Default | Description | Conf |
|---|---|---|---|
| `--disable-loop-opt` | bool **=false** | "disable loop optimizations." | CERTAIN |
| `--enable-internal-partitioner` | bool | "enable HLO partitioner." | CERTAIN |
| `--ignore-tensorizer-parallelfor-hint` | bool | "Ignore tensorizer's hint on parallel for" | CERTAIN |
| `--no-ignore-tensorizer-parallelfor-hint` | bool | "No Ignore any tensorizer's hint on parallel for" | CERTAIN |
| `--keep-tensorizer-engine-assignment` | bool | "Honor engine assignment from tensorizer." | HIGH |

### Allocation / DRAM / Color Rotation

| Flag | Type / Default | Description | Conf |
|---|---|---|---|
| `--fast-context-switch` | bool **=false** | "avoid pinning static weights in favor of faster context switching" | CERTAIN |
| `--disable-dram-rotation` / `--disable-dram-allocation` | bool **=false** | disable dram rotation / allocation | CERTAIN |
| `--dram-page-size` | int | "DRAM page size for GCA allocation aligment" | CERTAIN |
| `--dram-rotation-size` | int | "…rotation maximum memory size for DRAM address rotation, default 8G. -1 = minimum size possible" | CERTAIN |
| `--allreduce-buffer-size` | int | "…CC op pipeline buffer size, default 500 = 500MB. -1 = auto" | CERTAIN |
| `--allreduce-rotation-dis` | int | "Allreduce rotation distance" | CERTAIN |
| `--gca-use-no-spill-hint` | bool **=false** | "GCA use no spill hint from Tensorizer" | CERTAIN |
| `--gca-dma-optlevel` | int **=0** | "LIW GCA reload/spill insertion optimization level" | CERTAIN |
| `--io-to-internal-dmacopy-insertion` | bool | "…copy IO to Internal, to reduce runtime IO descriptor generation overhead" | CERTAIN |
| `--skip-backend-allocation-opt-nki` | bool **=false** | "Skip any backend pass that can potentially alter memorylocation address" | CERTAIN |
| `--nki-enable-sb-allocation` / `--bir-enable-sb-allocation` | bool **=false** | NKI / BIR SB-allocation flow toggles | CERTAIN |
| `--num-semaphores-per-queue` | int **=16** | "Set default count of semaphores per queue." | CERTAIN |
| `--trivial-semaphore-alloc` | bool **=false** | "Enable trivial semaphore allocation." | CERTAIN |

### VN-Splitter (snake_case, BIRVNSplitter family)

| Flag | Type / Default | Description | Conf |
|---|---|---|---|
| `--skip_split_vns` | str **=""** | "A comma-delimited str of vn names to skip." | CERTAIN |
| `--min_split_size` | int | "Incrementally growing the Access Groups to this size (bytes/partition)" | CERTAIN |
| `--vn_limit` | int | "Maximum size (per partition) of each individual split (bytes/partition)" | CERTAIN |
| `--no_split_dram` | bool **=false** | "Do not split the vn if this would …place …in DRAM (perSplitLimit exceeded)" | CERTAIN |
| `--lower_ac_only` | bool **=false** | "Only run the LowerAC pass…bypass BIRVNSplitter, but still handle AbstractCopy" | CERTAIN |
| `--max_dup_factor` | int | "If splitting results in more than max_duplication_factor size increase, do not split…" | CERTAIN |
| `--experimental_use_bloom` | float | "Use bloom filter for AP overlap checking. FPR ∈[0,1]. 0.0→bitarray. Negative→no bloom" | CERTAIN |
| `--split_sb_node` / `--vn_split_dram_node` / `--split_module` / `--split_huge_dram_tensor` / `--sb_batch_simplify` | bool/float | SB / DRAM-node / module split toggles | CERTAIN |

### Scheduler / Loops-on-Chip / LNC

| Flag | Type / Default | Description | Conf |
|---|---|---|---|
| `--loops-on-chip` | bool **=false** | "Compile loops to execute natively on chip" | CERTAIN |
| `--loops-in-backend` | bool | "Keep loops in Backend" | HIGH |
| `--policy` | int **=3** | "post-scheduler policy. 0: none; 2: heuristics; 3: time-aware" | CERTAIN |
| `--interleave` | str **="none"** | scheduler interleave mode | CERTAIN |
| `--enable-engine-balancing` | bool **=false** | "Enable engine balancing optimization." (hidden) | CERTAIN |
| `--enable-hwdge-trigger-engine-scheduling` | bool **=false** | "Enable HWDGE trigger engine scheduling." | CERTAIN |
| `--lnc-aware-scheduler` | bool | "Run LNC-aware scheduler." | CERTAIN |
| `--run-shared-allocation-before-post-sched` | bool | "Run lower_local_collectives and coloring_allocator_dram_shared before post_sched. trn2 only." | CERTAIN |
| `--numcores` / `--scheduler-tuning` / `--schedule-delayed-latency` | int/str/int | scheduler knobs | HIGH |

### DMA / RMW / AXI Alignment

| Flag | Type / Default | Description | Conf |
|---|---|---|---|
| `--AXI-alignment-size-thres` | int **=512** | "SB Tensor smaller then thres aligned to 256-byte start … AXI bus efficiency. Default 512." | CERTAIN |
| `--RMW-alignment` | int **=32** | "SB Tensor aligned to RMW-alignment bytes start … RMW efficiency. Default 32." | CERTAIN |
| `--RMW-alignment-target-arch` | str **="core_v4"** | "Any arch incl/beyond set value triggers RMW alignment. Avail: tonga, sunda,gen3,core4,gen5" | CERTAIN |
| `--cc-dma-alignment-mode` | int | "Mode 0: prefetch max; 1: prefer prefetch DMA+CC; 2: avoid prefetch DMA+CC" | CERTAIN |
| `--coalesce-dma-blocks` | bool **=false** | "Coalesce DMA blocks after lowering." | CERTAIN |
| `--legalize-strided-dma` | bool | "Legalize strided DMA access." | CERTAIN |
| `--dge-levels` | list\<DGElevel\> | "Available DGE Levels:" — enum (cl::values, §Enumerations) | CERTAIN |
| `--dma-qos-class-count` | uint | "Set the number of DMA QoS classes mapped by runtime" | CERTAIN |
| `--dma-qos-for-spill-code` | DMAQoSClass **=Default** | "Set the DMA QoS class used for spill/reload code" — enum | CERTAIN |
| `--dma-order-constraint-file` | str | "A json file specifying desired scheduling order amongst a set of DMA operations." | CERTAIN |
| `--dynamic-dma-scratch-size-per-partition` | int | "The SB scratch size per partition for Dynamic DMA" | CERTAIN |
| `--force-prefetch-follow-incoming-order` | int | "Force prefetch of tensor larger than set <size>… Default -1, off" | CERTAIN |

### Codegen Mode / FP8 / MX / Arch

| Flag | Type / Default | Description | Conf |
|---|---|---|---|
| `--enable-new-backend` | bool | "Run the new BIR codegen passes" | CERTAIN |
| `--enable-backend-passes-only` | bool **=false** | "Run backend (codegen) passes only" | CERTAIN |
| `--enable-mt-backend` | bool **=false** | "Build subgraphs in parallel threads and link" | CERTAIN |
| `--enable-pre-opts` | bool **=false** | "Enable pre-walrus BIR optimization pass" | CERTAIN |
| `--enable-call-graph` | bool **=false** | "Enable function call-graph mode" | CERTAIN |
| `--unified-backend-and-legacy-codegen` | bool | "Run Backend and Legacy Codegen as a single process." | CERTAIN |
| `--enable-separate-load-and-compute` | str **="none"** | "Separate load and compute regions (none\|direct\|indirect). WARNING: Will break program semantics" | CERTAIN |
| `--enable-fp8-nan-suppression` / `--disable-fp8-saturation` | bool | FP8 NaN / saturation behavior | CERTAIN / HIGH |
| `--enable-mx-alternative-emax` | bool | "…MX scales, use alternative calc based on smaller EMAX to increase scale range" | CERTAIN |
| `--enable-arch-legalize` | bool | "…legalization pass that transforms BIR …compatible with the latest architecture version being tested" | CERTAIN |

### LNC / Subgraph Link / Shared-Mem

| Flag | Type / Default | Description | Conf |
|---|---|---|---|
| `--link-subgraphs` | list\<str\> | "Subgraph dir names to load BIRs from, for linking" | CERTAIN |
| `--link-dir` | str **="sgLnk/sg00"** | "Link directory" | CERTAIN |
| `--vnc-nc-per-sengine` | int | "The number of neuron cores targeted for this engine." | CERTAIN |
| `--enable-lnc-single-graph-compilation` | bool | "…input graph may contain symbolic ShardIds corresponding to LNC core ids." | CERTAIN |
| `--lnc1-with-shared-mem` | bool | "Run lnc1 flow but with shared mem allocation for storage improvement." | CERTAIN |
| `--enable-output-completion-notifications` | bool | "Notify the runtime whenever the last write to an output tensor occurs." | CERTAIN |
| `--enable-bir-e2e-compilation` | bool | "bir input of neuronxcc, do not need some of the json files." | CERTAIN |

### BIR Dump / Diagnostics

| Flag | Type / Default | Description | Conf |
|---|---|---|---|
| `--print-after-all` | bool **=false** | "Dump BIR after every pass." | CERTAIN |
| `--print-after` / `--print-before` | list\<str\> | "Dump BIR after/before comma separated passes." | CERTAIN |
| `--print-format` | list\<str\> | "Format(s) for BIR dump: json, condensed, debug. … Default: json." | CERTAIN |
| `--dump-on-error` | bool **=false** | "Dump BIR in case backend aborts." | CERTAIN |
| `--annotate-live-mem` | bool | "Compute per-instruction live memory size and annotate any condensed BIR dumps" | CERTAIN |
| `--generate-ir-signature` / `--ir-sigs-after-passes` / `--ir-sigs-also-dump-bir` | bool / list\<uint\> / bool | IR-signature generation | CERTAIN |
| `--enable-random-init` | bool **=false** | "Init SB with random data" | CERTAIN |

### Memory Analysis (`cl::cat "memory_analysis"`)

| Flag | Type / Default | Description | Conf |
|---|---|---|---|
| `--memory-analysis-dump-all` | bool | "Perform all types of memory analysis and save results" | CERTAIN |
| `--memory-analysis-dump` | list\<enum\> | vals {spilling, live-range, memory-pressure} | CERTAIN |
| `--memory-analysis-mem-types` | list\<enum\> | vals {dram, sb, psum} | CERTAIN |
| `--memory-analysis-after` | list\<str\> | "Restrict memory analysis to run only after specified passes" (pass-name set) | CERTAIN |
| `--memory-analysis-target-tensor` | str **=""** | "Perform memory analysis only during live range of specified tensor." | CERTAIN |

### Verifier / Checkers / Simulator

| Flag | Type / Default | Description | Conf |
|---|---|---|---|
| `--enable-verifier` / `--enable-verifier-for-all` | bool **=false** | "Enable the verifier." / "…for all backend passes" | CERTAIN |
| `--verify-bir-serdes-for-all` | bool **=false** | "verifies BIR serialization/deserialization for all backend passes" | CERTAIN |
| `--enable-checker-after` | list\<str\> | "Enable checker passes (birsim/birverifier) after comma separated passes." (pass-name set) | CERTAIN |
| `--enable-birsim` / `--enable-birsim-after-all` / `--enable-birsim-at-begin` / `--enable-birsim-at-end` / `--enable-birsim-sync-only` | bool **=false** | BIR-simulation placement toggles | CERTAIN |
| `--enable-birsim-with-kernel-inline` | bool **=false** | "Run BIR kernel inline before running BIRSIM …move enableBirSimAtBegin to after unroll." | CERTAIN |
| `--enable-data-race-checker` / `--enable-barrier-checker` | bool | data-race / core-barrier checker | CERTAIN |
| `--enable-indirect-memcpy-bound-check` / `--enable-vector-dge-bound-check` | bool | bound checks | CERTAIN |
| `--disable-hbm-usage-check` | bool | "Disable total HBM usage check" | CERTAIN |
| `--enable-perf-sim` | bool | "Simulates the current instruction order and dumps the estimated latency …" | CERTAIN |
| `--enable-branch-hint` / `--enable-rqi-optim` / `--debug-dve-nan` | bool | perf / RQI / DVE-NaN debug | CERTAIN |

### Debugger (DAP) / Bugpoint

| Flag | Type / Default | Description | Conf |
|---|---|---|---|
| `--bugpoint-throw-after` | str | "For testing only, walrus driver will error after this pass." (pass-name set) | CERTAIN |
| `--run-debugger-after` | str | "Run execute the debugger after the given pass rather than completing compilation" | CERTAIN |
| `--dap-log-file` | str | "File to log dap events to if running as debugger" | CERTAIN |

> **NOTE — debugger pass-name vocabulary is a 7-name subset.** The DAP/debugger entry points recognize a fixed set of pass names interned at `0x1dc4129..0x1dc4178`: `bir_sim`, `birverifier`, `bir_racecheck`, `verify_bir_serdes` (@`0x1dc414b`), `lnc_verifier` (@`0x1dc415d`), `debugger_pass` (@`0x1dc416a`), `bir_sim_with_kernel_inline` (@`0x1dc4178`). All but `debugger_pass` are also in the 150-name `--pass` vocabulary.

> **GOTCHA — there is a *second* tier of hidden per-pass `cl::opt` sub-flags.** Beyond the ~150 driver flags above, hundreds of developer knobs are registered in their *own* translation units (rodata belt `0x1c77000..0x1c8c000`): e.g. `enable-zero-propagation`, `disable-peep-split-select`, `disable-peep-tsp`, `disable-peep-tc`, `disable-activation-accumulate`, `tensor-map`, `dump-postsched-trace`, `birsim-output-tolerance`, `inject-error`. These are mostly hidden and *not* emitted by the production job. The tier's existence is HIGH; the individual flags are LOW/MED and enumerated by name only.

---

## The `--pass` Vocabulary

### Purpose

Every legal `--pass` / `--skip-pass` / `--start-pass` / `--print-after` / `--print-before` / `--enable-checker-after` / `--memory-analysis-after` argument is a **backend pass name**. The complete, closed namespace is the set of `neuronxcc::backend::register_generator_<NAME>__()` free functions — each one returning a `unique_ptr<BackendPass>` from a `PassOptions const&`. `--list-passes` dumps exactly this set via `getPassNames()`; an argument outside it produces `Backend pass: <X> is not registered!`.

### The Count Is 150

The vocabulary size is **150**, reachable three ways in `libwalrus.so`:

1. **Strict mangled match** `_ZN9neuronxcc7backend<len>register_generator_<lc>__E` = **147** symbols.
2. **Plus 3 shortnamed** symbols whose `_ZN…backend` prefix did not survive into the dynamic table, present as `register_generator_<name>__`: `dep_reduction` (`0x3e016a0`), `lnc_barriercheck` (`0x3e05420`), `tensorcopy_accel` (`0x3dff759`) — 147 + 3 = **150**.
3. **Broad mangle-aware capture** over name + demangled fields = **150** distinct names.

> **GOTCHA — count pass *names*, not `register_generator` symbols.** The raw token yields ~750 dynsym hits and a naive per-pass grouping yields ~180, because each pass ships roughly nine symbols: the free function, its lambda `_M_invoke`/`_M_manager` thunks, the `_ZTI`/`_ZTS` typeinfo pair, and `_ptr` variants. The distinct pass-name count — the thing `--pass` accepts — is 150. The helpers `save_outputs`, `set_first_matmults`, and `debug_dump` are *not* `register_generator` passes and are not in the namespace at all.

> **GOTCHA — the flags are singular.** The registered ArgStrings are `--pass` ("Comma separated pass names") and `--skip-pass` ("Skip comma separated passes"). `passes` and `skip-passes` do not exist as standalone ArgStr literals; a reimplementation that accepts the plural spelling accepts something `walrus_driver` never did.

### The 150 Passes by Subsystem

The list below groups the 150 names by function. The `register_generator_<name>__` free-function VA is given for a representative anchor per group; full per-pass addresses are in the [Part 14 pass catalog](../appendix/) (the appendix pass-catalog page is pending).

**Allocators & coloring** (`0x3dff391`–`0x3dff588` band): `coloring_allocator_dram`, `coloring_allocator_dram_debug`, `coloring_allocator_dram_post_lnk`, `coloring_allocator_dram_shared`, `coloring_allocator_dram_shared_post_lnk`, `coloring_allocator_psum`, `coloring_allocator_reg`, `coloring_allocator_sb`, `coloring_allocator_with_loop`, `linear_scan_allocator`, `runtime_memory_reservation`, `do_nothing`.

**Address rotation & DMA placement**: `address_rotation_dram`, `address_rotation_psum`, `address_rotation_psum_post_schedule`, `address_rotation_sb`, `dma_optimization_psum`, `dma_optimization_sb`, `input_dma_coalescing`, `dma_report`, `dma_metrics`, `coalesce_dma_blocks`, `coalesce_multichannel_cc_ops`, `chain_dma_transposes`, `label_dma_qos`, `legalize_cce_dma`, `legalize_strided_dma`.

**Dynamic DMA & DGE**: `dynamic_dma_cleanup`, `dynamic_dma_scan`, `dynamic_dma_setup`, `lower_dynamic_dma`, `lower_generic_indirect`, `insert_dma_switch_queue_instance`.

**Unroll / loops**: `unroll`, `full_unroll_all`, `full_unroll_some`, `full_unroll_memloc_generation`, `heuristic_unroll`, `flatten_small_loops`, `loop_optimization`, `expand_scheduling_units`.

**Schedule**: `pre_sched`, `post_sched`, `prefetch_scheduling_before_sched`, `prefetch_scheduling_after_sched`, `instruction_reorder`, `order_column_tiled_mms`, `order_constraints`, `optimize_queue_switch`.

**Lowering / codegen** (`0x3e03d…` band): `codegen`, `lower_ac`, `lower_act`, `lower_ap`, `lower_branch`, `lower_control`, `lower_dma`, `lower_dve`, `lower_select`, `lower_sync`, `lower_symbolic_inst`, `lower_local_collectives`, `arch_legalize`, `psum_legalization`, `sb_size_legalization`, `non_ssa_legalization`, `legalize_mm_accumulation_groups`, `mem2reg`, `expand_inst_late`, `expand_replication`, `expand_device_print`, `expand_all_engine_pre_alloc_sema_and_reg`, `expand_all_engine_final_pre_codegen`.

**Semaphores / queues / engines / triggers**: `alloc_queues`, `alloc_semaphores`, `synchronizer`, `lower_sync` *(also above)*, `assign_hwdge_engine`, `assign_trigger_engine`, `infer_stream_ids`, `optimize_act_control`, `optimize_prefetch_act_control`.

**Optimizations**: `constant_propagate`, `value_numbering`, `peephole_opts`, `early_peephole_opts`, `dead_code_elim_o0`, `dead_code_elim_o1`, `remove_redundancies`, `remat_optimization`, `tensor_copy_elim`, `tensorcopy_accel`, `seq_inst_opt`, `dep_opt`, `dep_reduction`, `shrink_ml`, `branch_hint`.

**Splitters / partition / VN**: `partitioner`, `bir_splitter`, `vn_splitter`, `lnc_splitter`, `localize_shared_memory`, `shared_mem_cb_insertion`, `extend_shared_lifetimes`, `sync_shared_allocations`, `sync_before_global_cc`.

**Kernel inline / NKI / linking**: `inline_bir_kernel`, `inline_nki_kernel`, `translate_nki_ast_to_bir`, `bir_linker`, `vnc_link`, `vnc_remote_addr_map`, `build_fdeps`.

**Separate load & compute**: `separate_load_and_compute`, `separate_load_and_compute_post_ada`, `separate_load_and_compute_with_memset`.

**Analysis / verify / sim** (`0x3e01f…`, `0x3e054…` bands): `bir_sim`, `bir_sim_with_kernel_inline`, `birverifier`, `bir_racecheck`, `verify_bir_serdes`, `lnc_verifier`, `lnc_barriercheck`, `anti_dependency_analyzer`, `anti_dependency_analyzer_post_shared_dram`, `perf_sim`, `perf_sim_at_end`, `perf_sim_package_pass`, `oom_checker`, `hbm_usage`, `report_stats`, `hasher`, `dumper`, and the ten `memory_analysis_after_*` passes (`…address_rotation_{dram,psum,sb}`, `…coloring_allocator_{dram_post_lnk,dram_shared,sb}`, `…dma_optimization_sb`, `…{post_sched,pre_sched,unroll}`).

**Debug / inject / packaging**: `debug_dve_nan`, `debug_init_mem`, `debug_onchip`, `error_injector`, `address_tensor_content_dump`, `address_tensor_insertion`, `insert_ptcom_flat`, `pre_opts`, `neff_packager`.

---

## Enum-Typed Flag Values (`cl::values`)

Three flags take an enumerated value set, registered with `cl::values`. The value-name literals are all read directly from rodata.

`--dge-levels` (DGE-feature bitset, "Available DGE Levels:"): `spill_reload`, `scalar_dynamic_offset`, `vector_dynamic_offsets`, `dynamic_size`, `dst_reduce`, `transpose`.

`--memory-analysis-dump`: `spilling` | `live-range` | `memory-pressure`.

`--memory-analysis-mem-types`: `dram` | `sb` | `psum`.

`--dma-qos-for-spill-code` (`bir::DMAQoSClass`): "Use QoS class 0" … "Use QoS class 14", plus `Default` ("Use the runtime's default QoS") and a "Make no changes to the QoS class" sentinel; default initializer = `Default`.

> **GOTCHA — `--dge-levels` names and help texts are laid out off-by-one in rodata.** The value names above are the reliable part; the value→description *pairing* as it appears in the string belt is shifted by one slot and is reproduced here as laid out, not as corrected. Do not read a name's neighbouring help string as its documentation.

---

## The Job Argv Template

### Purpose

The Cython `WalrusDriver` Job (`neuronxcc.driver.jobs.WalrusDriver.WalrusDriver`) is what actually constructs the `walrus_driver` argv in production. It emits two families of flags, and the distinction matters for a reimplementer: most flags pass through unchanged, but a large `--internal-*`/`--enable-internal-*` family is **driver-level convenience flags that the Job translates** into the native flags — those translated names are *not* in libwalrus's `cl::opt` set.

### Algorithm

```c
// runWalrusDriver argv builder @ WalrusDriver.cpython-310.so 0x8ddf0 (Cython, illustrative)
function build_walrus_argv(self, state):
    argv = ["walrus_driver", input_bir]          // input = bir.json / walrus_bir.out.json
    // (A) NATIVE pass-through — identical ArgStr on walrus_driver:
    emit("--pass", join(self.walrus_passes))      // the §vocabulary list
    emit("--optlevel", self.optlevel)
    emit("--allocator", self.allocator)           // or --smt-allocation
    emit("--neff-output-filename", ...)
    emit_if(self.print_after_all,  "--print-after-all")
    emit("--enable-checker-after", ...); emit("--enable-birsim...", ...)
    emit("--memory-analysis-dump=memory-pressure"); emit("--memory-analysis-mem-types=dram")
    emit("--dma-qos-class-count", ...); emit("--dge-levels", ...); ...
    // (B) DRIVER-LEVEL --internal-* — TRANSLATED to native, never seen by walrus_driver:
    //   --internal-smt-allocation        -> --smt-allocation
    //   --internal-print-after           -> --print-after
    //   --internal-max-instruction-limit -> --max-instruction-limit
    //   --enable-internal-fork-walrus    -> (chooses embedded vs subprocess; NOT forwarded)
    //   --enable-internal-walrus-replay-script -> emit replay_walrus.sh
    return argv
```

### Considerations

The Job emits ~203 distinct `--flags` across the two families. The pass-through set (A) is the bulk of what reaches `walrus_driver`; the `--internal-*` set (B) lives on the Job and is mapped to native flags by name correspondence. The `--internal-X → native` translation map rests on emitted-string evidence and name correspondence rather than a full decompile of the Cython argv builder, so the pairs shown above are HIGH and the map is not claimed complete. When `--enable-internal-walrus-replay-script` is set, the Job writes a `#!/bin/bash` script `replay_walrus.sh` reproducing the exact `walrus_driver` invocation and `chmod`s it executable — the production replay/debug aid.

---

## Binary Anchors

The central claims on this page and where each is pinned in `libwalrus.so`:

| Claim | Anchor |
|---|---|
| 150-pass vocabulary | `nm -D \| rg -o 'register_generator_([a-z0-9_]+)__'` → 150 distinct names; 147 strict-mangled + `dep_reduction`@`0x3e016a0`, `lnc_barriercheck`@`0x3e05420`, `tensorcopy_accel`@`0x3dff759` |
| `run_backend_driver` exported | `nm -D` → `00000000008113b0 T _Z18run_backend_driveriPPc` |
| Subprocess-vs-embedded split | `WalrusDriver.so` carries both `runWalrusDriver` and `runEmbeddedWalrusDriver` plus strings `walrus_driver`, `--enable-internal-fork-walrus`, `replay_walrus.sh`, `#!/bin/bash`; `EmbeddedWalrusDriver.so` imports `libwalrus.so` + `_Z18run_backend_driveriPPc` |
| `--pass` singular | ArgStrs `pass`/`skip-pass`/`start-pass` present; help "Comma separated pass names"@`0x1dc10b8`, "Skip comma separated passes"@`0x1dc10d8` |
| `hasGenerator` validation | `hasGenerator`@`0x1dc5960`, `GeneratorRegistration12hasGenerator…` symbol@`0x1643a3`, `" is not registered!"`@`0x1dc58cf` |

Two areas remain short of that standard and are marked as such above. The optlevel pipeline-builder VAs (`sub_80A6E0` / `806F80` / `807EF0` / `809580` / `80D9D0`) are decompiler-internal labels in a stripped binary — the addresses are solid, the *names* are not symtab-backed. And the `--internal-*`→native translation map is name-correspondence work, HIGH on the pairs shown and incomplete beyond them.

---

## Related Components

| Name | Relationship |
|---|---|
| `CompileCommand` pipeline | Builds the Job list; the `WalrusDriver` Job spawns this CLI — see [3.3](compilecommand-pipeline.md) |
| `JobRegistry` | Maps the `WalrusDriver` name to the Job class that builds the argv — see [job-registry.md](job-registry.md) |
| libwalrus backend | Owns all 150 passes the `--pass` vocabulary names — see [Part 8](../walrus/) |
| Pass catalog | Per-pass addresses, algorithms, and roster — see [Part 14](../appendix/) (pending) |

## Cross-References

- [CompileCommand Pipeline & the Canonical Job Order](compilecommand-pipeline.md) — the driver that emits the `WalrusDriver` Job and the `--walrus-passes` it forwards
- [The Compile Pipeline at a Glance](../front/pipeline.md) — `WalrusDriver` is one Job, not a chain; where this CLI sits in the process pipeline
- [Part 8 — The libwalrus Backend](../walrus/) — the 150 passes, allocators, schedulers, and the optlevel pipeline builders this CLI selects
- [Opt-Level Planes](opt-level-planes.md) — `--optlevel`/`--allocator`/`--smt-allocation` and the pipelines they choose (3.10)
- [Part 14 — Pass Catalog](../appendix/) — per-pass `register_generator_<name>__` addresses and algorithms (appendix page pending)
