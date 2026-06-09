# CLI Flag Inventory

cicc v13.0 accepts approximately **117 unique flag keys** across five parsing sites (111 documented in the catalog tables + 6 stubbed at the end of this page: `-extra-device-vectorization`, `-covinfo`, `-profinfo`, `-profile-instr-use`, `-global-mem`, `-qualified`), expanding to ~148 flag+value combinations when counting value variants, and ~175 when including all architecture triplets. Flags are parsed in `sub_8F9C90` (real main), `sub_900130` (LibNVVM path A), `sub_12CC750`/`sub_9624D0` (LibNVVM option processors), and `sub_12C8DD0` (flag catalog builder with 65 registered configurations).

The flag system is architecturally split into two layers: a **hardcoded dispatch** layer in the top-level parsers (`sub_8F9C90`, `sub_900130`, `sub_12CC750`/`sub_9624D0`) that handles mode selection, pass-through, LTO, and structural flags via `strcmp`/prefix-match chains; and a **BST-backed catalog** layer (`sub_12C8DD0` + `sub_95EB40`/`sub_12C8B40`) that handles all flags whose effect is purely "store a value and forward strings to output vectors."

## The Four Output Vectors

Every flag ultimately routes its effects into one or more of four output `std::vector<std::string>` buffers. These vectors are the sole interface between the CLI parser and the downstream pipeline stages:

| Vector | Seed | Output args | Downstream stage |
|--------|------|-------------|------------------|
| `v324` (lnk) | `"lnk"` | `a5`/`a6` | Phase 1: Linker / IR-link (`sub_906xxx`) |
| `v327` (opt) | `"opt"` | `a7`/`a8` | Phase 2: Optimizer (LLVM opt / `sub_12E54A0`) |
| `v330` (lto) | (none) | `a9`/`a10` | Phase 3: LTO passes |
| `v333` (llc) | `"llc"` | `a11`/`a12` | Phase 4: LLC codegen |

Each vector element is a 32-byte `std::string` with SSO. At function exit (lines ~1462-1553 of `sub_9624D0`), each vector is serialized: count = `(end - begin) >> 5`, then `malloc(8 * count)` for the `char**` array, with each string individually `malloc(len+1)` + `memcpy` + null-terminated.

The lto vector receives no seed string and is only populated by explicit LTO flags (`-Xlto`, `-olto`, `-gen-lto`, `-link-lto`, `--device-c`, `--force-device-c`, host-ref flags) and the architecture string.

## Mode Selection

The top-level entry point `sub_8F9C90` sets a mode variable `v263` that selects the compilation pipeline:

| Flag | Mode | Description |
|------|------|-------------|
| `-lgenfe` | 1 | EDG C++ frontend (legacy genfe path) |
| `-libnvvm` | 2 | LibNVVM API path |
| `-lnk` | 3 | Linker path (forces `keep=true`) |
| `-opt` | 4 | Optimizer-only path (forces `keep=true`) |
| `-llc` | 6 | LLC backend-only path |

Within the LibNVVM option processors (`sub_12CC750`/`sub_9624D0`), the first argument is checked as a 4-byte or 8-byte integer for phase routing. Phase routing is stored at `a1+240`:

| argv[0] hex | String | Phase ID | a1+240 |
|-------------|--------|----------|--------|
| `0x6B6E6C2D` | `-lnk` | 1 | 1 |
| `0x74706F2D` | `-opt` | 2 | 2 |
| `0x636C6C2D` | `-llc` | 3 | 3 |
| `0x63766E2D` | `-nvc` | 3 | 3 (alias) |
| `0x6D76766E62696C2D` | `-libnvvm` | 4 | 4 |

When phase routing is active (`a1+240 != 0`), `sub_95C880(phase_id, argc, argv, &count, &mode_flags)` returns the allocated argv array for that single phase, stored directly into the corresponding output pair. When `a1+240 == 0`, mode flags default to 7 (all phases), and the full multi-phase option parsing loop runs.

## The BST-Backed Flag Catalog

### Catalog construction: `sub_95EB40` / `sub_12C8DD0`

The function `sub_95EB40(a1, cl_mode_flag)` (standalone path) or `sub_12C8DD0` (LibNVVM path) builds a `std::map<std::string, OptionEntry>` at `a1+248`. The underlying data structure is a C++ red-black tree (the standard library `std::map` implementation), with the tree root at `a1+248`, the sentinel/end node at `a1+256`, and the node count at `a1+288`.

Registration is performed by 65 calls to `sub_95E8B0` + `sub_95BF90` (standalone) or `sub_12C8B40` (LibNVVM). Each call inserts one BST node.

### BST node layout (168 bytes)

Each node in the red-black tree has the following layout:

| Offset | Size | Content |
|--------|------|---------|
| +0 | 24 | RB-tree metadata (color, parent, left, right pointers) |
| +32 | 32 | **Key**: flag name string (`std::string` with SSO) |
| +64 | 32 | **lnk forwards**: space-separated flags for lnk vector |
| +96 | 32 | **opt forwards**: space-separated flags for opt vector |
| +128 | 32 | **llc forwards**: space-separated flags for llc vector |
| +160 | 8 | **Value pointer**: points to the offset in the options structure where the flag's current value is stored |

### BST lookup: `sub_95D600` / `sub_12C8530`

When the main parsing loop encounters a flag string, it calls `sub_95D600` (standalone) or `sub_12C8530` (LibNVVM) to perform a standard `std::map::lower_bound`-style traversal of the red-black tree. The lookup compares the input flag string against registered key strings at node offset +32 using `strcmp` semantics. On match, the node's three forwarding strings (lnk/opt/llc) are split on spaces and appended to their respective output vectors.

### Duplicate detection

Each BST node's value pointer points into the options structure. If the value storage already has a non-zero sentinel (the QWORD immediately following the 32-byte STR32 slot), the flag was already set. On duplicate:
```text
"libnvvm : error: <flag> defined more than once"
```

### Flags NOT in the catalog

The following flag categories are handled by hardcoded `strcmp`/prefix-match chains in the main parsing loop BEFORE the catalog lookup, and therefore bypass the BST entirely:

- Mode selection flags (`-lnk`, `-opt`, `-llc`, `-nvc`, `-libnvvm`)
- `-Ofast-compile=<level>` (parsed at lines ~690-833)
- Pass-through flags (`-Xopt`, `-Xllc`, `-Xlnk`, `-Xlto`)
- LTO flags (`-lto`, `-gen-lto`, `-gen-lto-and-llc`, `-link-lto`, `-olto`, `-gen-opt-lto`, `--trace-lto`)
- Device compilation flags (`--device-c`, `--force-device-c`, `--partial-link`)
- Host reference flags (`-host-ref-{ec,eg,ek,ic,ig,ik}`)
- `-maxreg=<N>` (has its own duplicate-check logic at `a1+1200`)
- `-split-compile=<N>`, `-split-compile-extended=<N>` (at `a1+1480`/`a1+1488`)
- `-opt-passes=<pipeline>` (at `a1+1512`/`a1+1520`)
- `-discard-value-names=<0|1>` (complex multi-phase interaction)
- `-time-passes` (must be sole flag; unsupported in LibNVVM API path)
- `-cl-mode` (sets `v278=1`, affects routing for `-prec-div`, `-fast-math`, `-prec-sqrt`)
- `-jump-table-density=<N>` (forwarded directly to llc)
- `-jobserver` (forwarded to opt)
- `--emit-optix-ir` (disables ip-msp + licm, sets `a13=0x43`)
- `--nvvm-64`, `--nvvm-32` (handled in `sub_95C230`)

If none of the hardcoded checks match and the BST lookup also fails, the flag falls through to the catchall entry at options structure offset +1256, which triggers:
```text
"libnvvm : error: <flag> is an unsupported option"
```

## Complete Flag-to-Pipeline Vector Routing Table

The table below documents every flag's routing from user input to the four output vectors. "Store" indicates the options structure offset where the value is recorded. Flags marked with **[BST]** are registered in the catalog; flags marked with **[HC]** are hardcoded in the parsing loop.

### Architecture Flags [BST]

All 24 architecture entries share options structure offset +552 and follow the same 3-column pattern:

| User flag | lnk vector | opt vector | llc vector |
|-----------|-----------|-----------|-----------|
| `-arch=compute_75` | `-R __CUDA_ARCH=750` | `-opt-arch=sm_75` | `-mcpu=sm_75` |
| `-arch=compute_80` | `-R __CUDA_ARCH=800` | `-opt-arch=sm_80` | `-mcpu=sm_80` |
| `-arch=compute_86` | `-R __CUDA_ARCH=860` | `-opt-arch=sm_86` | `-mcpu=sm_86` |
| `-arch=compute_87` | `-R __CUDA_ARCH=870` | `-opt-arch=sm_87` | `-mcpu=sm_87` |
| `-arch=compute_88` | `-R __CUDA_ARCH=880` | `-opt-arch=sm_88` | `-mcpu=sm_88` |
| `-arch=compute_89` | `-R __CUDA_ARCH=890` | `-opt-arch=sm_89` | `-mcpu=sm_89` |
| `-arch=compute_90` | `-R __CUDA_ARCH=900` | `-opt-arch=sm_90` | `-mcpu=sm_90` |
| `-arch=compute_90a` | `-R __CUDA_ARCH=900` | `-opt-arch=sm_90a` | `-mcpu=sm_90a` |
| `-arch=compute_100` | `-R __CUDA_ARCH=1000` | `-opt-arch=sm_100` | `-mcpu=sm_100` |
| `-arch=compute_100a` | `-R __CUDA_ARCH=1000` | `-opt-arch=sm_100a` | `-mcpu=sm_100a` |
| `-arch=compute_100f` | `-R __CUDA_ARCH=1000` | `-opt-arch=sm_100f` | `-mcpu=sm_100f` |
| `-arch=compute_103` | `-R __CUDA_ARCH=1030` | `-opt-arch=sm_103` | `-mcpu=sm_103` |
| `-arch=compute_103a` | `-R __CUDA_ARCH=1030` | `-opt-arch=sm_103a` | `-mcpu=sm_103a` |
| `-arch=compute_103f` | `-R __CUDA_ARCH=1030` | `-opt-arch=sm_103f` | `-mcpu=sm_103f` |
| `-arch=compute_110` | `-R __CUDA_ARCH=1100` | `-opt-arch=sm_110` | `-mcpu=sm_110` |
| `-arch=compute_110a` | `-R __CUDA_ARCH=1100` | `-opt-arch=sm_110a` | `-mcpu=sm_110a` |
| `-arch=compute_110f` | `-R __CUDA_ARCH=1100` | `-opt-arch=sm_110f` | `-mcpu=sm_110f` |
| `-arch=compute_120` | `-R __CUDA_ARCH=1200` | `-opt-arch=sm_120` | `-mcpu=sm_120` |
| `-arch=compute_120a` | `-R __CUDA_ARCH=1200` | `-opt-arch=sm_120a` | `-mcpu=sm_120a` |
| `-arch=compute_120f` | `-R __CUDA_ARCH=1200` | `-opt-arch=sm_120f` | `-mcpu=sm_120f` |
| `-arch=compute_121` | `-R __CUDA_ARCH=1210` | `-opt-arch=sm_121` | `-mcpu=sm_121` |
| `-arch=compute_121a` | `-R __CUDA_ARCH=1210` | `-opt-arch=sm_121a` | `-mcpu=sm_121a` |
| `-arch=compute_121f` | `-R __CUDA_ARCH=1210` | `-opt-arch=sm_121f` | `-mcpu=sm_121f` |

Note: the `a` and `f` sub-variants share the base SM number for `__CUDA_ARCH` (e.g., `sm_100a` and `sm_100f` both emit `__CUDA_ARCH=1000`) but get distinct `-opt-arch=` and `-mcpu=` strings. The architecture string is also stored into the lto vector via `sub_95D700`, preserving the full `-arch=compute_XX` string.

### Architecture validation bitmask

Architecture is validated at `a1+8` using bitmask `0x60081200F821`:
```c
offset = SM_number - 75
if (offset > 0x2E || !_bittest64(&0x60081200F821, offset))
    -> ERROR: "is an unsupported option"
```

Valid bit positions:

| Bit | SM | Generation |
|-----|-----|-----------|
| 0 | 75 | Turing |
| 5 | 80 | Ampere |
| 11 | 86 | Ampere |
| 12 | 87 | Jetson Orin |
| 13 | 88 | Ada |
| 14 | 89 | Ada Lovelace |
| 15 | 90 | Hopper |
| 25 | 100 | Blackwell |
| 28 | 103 | Blackwell+ |
| 35 | 110 | Post-Blackwell |
| 45 | 120 | Next-gen |
| 46 | 121 | Next-gen |

Maximum offset: `0x2E` = 46 (SM 121). All pre-Turing architectures (SM 70 and below) are rejected.

### Architecture specification forms

Architecture can be specified in many forms, all converging to a numeric SM value. Trailing `a` or `f` suffixes are stripped before numeric parsing. On parse failure: `"Unparseable architecture: <val>"`.

| Form | Example | Source |
|------|---------|--------|
| `-arch <val>` | `-arch sm_90` | `sub_8F9C90` |
| `-arch<val>` | `-archsm_90` | `sub_8F9C90` (compact) |
| `--nv_arch <val>` | `--nv_arch sm_100a` | `sub_8F9C90` |
| `-mcpu=sm_<N>` | `-mcpu=sm_90` | LLVM-style |
| `-opt-arch=sm_<N>` | `-opt-arch=sm_90` | Optimizer |
| `-arch=compute_<N>` | `-arch=compute_100` | Compute capability |
| `__CUDA_ARCH=<N>` | `__CUDA_ARCH=900` | Raw define |

Hex-encoded flag checks in `sub_8F9C90`:
- `0x6D733D7570636D2D` = `-mcpu=sm`
- `0x6372612D74706F2D` = `-opt-arc`
- `0x6F633D686372612D` = `-arch=co`
- `0x6372615F766E2D2D` = `--nv_arc`

### Optimization Level Flags

| User flag | Type | Store | lnk | opt | llc | Default |
|-----------|------|-------|-----|-----|-----|---------|
| `-opt=0` | [BST] | +392 | — | — | — | |
| `-opt=1` | [BST] | +392 | — | — | — | |
| `-opt=2` | [BST] | +392 | — | — | — | |
| `-opt=3` | [BST] | +392 | — | — | — | **default** |
| `-Osize` | [BST] | +488 | — | `-Osize` | `-Osize` | off |
| `-Om` | [BST] | +520 | — | `-Om` | `-Om` | off |
| `-disable-allopts` | [BST] | +424 | `-lnk-disable-allopts` | `-opt-disable-allopts` | `-llc-disable-allopts` | off |
| `-disable-llc-opts` | [BST] | +840 | — | — | — | off |

The `-opt=<N>` flags do not directly emit to any vector at registration time. Instead, at the routing stage (lines 1444-1563 of `sub_9624D0`), the optimization level drives one of three code paths:

1. **Custom pipeline set** (`a1+1520 != 0`): emits `-passes=<pipeline_string>` to opt vector
2. **Normal mode** (`a1+1520 == 0`, `a1+1640 == 0`): emits `-O<level>` to opt vector
3. **Fast-compile mode** (`a1+1640 != 0`): emits `-optO<level>` + `-llcO2` to llc vector

### Floating Point Control Flags

| User flag | Type | Store | lnk | opt | llc | Default |
|-----------|------|-------|-----|-----|-----|---------|
| `-ftz=0` | [BST] | +584 | — | — | — | **default** |
| `-ftz=1` | [BST] | +584 | `-R __CUDA_FTZ=1` | `-nvptx-f32ftz` | `-nvptx-f32ftz` | |
| `-prec-sqrt=0` | [BST] | +616 | — | — | `-nvptx-prec-sqrtf32=0` | CL default |
| `-prec-sqrt=1` | [BST] | +616 | `-R __CUDA_PREC_SQRT=1` | — | `-nvptx-prec-sqrtf32=1` | **CUDA default** |
| `-prec-div=0` (CL) | [BST] | +648 | — | `-opt-use-prec-div=false` | `-nvptx-prec-divf32=0` | |
| `-prec-div=0` (CUDA) | [BST] | +648 | — | `-opt-use-prec-div=false` | `-nvptx-prec-divf32=1` | |
| `-prec-div=1` (CL) | [BST] | +648 | — | `-opt-use-prec-div=true` | `-nvptx-prec-divf32=1` | |
| `-prec-div=1` (CUDA) | [BST] | +648 | `-R __CUDA_PREC_DIV=1` | `-opt-use-prec-div=true` | `-nvptx-prec-divf32=2` | **default** |
| `-prec-div=2` | [BST] | +648 | — | — | `-nvptx-prec-divf32=3` | |
| `-fma=0` | [BST] | +680 | — | — | `-nvptx-fma-level=0` | |
| `-fma=1` | [BST] | +680 | — | — | `-nvptx-fma-level=1` | **default** |
| `-enable-mad` | [BST] | +712 | — | — | `-nvptx-fma-level=1` | off |
| `-opt-fdiv=0` | [BST] | +456 | — | `-opt-fdiv=0` | — | **default** |
| `-opt-fdiv=1` | [BST] | +456 | — | `-opt-fdiv=1` | — | |
| `-no-signed-zeros` | [BST] | +1160 | — | `-opt-no-signed-zeros` | — | off |

Note on `-prec-div`: the CUDA vs CL distinction is controlled by the magic cookie `a4` (0xABBA = CUDA, 0xDEED = OpenCL). CUDA `-prec-div=1` maps to `-nvptx-prec-divf32=2` (IEEE-correct division), while CL maps to level 1 (software approximation). When `-prec-div=0` is set under CUDA, it still maps to `-nvptx-prec-divf32=1` (not 0), because CUDA never drops below software approximation.

### Fast Math Aggregate Flags

| User flag | Type | Store | lnk | opt | llc |
|-----------|------|-------|-----|-----|-----|
| `-unsafe-math` | [BST] | +744 | `-R FAST_RELAXED_MATH=1` `-R __CUDA_FTZ=1` | `-opt-use-fast-math` `-nvptx-f32ftz` | `-nvptx-fma-level=1` `-nvptx-f32ftz` |
| `-fast-math` (CL) | [BST] | +776 | `-R FAST_RELAXED_MATH=1` `-R __CUDA_FTZ=1` | `-opt-use-fast-math` `-nvptx-f32ftz` | `-nvptx-f32ftz` |
| `-fast-math` (CUDA) | [BST] | +776 | `-R __CUDA_USE_FAST_MATH=1` | `-opt-use-fast-math` | — |

`-unsafe-math` always sets FTZ in the backend (`-nvptx-f32ftz`), while CUDA `-fast-math` does not touch the backend FTZ flag — it only sets the preprocessor define and the optimizer flag.

### Debug and Diagnostic Flags

| User flag | Type | Store | lnk | opt | llc | Default |
|-----------|------|-------|-----|-----|-----|---------|
| `-g` | [BST] | +296 | `-debug-compile` | `-debug-compile` | — | off |
| `-generate-line-info` | [BST] | +328 | — | `-generate-line-info` | — | off |
| `-no-lineinfo-inlined-at` | [BST] | +360 | — | — | `-line-info-inlined-at=0` | off |
| `-show-src` | [BST] | +808 | — | — | `-nvptx-emit-src` | off |
| `-enable-verbose-asm` | [BST] | +1224 | — | — | `-asm-verbose` | off |
| `-w` | [BST] | +872 | — | `-w` | `-w` | off |
| `-Werror` | [BST] | +904 | — | `-Werror` | `-Werror` | off |
| `-debug-compile` | [BST] | +296 | — | `-debug-compile` | — | off |
| `-line-info-inlined-at=0` | alias | — | — | — | `-line-info-inlined-at=0` | off |
| `-inline-info` | [HC] | — | — | `-pass-remarks=inline` `-pass-remarks-missed=inline` `-pass-remarks-analysis=inline` | — | off |

### Inlining and Function Flags

| User flag | Type | Store | lnk | opt | llc | Default |
|-----------|------|-------|-----|-----|-----|---------|
| `-disable-inlining` | [BST] | +1064 | — | `-disable-inlining` | — | off |
| `-aggressive-inline` | [BST] | +1608 | — | `-inline-budget=40000` | — | off |
| `-restrict` | [BST] | +1096 | — | — | `-nvptx-kernel-params-restrict` | off |
| `-allow-restrict-in-struct` | [BST] | +1128 | — | `-allow-restrict-in-struct` | `-allow-restrict-in-struct` | off |
| `-enable-opt-byval` | [BST] | +1032 | — | `-enable-opt-byval` | — | off |

### Optimization Control Flags

| User flag | Type | Store | lnk | opt | llc | Default |
|-----------|------|-------|-----|-----|-----|---------|
| `-opt-disable-allopts` | derived | — | — | `-opt-disable-allopts` | — | off |
| `-lnk-disable-allopts` | derived | — | `-lnk-disable-allopts` | — | — | off |
| `-llc-disable-allopts` | derived | — | — | — | `-llc-disable-allopts` | off |

These three are emitted by `-disable-allopts` (see above); they do not exist as independent user flags.

### Rematerialization Flags

| User flag | Type | Store | lnk | opt | llc |
|-----------|------|-------|-----|-----|-----|
| `-vasp-fix` | [BST] | +1352 | — | — | `-vasp-fix1=true -vasp-fix2=true` |
| `-new-nvvm-remat` | [BST] | +1384 | — | — | `-enable-new-nvvm-remat=true -nv-disable-remat=true -rp-aware-mcse=true` |
| `-disable-new-nvvm-remat` | [BST] | +1416 | — | — | `-enable-new-nvvm-remat=false -nv-disable-remat=false -rp-aware-mcse=false` |
| `-disable-nvvm-remat` | [BST] | +1448 | — | — | `-enable-new-nvvm-remat=false -nv-disable-remat=true -rp-aware-mcse=false` |

These are multi-flag compound emissions. Note the subtle difference: `-disable-nvvm-remat` sets `-nv-disable-remat=true` (disables classic remat) but `-enable-new-nvvm-remat=false` (also disables new remat), while `-disable-new-nvvm-remat` disables both new remat AND classic remat AND register-pressure-aware MCSE.

### Analysis and Transform Control Flags

| User flag | Type | Store | lnk | opt | llc |
|-----------|------|-------|-----|-----|-----|
| `-no-aggressive-positive-stride-analysis` | [BST] | +1544 | — | `-aggressive-positive-stride-analysis=false` | — |
| `disable-load-select-transform` | [BST] | +1576 | — | `-disable-load-select-transform=true` | — |

Note: `disable-load-select-transform` is registered WITHOUT a leading `-` in the catalog.

### Pass-Through (Forwarding) Flags [HC]

| Flag | Target vector | Special handling |
|------|---------------|------------------|
| `-Xopt <arg>` | opt | If `<arg>` starts with `-opt-discard-value-names=`, extracts value; if `"1"`, sets `v276=false` |
| `-Xllc <arg>` | llc | None |
| `-Xlnk <arg>` | lnk | If `<arg>` starts with `-lnk-discard-value-names=`, extracts value; if `"1"`, sets `v275=false` |
| `-Xlto <arg>` | lto | If `<arg>` starts with `-lto-discard-value-names=`, extracts value; if `"1"`, sets `v282=false` |

Each consumes the next argument from argv.

### LTO Flags [HC]

| User flag | a13 bitmask effect | lto vector | Notes |
|-----------|-------------------|-----------|-------|
| `-lto` | `(a13 & 0x300) \| 0x23` | — | Full LTO mode |
| `-gen-lto` | `(a13 & 0x300) \| 0x21` | `-gen-lto` | Emit LTO bitcode |
| `-gen-lto-and-llc` | `a13 \|= 0x20` | `-gen-lto` | Emit LTO + run LLC |
| `-link-lto` | `(a13 & 0x300) \| 0x26` | `-link-lto` | Link LTO modules |
| `-olto` | — | `-olto` + argv[i+1] | Takes next arg as LTO opt level |
| `-gen-opt-lto` | sets `v280=1` | — | Affects lowering at end of parsing |
| `--trace-lto` | — | `--trace` | LTO tracing |

### Device Compilation Flags [HC]

| User flag | lto vector |
|-----------|-----------|
| `--device-c` | `--device-c` |
| `--force-device-c` | `--force-device-c` |
| `--partial-link` | (no-op, consumed but not forwarded) |

### Host Reference Flags [HC]

| User flag | lto vector |
|-----------|-----------|
| `-host-ref-ek=<val>` | `-host-ref-ek=<val>` |
| `-host-ref-ik=<val>` | `-host-ref-ik=<val>` |
| `-host-ref-ec=<val>` | `-host-ref-ec=<val>` |
| `-host-ref-ic=<val>` | `-host-ref-ic=<val>` |
| `-host-ref-eg=<val>` | `-host-ref-eg=<val>` |
| `-host-ref-ig=<val>` | `-host-ref-ig=<val>` |
| `-has-global-host-info` | `-has-global-host-info` |

### Pipeline Control Flags [HC]

| User flag | Store | Routing | Default |
|-----------|-------|---------|---------|
| `-opt-passes=<pipeline>` | +1512 | opt: `-passes=<pipeline>` (overrides `-O<N>`) | unset |
| `-passes=<pipeline>` | — | opt: `-passes=<pipeline>` (`sub_9624D0` only) | unset |
| `-lsa-opt=0` | — | opt: `-lsa-opt=0` | generated by `-Ofast-compile=max` or CL-mode |
| `-memory-space-opt=0` | — | opt: `-memory-space-opt=0` | generated by `-Ofast-compile=max` |
| `-memory-space-opt=1` | — | opt: `-memory-space-opt=1` | generated when opt level allows |
| `-rox-opt=0` | — | opt: `-rox-opt=0` | generated when `-prec-div=0` or `-prec-sqrt=0` (non-CL) |
| `-do-ip-msp=<0\|1>` | — | opt: `-do-ip-msp=<val>` | |
| `-do-licm=<0\|1>` | — | opt: `-do-licm=<val>` | |
| `-optimize-unused-variables` | — | lto: `-optimize-unused-variables` | off |

### Ofast-compile Levels [HC]

Stored at `a1+1640`. Only ONE `-Ofast-compile=` is allowed; a second triggers `"libnvvm : error: -Ofast-compile specified more than once"`.

| Level string | a1+1640 | Description | Side effects |
|-------------|---------|-------------|--------------|
| `"0"` | 1 (then reset to 0) | Disabled | opt: fast-compile=off string |
| `"min"` | 4 | Minimal speedup | opt: `-fast-compile=min` |
| `"mid"` | 3 | Medium speedup | opt: `-fast-compile=mid` + second flag |
| `"max"` | 2 | Maximum speedup | opt: `-fast-compile=max`; forces `-lsa-opt=0`, `-memory-space-opt=0` |

When `-Ofast-compile` is active (level >= 1), the `-passes=`/`-O` routing is bypassed. Instead: `-optO<level>` and `-llcO2` are emitted to the llc vector (lines 1453-1460).

### Miscellaneous Flags [HC]

| User flag | Store | Routing | Notes |
|-----------|-------|---------|-------|
| `-maxreg=<N>` | +1192 | opt: `-maxreg=<N>`, llc: `-maxreg=<N>` | Error on duplicate |
| `-split-compile=<N>` | +1480 | opt: `-split-compile=<N>` | Error on duplicate |
| `-split-compile-extended=<N>` | +1480 | opt: `-split-compile-extended=<N>`, sets `a1+1644=1` | Same storage as -split-compile |
| `-jump-table-density=<N>` | — | llc: `-jump-table-density=<N>` | |
| `-jobserver` | — | opt: `-jobserver` | |
| `-cl-mode` | — | No forwarding; sets `v278=1` | Affects `-prec-div`, `-prec-sqrt`, `-fast-math` routing |
| `-time-passes` | — | Unsupported in LibNVVM API (error if `a14 != NULL`) | Must be sole flag |
| `--emit-optix-ir` | — | opt: `-do-ip-msp=0`, opt: `-do-licm=0`; `a13 = (a13 & 0x300) \| 0x43` | |
| `--nvvm-64` | — | `a13 \|= 0x100` | 64-bit NVVM mode |
| `--nvvm-32` | — | `a13 \|= 0x200` | 32-bit NVVM mode |

### Discard-Value-Names [HC]

This flag has the most complex interaction logic in the parser. Seven boolean tracking variables control its behavior:

| Variable | Meaning |
|----------|---------|
| `v275` | lnk-discard-value-names override (from `-Xlnk`) |
| `v276` | opt-discard-value-names override (from `-Xopt`) |
| `v277` | global discard-value-names flag was used |
| `v278` | CL-mode detected |
| `v279` | `-Xlnk` was used for discard-value-names |
| `v281` | `-Xlto` was used for discard-value-names |
| `v282` | lto-discard-value-names override (from `-Xlto`) |
| `v283` | `-Xopt` was used for discard-value-names |

When `a4 == 0xABBA` (CUDA) and no explicit `-discard-value-names`:
- Default: discard (a1+232 = 1)
- Emits: `-lnk-discard-value-names=1` to lnk, `-opt-discard-value-names=1` to opt, `-lto-discard-value-names=1` to lto
- UNLESS overridden by per-phase `-X` flags

When `a4 == 0xDEED` (OpenCL): only applies if `(a13 & 0x20)` is set.

Error on conflicting definitions: `"libnvvm : error: -discard-value-names defined more than once, or defined for both libnvvm and sub-phase"`.

## I/O and General Flags

| Flag | Effect |
|------|--------|
| `-o <file>` | Output file (fatal if missing) |
| `-v` | Verbose mode |
| `-dryrun` | Do not execute compilation |
| `-keep` | Keep intermediate files |
| `-irversion` | Print IR version and exit |
| `-nvvmir-library <f>` | NVVM IR library file (also `=` form) |
| `-m64` | 64-bit mode flag (sets `*a8 = 1`) |

Recognized input extensions: `.bc`, `.ci`, `.i`, `.cup`, `.optixir`, `.ii`. The `.cup` extension triggers `--orig_src_path_name` / `--orig_src_file_name` handling.

## Options Structure Layout

The options structure passed as `a1` to `sub_9624D0`/`sub_12CC750` is ~1,644 bytes. Key offsets:

| Offset | Size | Content | Default |
|--------|------|---------|---------|
| +8 | DWORD | SM architecture number | 75 |
| +232 | BYTE | discard-value-names master (0=keep, 1=discard) | 0 |
| +240 | DWORD | Phase routing mode (0=full, 1-4=single) | 0 |
| +248 | PTR | BST root (`std::map` red-black tree) | |
| +256 | PTR | BST sentinel/end node | |
| +288 | QWORD | BST node count | |
| +296 | STR32 | `-g` / `-debug-compile` value | |
| +328 | STR32 | `-generate-line-info` value | |
| +360 | STR32 | `-no-lineinfo-inlined-at` value | |
| +392 | STR32 | Optimization level (0/1/2/3) | `"3"` |
| +400 | QWORD | opt-level already-set sentinel | |
| +424 | STR32 | `-disable-allopts` value | |
| +456 | STR32 | `-opt-fdiv` value | `"0"` |
| +464 | QWORD | opt-fdiv already-set sentinel | |
| +488 | STR32 | `-Osize` value | |
| +520 | STR32 | `-Om` value | |
| +552 | STR32 | Architecture defines | `compute_75` |
| +560 | QWORD | arch already-set sentinel | |
| +584 | STR32 | `-ftz` value | `"0"` |
| +592 | QWORD | ftz already-set sentinel | |
| +616 | STR32 | `-prec-sqrt` value | `"1"` (CUDA) / `"0"` (CL) |
| +624 | QWORD | prec-sqrt already-set sentinel | |
| +648 | STR32 | `-prec-div` value | `"1"` |
| +656 | QWORD | prec-div already-set sentinel | |
| +680 | STR32 | `-fma` value | `"1"` |
| +688 | QWORD | fma already-set sentinel | |
| +712 | STR32 | `-enable-mad` value | |
| +744 | STR32 | `-unsafe-math` value | |
| +776 | STR32 | `-fast-math` value | |
| +808 | STR32 | `-show-src` value | |
| +840 | STR32 | `-disable-llc-opts` value | |
| +872 | STR32 | `-w` value | |
| +904 | STR32 | `-Werror` value | |
| +1032 | STR32 | `-enable-opt-byval` value | |
| +1064 | STR32 | `-disable-inlining` value | |
| +1096 | STR32 | `-restrict` value | |
| +1128 | STR32 | `-allow-restrict-in-struct` value | |
| +1160 | STR32 | `-no-signed-zeros` value | |
| +1192 | STR32 | `-maxreg` value string | |
| +1200 | QWORD | maxreg already-set sentinel | |
| +1224 | STR32 | `-enable-verbose-asm` value | |
| +1256 | STR32 | Catchall (unrecognized flag) | |
| +1352 | STR32 | `-vasp-fix` value | |
| +1384 | STR32 | `-new-nvvm-remat` value | |
| +1416 | STR32 | `-disable-new-nvvm-remat` value | |
| +1448 | STR32 | `-disable-nvvm-remat` value | |
| +1480 | STR32 | `-split-compile` value | |
| +1488 | QWORD | split-compile already-set sentinel | |
| +1512 | STR32 | `-opt-passes` pipeline string | |
| +1520 | QWORD | opt-passes already-set sentinel | |
| +1544 | STR32 | `-no-aggressive-positive-stride-analysis` | |
| +1576 | STR32 | `disable-load-select-transform` | |
| +1608 | STR32 | `-aggressive-inline` value | |
| +1640 | DWORD | Ofast-compile level (0-4) | 0 |
| +1644 | BYTE | split-compile-extended flag | 0 |

Each STR32 is a 32-byte `std::string` with SSO (small string optimization). The QWORD "already-set sentinel" fields serve as duplicate-detection guards.

## Compilation Mode Bitmask (a13)

The `a13` parameter is an in/out bitmask that controls which pipeline phases execute and what LTO mode is active:

| Bit/Mask | Meaning |
|----------|---------|
| `0x07` | Phase control (default = 7 = all phases) |
| `0x10` | Debug compile or line-info enabled |
| `0x20` | LTO generation enabled |
| `0x21` | gen-lto mode |
| `0x23` | Full LTO mode |
| `0x26` | link-lto mode |
| `0x43` | emit-optix-ir mode |
| `0x80` | gen-opt-lto lowering flag |
| `0x100` | `--nvvm-64` (64-bit mode) |
| `0x200` | `--nvvm-32` (32-bit mode) |
| `0x300` | Mask for 64/32-bit mode bits |

## Magic Cookie Values (a4)

| Value | Meaning | Effects |
|-------|---------|---------|
| `0xABBA` (43962) | CUDA compilation | `-prec-div` routing uses CUDA levels; `-fast-math` uses CUDA defines; discard-value-names defaults to on |
| `0xDEED` (57069) | OpenCL compilation | `-prec-sqrt` defaults to 0; `-fast-math`/`-prec-div` use CL routing; `-cl-mode` scanning active |

## Default Values When Flags Are Absent

When a registered flag is not found in the user's arguments, `sub_9624D0` checks whether the stored-value sentinel is zero and applies defaults:

| Flag | Sentinel | Default applied |
|------|----------|----------------|
| `-opt=` | `a1+400 == 0` | `-opt=3` (optimization level 3) |
| `-arch=compute_` | `a1+560 == 0` | `-arch=compute_75` (SM 75 Turing) |
| `-ftz=` | `a1+592 == 0` | `-ftz=0` (no flush-to-zero) |
| `-prec-sqrt=` | `a1+624 == 0` | `-prec-sqrt=1` (CUDA) or `-prec-sqrt=0` (CL) |
| `-prec-div=` | `a1+656 == 0` | `-prec-div=1` (precise division) |
| `-fma=` | `a1+688 == 0` | `-fma=1` (FMA enabled) |
| `-opt-fdiv=` | `a1+464 == 0` | `-opt-fdiv=0` |

## Differences Between sub_12CC750 and sub_9624D0

The two option processors are near-identical. Key differences:

| Aspect | `sub_12CC750` | `sub_9624D0` |
|--------|--------------|--------------|
| Binary size | 87KB decompiled | 75KB decompiled |
| `-memory-space-opt` default | `0` | `1` |
| `-passes=` flag | absent | present |
| `-disable-struct-lowering` | present | absent |
| `-prec-sqrt` CL default | `0` | `1` |
| Pipeline | LibNVVM entry path | Standalone/generic path |
| Companion builder | `sub_12C8DD0` | `sub_95EB40` |
| BST lookup | `sub_12C8530` | `sub_95D600` |

## Error Handling

All error strings follow the pattern `"libnvvm : error: <message>"`:

| Error | Trigger |
|-------|---------|
| `<flag> is an unsupported option` | Flag not matched by hardcoded checks or BST lookup |
| `<flag> defined more than once` | Duplicate `-maxreg`, or duplicate BST-registered flag |
| `-arch=compute_<N> is an unsupported option` | Architecture fails bitmask validation |
| `-Ofast-compile specified more than once` | Second `-Ofast-compile=` encountered |
| `-Ofast-compile called with unsupported level, only supports 0, min, mid, or max` | Invalid level string |
| `split compilation defined more than once` | Duplicate `-split-compile` or `-split-compile-extended` |
| `-discard-value-names defined more than once, or defined for both libnvvm and sub-phase` | Conflicting discard-value-names |
| `<value> is an unsupported value for option: <flag>` | From `sub_95C230` extended parser |

## Function Address Map

| Address | Function | Role |
|---------|----------|------|
| `0x8F9C90` | `sub_8F9C90` | Real main entry point (argc/argv from OS) |
| `0x900130` | `sub_900130` | LibNVVM Path A CLI parser |
| `0x9624D0` | `sub_9624D0` | LibNVVM option processor (standalone variant) |
| `0x9685E0` | `sub_9685E0` | Pipeline orchestrator (wraps `sub_9624D0`) |
| `0x967070` | `sub_967070` | Post-option-parse pipeline setup |
| `0x95EB40` | `sub_95EB40` | BST option map builder (standalone) |
| `0x95E8B0` | `sub_95E8B0` | Flag template registration (standalone) |
| `0x95D600` | `sub_95D600` | BST option map lookup (standalone) |
| `0x95CB50` | `sub_95CB50` | Prefix-match string comparison |
| `0x95CA80` | `sub_95CA80` | Value extraction after `=` |
| `0x95C880` | `sub_95C880` | Single-phase delegator |
| `0x95C230` | `sub_95C230` | Extended flag parser (`--nvvm-64`/`--nvvm-32`) |
| `0x95BF90` | `sub_95BF90` | BST node insertion helper |
| `0x95BC80` | `sub_95BC80` | String storage into options struct |
| `0x12CC750` | `sub_12CC750` | LibNVVM option processor (LibNVVM variant) |
| `0x12C8DD0` | `sub_12C8DD0` | BST option map builder (LibNVVM, 65 entries) |
| `0x12C8B40` | `sub_12C8B40` | Individual flag registration (LibNVVM) |
| `0x12C8530` | `sub_12C8530` | BST option map lookup (LibNVVM) |
| `0x12C7B30` | `sub_12C7B30` | Pass name registration into pipeline ordering |
| `0x12C6E90` | `sub_12C6E90` | Sub-argument splitter for mode flags |
| `0x12C6910` | `sub_12C6910` | Flag filter (`-debug-compile`, `-g`, `-generate-line-info`) |
| `0x8FD0D0` | `sub_8FD0D0` | Key-value parser (used by `sub_900130`) |
| `0x8FD6D0` | `sub_8FD6D0` | String concatenation builder |

## Raw-Data Gaps (Stub)

> ⚡ **QUIRK — 47 internal `nvptx-*` cl::opt flags absent from the catalog**
> The binary strings expose 47 distinct hidden NVPTX-backend `cl::opt` flags consumed by the NVPTX LLVM target. Roughly half are referenced by current pages ([scheduling](../llvm/scheduling.md), [alias-analysis](../infra/alias-analysis.md), [nvvm-peephole](../passes/nvvm-peephole.md), [memory-space-opt](../passes/memory-space-opt.md), [knobs](./knobs.md), [nvptx-target](../infra/nvptx-target.md)); the other half remain undocumented. Uncovered: `nvptx-aa-wrapper`, `nvptx-add-scalar-move-for-vector-load`, `nvptx-forward-params`, `nvptx-isel`, `nvptx-emit-init-fini-kernel`, `nvptx-disable-set-shared-array-alignment`, `nvptx-disable-combiner-for-*`, `nvptx-approx-log2f32`, `nvptx-libcall-callee`, `nvptx-mem2reg` (referenced but no dedicated text), `nvptx-normalize-select`, `nvptx-remat-block` (link target exists, no flag entry), `nvptx-trunc-opts`, `nvptx-rsqrt-approx-opt`, `nvptx-traverse-address-aliasing-limit`, `nvptx-proxyreg-erasure`, `nvptx-generate-pack-unpack`, `nvptx-no-f16-math`, `nvptx-nan`. Confidence: HIGH (string extraction).

> ⚡ **QUIRK — 6 undocumented top-level flags handled outside the BST catalog**
> The following six flags are parsed by hardcoded `strcmp`/prefix-match chains in `sub_8FD0D0` (Path A) and `sub_125D010` (LibNVVM path) but absent from every flag-routing table above. They take an optional `=value` argument (or stand alone):
> - `-extra-device-vectorization` — gates an additional vectorizer pass; tested via `sub_2241AC0` string compare, increments the argv cursor on match.
> - `-covinfo` — code-coverage instrumentation emission marker.
> - `-profinfo` — profiling info emission marker.
> - `-profile-instr-use` — consumes a profile-data path for PGO use.
> - `-global-mem` — 11-byte literal forwarded into a string-builder used by the lto/lnk argv composition (`sub_CB6200`).
> - `-qualified` — fully-qualified-name emission mode.
> Confidence: HIGH (decompiled call sites confirmed in `cicc_full.c`). All six are candidates for the next-wave catalog expansion.

## Cross-References

- [Optimization Levels](./optimization-levels.md) — O-level pipeline builders and fast-compile tiers
- [Configuration Knobs](./knobs.md) — 1,496 `cl::opt` knobs set by the flags documented here
- [NVVMPassOptions](./nvvm-pass-options.md) — 221-slot struct that receives CLI-routed values
- [Environment Variables](./env-vars.md) — environment-based configuration (parallel to CLI)
- [Pipeline Overview](../pipeline/overview.md) — how the four output vectors feed into pipeline stages
- [nvcc Interface](../pipeline/nvcc-interface.md) — how nvcc constructs the argv passed to cicc
- [Architecture Targets](../targets/index.md) — SM feature gating driven by `-arch=compute_<N>`
