# The `ucode.c` dlopen Facade

> *All addresses on this page apply to the consumer `libnrt.so` (`libnrt.so.2.31.24.0`) from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` — ELF64 x86-64 DYN, **not stripped** (full C++ symbols + DWARF, `.text`/`.rodata` VMA == file offset, so every `0x…` offset here is also an analysis VMA). The emitter TU is `/opt/workspace/KaenaRuntime/ucode/ucode.c` (comp-dir confirmed in `.debug_str`). The provider it binds is the sibling carrier `libnrtucode_extisa.so` (build-id `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`), owned by [extisa-provider](extisa-provider.md). Other versions will differ.*
> *Evidence grade: **Confirmed (symbol- and byte-anchored)** — the `dlopen`/`dlsym`×30/api-level-3 handshake, the `ucode_func_symbols`@`0xbf2ea0` table and its 30 fn-ptr globals `0xc96a48..0xc96b30`, the two callback vtables `0xbf6c40`/`0xbf6c80`, and the `ucode_core_create` provider-call order are each read from the DWARF-typed decompile + data-ref / data-table extraction of the cited functions. The provider-internal semantics behind each fn-ptr are **not** re-derived here (owned by [extisa-provider](extisa-provider.md)). · Part XI — GPSIMD / Q7 Microcode & ISA · [back to index](../index.md)*

## Abstract

`tdrv/ucode.c` is the **thin runtime facade** that binds `libnrt.so` to the external microcode provider. It contains *no* microcode logic, *no* relocation, *no* blob store — every function in the TU is one of three things: the bootstrap that `dlopen`s the provider and `dlsym`s its entry points (`ucode_init_module`), a one-line wrapper that maps the running silicon's TPB arch to an `nrtucode` coretype and forwards through a `dlsym`'d function pointer, or the symmetric teardown that `dlclose`s and nulls the table. The whole TU is the foreign-library *consumer* half of the contract whose *provider* half — the 52 `nrtucode_*` exports of `libnrtucode_extisa.so` — is [extisa-provider](extisa-provider.md). It is the GPSIMD/Q7 analogue of the NCFW carrier facade `encd_libncfw_init` ([upload-path](../firmware/upload-path.md)): same `dlopen` → version-gate → fn-ptr-cache shape, a different carrier and a fatter binding (30 symbols, not 3).

The bootstrap `ucode_init_module` @`0x225940` does four things in order: (1) `dlopen`s `"libnrtucode_extisa.so"` (the arch-defaulted name from `ucode_shared_library_name` @`0x225930`, an 8-byte constant-return); (2) walks the 30-entry dispatch table `ucode_func_symbols`@`0xbf2ea0` (`.data.rel.ro`, 30×16 B) `dlsym`-binding each `nrtucode_*` string into a fn-ptr global in the descending `.bss` band `0xc96b30..0xc96a48`; (3) calls the freshly-bound `ucode_lib_get_api_level()` and asserts it equals `3` (else `dlclose` + null the table); (4) probes that the running arch's ext-ISA and IRAM images exist, then `strdup`s the path/build/git version strings. The provider is bound by name and exercised entirely through those 30 indirect pointers — a coupling the static call-graph cannot see, because every provider call is `call *0x…(%rip)` through a `.bss` slot resolved only at runtime.

The single contract a reimplementer must reproduce — beyond the bind itself — is the **two-vtable callback handshake** the provider calls *back* into. The provider performs no device I/O; it reaches silicon only through caller-supplied function tables installed at `context_create` and `set_memhandle_impl`. `libnrt` supplies two: `nrtucode_platform_rw_t` @`0xbf6c80` (4 slots: device CSR read/write + log) and `nrtucode_platform_memhandle_t` @`0xbf6c40` (5 slots: device-DRAM malloc/free + memhandle copy-in/out + SOC-addr translate). Every one of those nine callbacks recovers its `libnrt` context by calling the provider thunk `ucode_context_get_userdata` and then issuing a concrete `al_reg_{read,write}32` / `dmem_buf_copy{in,out}` / `dmem_alloc_aligned`. The page is four units: the **bind bootstrap** (§1), the **wrapper layer** and the arch→coretype map (§2), the **provider↔libnrt callback contract** (§3), and the **per-op load path** request→load→upload (§4).

For reimplementation, the contract is:

- **The bind bootstrap** — `dlopen("libnrtucode_extisa.so")` → `dlsym` the 30-entry `ucode_func_symbols` table into 30 `.bss` fn-ptr globals → assert `get_api_level()==3` → probe ext-ISA+IRAM → `strdup` version strings; on any failure, `dlclose` and null all 30 globals.
- **The 30-of-52 subset** — which `nrtucode_*` the runtime binds (a strict subset of the provider's 52 exports), in table order, each pinned to its `.bss` slot.
- **The two callback vtables** — `nrtucode_platform_rw_t` (4 slots @`0xbf6c80`) and `nrtucode_platform_memhandle_t` (5 slots @`0xbf6c40`) the provider calls back into, and the concrete `libnrt` device primitive each wraps.
- **The wrapper discipline** — arch → coretype via `CSWTCH.113`@`0x86ada8` `[6,13,21]`, indirect call, `nlog_write` + `NRT_STATUS` on failure; no business logic in the facade.
- **The per-op load path** — `get_num_ext_isa_libs` → `ll_create` → `ll_get_load_sequence`/`unload`, and the `get_q7_lib`/`get_q7_extram` image fetch, each a one-line forward to a bound provider pointer.

| | |
|---|---|
| **Emitter TU** | `/opt/workspace/KaenaRuntime/ucode/ucode.c` (DWARF comp-dir) |
| **Bootstrap** | `ucode_init_module` @`0x225940` (1066 B) — `dlopen` + `dlsym`×30 + api-level-3 + version `strdup` |
| **Teardown** | `ucode_teardown_module` @`0x225790` (409 B) — `free`×3 strings + `dlclose` + null 30 globals |
| **Provider name** | `ucode_shared_library_name` @`0x225930` → `"libnrtucode_extisa.so"` (arch arg ignored) |
| **Dispatch table** | `ucode_func_symbols` @`0xbf2ea0` (`.data.rel.ro`, **30**×16 B, ends `0xbf3080`) |
| **Fn-ptr globals** | `.bss` `0xc96b30..0xc96a48` (30 slots, 8 B, **descending**); handle @`0xc96a40` |
| **Version state** | `gitsha`@`0xc96a28` · `build_version`@`0xc96a30` · `external_lib_path`@`0xc96a38` (`strdup`/NULL) |
| **API contract** | `ucode_lib_get_api_level()==3` (`Incompatible version…expected: %d`); provider [re-gates @`0xab36`](extisa-provider.md) |
| **rw callback vtable** | `nrtucode_platform_rw_t` @`0xbf6c80` (4 slots) — `read_device`/`write_device`/`log`/`log_level_enabled` |
| **memhandle vtable** | `nrtucode_platform_memhandle_t` @`0xbf6c40` (5 slots) — `device_malloc`/`free`/`read`/`write`/`get_soc_addr` |
| **Arch→coretype** | `CSWTCH.113`@`0x86ada8` = `[6,13,21]` (`arch_type-2 → SUNDA/CAYMAN/MARIANA Q7_POOL`) |

---

## 1. The Bind Bootstrap (`ucode_init_module`)

### Purpose

`ucode_init_module` @`0x225940` is the one function in the TU that crosses a shared-object boundary, run once from `nrt_init`. It locates the provider, binds its 30 runtime entry points by name into fn-ptr globals, proves the ABI matches (`api_level == 3`), confirms the running silicon actually has microcode in the provider, and records the version strings the infodump path later reports. The facade cannot defer any of this to the provider: the provider exports no `dlopen` and knows nothing of `libnrt` — the bind is strictly the consumer's, exactly as `encd_libncfw_init` is for the NCFW carrier ([upload-path §1](../firmware/upload-path.md)).

### Entry Point

```text
nrt_init
  └─ ucode_init_module(ucode_lib_path | NULL)        @0x225940
       al_hal_tpb_get_arch_type                       @0x44bca0   ── running silicon arch
       ucode_shared_library_name(arch)                @0x225930   ── "libnrtucode_extisa.so"
       dlopen(name, RTLD_*)                            @.plt 0x3d3c0
         dlerror → "Failed to open Neuron Ucode library %s. Reason: %s"
       for i in 0..29:  dlsym(handle, ucode_func_symbols[i].symbol)   @.plt 0x3d950
         *ucode_func_symbols[i].func = ptr             (loop over 0xbf2ea0 → globals 0xc96b30..0xc96a58)
         dlsym fail → "Failed to get ucode symbol `%s` from shared lib %s!"
       ucode_lib_get_api_level()  (=*0xc96b30)         ── assert == 3
         mismatch → "Incompatible version of %s! Compatibility id: %d, expected: %d"
       ucode_lib_get_memory_image(coretype, IRAM, DEFAULT, &buf,&sz)   (=*0xc96b18)   @0x225b9a
       ucode_lib_get_ext_isa(coretype, DEFAULT, &kern)                 (=*0xc96b10)   @0x225c40
         either missing → "External library \"%s\" missing at least one ucode image"
       strdup(path) ; strdup(build_version) ; strdup(gitsha)   @.plt 0x3d920 (×3)
         path strdup fail → "Failed to create string for external ucode library path"
```

### Algorithm

```c
// ucode_init_module(ucode_lib_path)              // sub @0x225940, TU ucode.c
function ucode_init_module(path):
    arch = al_hal_tpb_get_arch_type()                       // @0x44bca0  → 2/3/4
    name = path ? path : ucode_shared_library_name(arch)    // @0x225930 = "libnrtucode_extisa.so"
    nlog_write(INFO, "Using shared microcode library from %s", name)

    handle = dlopen(name, RTLD_NOW)                          // @0x3d3c0
    if handle == NULL:                                       // @0x225d04
        nlog_write("Failed to open Neuron Ucode library %s. Reason: %s", name, dlerror())
        return NRT_FAILURE
    ucode_lib_handle = handle                                // store @0xc96a40 (0x2259b1)

    // --- bind: walk the 30-entry dispatch table, dlsym each symbol into its global ---
    for (e = ucode_func_symbols; e != total_col_header_names; e++):   // 0xbf2ea0 .. 0xbf3080, stride 16
        p = dlsym(handle, e->symbol)                         // e->symbol = "nrtucode_*"  (@0x3d950)
        if dlerror():                                        // @0x225a07
            nlog_write("Failed to get ucode symbol `%s` from shared lib %s!", e->symbol, name)
            goto fail_null_all                               // partial bind is never left live
        *(e->func) = p                                       // dest = a .bss global 0xc96b30..0xc96a58

    // --- ABI gate: api_level must be exactly 3 ---
    if ucode_lib_get_api_level() != 3:                       // *(0xc96b30); literal 3 in consts[]
        nlog_write("Incompatible version of %s! Compatibility id: %d, expected: %d",
                   name, ucode_lib_get_api_level(), 3)
        goto fail_dlclose

    // --- presence probe: this silicon must have an IRAM image AND an ext-ISA lib ---
    coretype = CSWTCH_113[arch - 2]                          // 0x86ada8 = [6,13,21]   (@0x225c54)
    rc1 = ucode_lib_get_memory_image(coretype, IRAM, DEFAULT, &buf, &sz)   // *(0xc96b18) @0x225b9a
    rc2 = ucode_lib_get_ext_isa(coretype, DEFAULT, &kern)                  // *(0xc96b10) @0x225c40
    if rc1 != SUCCESS or rc2 != SUCCESS:
        nlog_write("External library \"%s\" missing at least one ucode image", name)
        goto fail_dlclose

    // --- record version provenance for the infodump path ---
    ucode_external_lib_path = strdup(name)                   // 0xc96a38  (0x225bde)
    if !ucode_external_lib_path:
        nlog_write("Failed to create string for external ucode library path")
        goto fail_dlclose
    ucode_build_version = strdup(ucode_lib_get_build_version())  // 0xc96a30  via *(0xc96b28)
    ucode_gitsha        = strdup(ucode_lib_get_git_version())    // 0xc96a28  via *(0xc96b20)
    return NRT_SUCCESS

fail_dlclose:
fail_null_all:
    dlclose(handle); ucode_lib_handle = NULL
    memset(&ucode_lib_get_api_level .. &ucode_lib_core_disable_pc_bounds_check, 0)  // null all 30
    return NRT_FAILURE / NRT_INVALID / NRT_RESOURCE
```

The data-ref trace is byte-exact: the table base `0xbf2ea0` is read at `0x22598d`, the handle store `0xc96a40` at `0x2259b1`, the dlsym destinations span the full descending block `0xc96b30 → 0xc96a58` (refs `0x225a37..0x225b60`), the api-level slot `0xc96b30` is re-read at `0x225b80`, the IRAM probe (`get_memory_image`, slot `0xc96b18`) at `0x225b9a` with DWARF arg types `{uint8_t**, nrtucode_coretype_t, nrtucode_image_t, size_t*, nrtucode_flavors_t}` (comments `0x225bb9..0x225bc8`), and the ext-ISA probe (`get_ext_isa`, slot `0xc96b10`) at `0x225c40` with `{nrtucode_coretype_t, nrtucode_flavors_t, nrtucode_extkernel_t*}` (comments `0x225c5b..0x225c62`). The constant set used by the function is `{1,2,3,4,8,16,32,72}` — `3` is the api-level literal, `72` is the `nrtucode_core_t` size (`0x48` friendly-name + `0x28` header) echoed in the create path.

> **QUIRK — the provider name is a constant, not arch-derived.** `ucode_shared_library_name` @`0x225930` is an 8-byte function that ignores its `al_hal_tpb_arch_type_t` argument and returns the single string `"libnrtucode_extisa.so"`. The arch parameter exists in the signature so the facade *could* select a per-silicon carrier, but on this build all four arches resolve to the same `.so`; the per-arch divergence happens *inside* the one provider via the coretype argument, not by loading different libraries. A reimplementer that derives the library name from arch will diverge from the binary, which always loads one carrier and routes by coretype. CONFIDENCE: **HIGH** (the function body is a single `lea`+`ret` to the one string; arch arg unused).

> **GOTCHA — bind failure nulls the *entire* table, not just the failed slot.** Any `dlsym` miss, the `api_level != 3` mismatch, and either missing-image probe all funnel to the same cleanup: `dlclose` the handle and zero all 30 fn-ptr globals (the same block `ucode_teardown_module` zeroes). The facade never leaves a *partially* bound table live — so a later wrapper that dereferences `ucode_lib_*` either sees a fully-bound table or a NULL it must guard. A reimplementation that binds incrementally and leaves bound-so-far pointers populated on failure will let a wrapper call a real pointer while a sibling pointer is NULL, producing a mixed half-initialized provider. The all-or-nothing reset is the invariant. CONFIDENCE: **HIGH** (the cleanup nulls the same `0xc96b30..0xc96a58` block as the teardown global list).

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `ucode_init_module` | `0x225940` | 1066 | `dlopen` + `dlsym`×30 + api-level-3 gate + image probe + version `strdup` | HIGH |
| `ucode_shared_library_name` | `0x225930` | 8 | constant `"libnrtucode_extisa.so"` (arch arg ignored) | HIGH |
| `ucode_teardown_module` | `0x225790` | 409 | `free`×3 version strings + `dlclose(handle)` + zero all 30 globals | HIGH |
| `ucode_get_external_lib_path` | `0x225d70` | — | → `ucode_external_lib_path` (`0xc96a38`, may be NULL) | HIGH |
| `ucode_get_build_version` | `0x225d80` | — | `*out = ucode_build_version ?: "unknown"` | HIGH |
| `ucode_get_git_version` | `0x225da0` | — | `*out = ucode_gitsha ?: "unknown"` | HIGH |
| `ucode_func_symbols` (`.data.rel.ro`) | `0xbf2ea0` | 480 | 30× `{void** func, const char* symbol}` dispatch table | HIGH |
| `ucode_lib_handle` (`.bss`) | `0xc96a40` | 8 | `dlopen` handle; NULL when unbound | HIGH |

### The 30-entry bind table

`ucode_func_symbols` is 30 `ucode_symbol_t` records (`{void** func; const char* symbol;}`, 16 B each), byte-decoded from the binary. The `dlsym` loop is a plain stride-16 walk to the terminator `total_col_header_names` @`0xbf3080`. The destination globals descend by 8 in table order, so index *i* binds `0xc96b30 - 8*i`. This is the **30-of-52** subset of the provider's exports ([extisa-provider §1](extisa-provider.md)): the runtime binds `get_`×6, `context_`×5, `core_`×13, `ll_`×6, and *zero* `opset_*` — the producer-side opcode-set family the compiler uses but the runtime never calls.

| idx | `.bss` slot | `nrtucode_*` symbol | facade wrapper that calls it |
|---|---|---|---|
| 0 | `0xc96b30` | `get_api_level` | `ucode_init_module` (the `==3` gate) |
| 1 | `0xc96b20` | `get_git_version` | `ucode_init_module` (`strdup`) |
| 2 | `0xc96b28` | `get_build_version` | `ucode_init_module` (`strdup`) |
| 3 | `0xc96b18` | `get_memory_image` | `set_seq_ucode_bins`/`set_q7_ucode_bins`/`get_q7_extram`, init probe |
| 4 | `0xc96b10` | `get_ext_isa` | `ucode_get_q7_lib`, init probe |
| 5 | `0xc96b08` | `context_create` | `ucode_core_create` |
| 6 | `0xc96b00` | `context_destroy` | `ucode_core_destroy` |
| 7 | `0xc96af8` | `core_get_context` | `ucode_core_get_context`, `ucode_core_destroy` |
| 8 | `0xc96af0` | `context_get_userdata` | `ucode_context_get_userdata` (thunk; every callback) |
| 9 | `0xc96ae8` | `context_set_userdata` | `ucode_context_set_userdata` (thunk), `ucode_core_create` |
| 10 | `0xc96ae0` | `context_set_memhandle_impl` | `ucode_core_create` (installs the memhandle vtable) |
| 11 | `0xc96ad8` | `core_on_ucode_booted` | `ucode_core_create` (claim handshake) |
| 12 | `0xc96ad0` | `core_create` | `ucode_core_create` |
| 13 | `0xc96ac8` | `core_destroy` | `ucode_core_destroy` |
| 14 | `0xc96ac0` | `core_enable_logs` | `ucode_core_create` |
| 15 | `0xc96ab8` | `core_disable_logs` | (bound; not separately wrapped) |
| 16 | `0xc96ab0` | `core_print_logs` | `ucode_core_print_logs` |
| 17 | `0xc96aa8` | `core_get_friendly_name` | (bound) |
| 18 | `0xc96aa0` | `core_set_friendly_name` | `ucode_core_create` |
| 19 | `0xc96a98` | `get_num_ext_isa_libs` | `ucode_get_num_ext_isa_libs` |
| 20 | `0xc96a90` | `ll_create` | `ucode_ll_create` |
| 21 | `0xc96a88` | `ll_set_friendly_name` | `ucode_ll_create` |
| 22 | `0xc96a80` | `ll_destroy` | `ucode_ll_destroy` |
| 23 | `0xc96a78` | `ll_get_libraries_from_opcodes` | (bound; ib/translate path) |
| 24 | `0xc96a70` | `ll_get_load_sequence` | `ucode_ll_get_load_sequence` |
| 25 | `0xc96a68` | `ll_get_unload_sequence` | `ucode_ll_get_unload_sequence` |
| 26 | `0xc96a60` | `core_dge_set_priority_class_map` | `ucode_core_dge_set_priority_class_map` |
| 27 | `0xc96a58` | `core_get_dge_mailbox_addr` | `ucode_core_get_dge_mailbox_addr` |
| 28 | `0xc96a50` | `core_enable_pc_bounds_check` | `ucode_lib_core_enable_ib_bounds_check` (adjacent cell) |
| 29 | `0xc96a48` | `core_disable_pc_bounds_check` | `ucode_lib_core_disable_ib_bounds_check` (adjacent cell) |

> **NOTE —** the table is the *runtime* subset; it deliberately omits the 22 provider exports the runtime never needs (all 9 `opset_*`, `get_hwdecode_table`, the `core_*` userdata/coretype/loglevel/size-hint getters, the `_private_*` mailbox/pc-bounds accessors, `ll_get_library_size`, and the two `ll_get_*_num_instrs` getters — enumerated in [extisa-provider §1](extisa-provider.md)). The split is `6+5+13+6+0 = 30`. CONFIDENCE: **HIGH** (the table bytes and the `ucode_teardown_module` 30-global zero-list agree exactly).

> **CORRECTION (L-INFRA-02) —** the raw scaffold (`P1-L-INFRA-02 §2/§3`) calls the table "30-entry" and the provider a "30-fn C-API." The table size (30) and the *bound subset* (30) are correct, but a reimplementer must not read "30" as the provider's export count: the provider exports **52** `nrtucode_*` ([extisa-provider §1](extisa-provider.md)); 30 is the strict subset this facade `dlsym`s. The scaffold's own §1 corroborates this ("the 52-fn C-API lives in libnrtucode_extisa.so … libnrt.so holds the `ucode_*` consumer wrappers"). CONFIDENCE: **HIGH** (52 from `nm -D` on the provider, 30 from this table and the teardown list).

---

## 2. The Wrapper Layer

### Purpose

Beyond the bootstrap, every function in `ucode.c` is a stereotyped wrapper: it maps the running TPB arch to an `nrtucode` coretype, forwards through one bound `ucode_lib_*` pointer, and on a non-`SUCCESS` provider result logs via `nlog_write` and returns an `NRT_STATUS`. The wrappers exist so the rest of `libnrt` (tdrv, the HAL init, the instruction builder) is decoupled from the separately-versioned microcode implementation — they call `ucode_*`, never `nrtucode_*`. This is the layer that makes the provider swappable: only `ucode.c` knows the provider's ABI.

### The arch → coretype map

Every wrapper that takes a silicon-dependent path reads the arch via `al_hal_tpb_get_arch_type` (→ 2 SUNDA / 3 CAYMAN / 4 MARIANA) and indexes `CSWTCH.113`@`0x86ada8` = `[6, 13, 21]` (`arch_type - 2`). Those are scheme-A "ext-ISA coretype" values `{6,13,21}` = `{SUNDA, CAYMAN, MARIANA}_Q7_POOL`, *not* the scheme-B `core->kind` `{2,9,17,25}` the provider's DGE/pc-bounds gates use — the two numbering schemes and their full reconciliation are [dispatch-tables](dispatch-tables.md) / [extisa-provider §2](extisa-provider.md). The wrappers `ucode_get_q7_lib`, `ucode_get_q7_extram`, `ucode_get_num_ext_isa_libs`, and `ucode_ll_create` each ref `0x86ada8` directly; arch values outside `{2,3,4}` map to `NRT_INVALID`.

### Algorithm — the wrapper template

```c
// representative: ucode_get_num_ext_isa_libs(size_t* out)   // sub @0x226870
function ucode_get_num_ext_isa_libs(out):
    arch = al_hal_tpb_get_arch_type()                        // @0x44bca0
    if arch < 2 or arch > 4: return NRT_INVALID              // unsupported silicon
    coretype = CSWTCH_113[arch - 2]                          // 0x86ada8 = [6,13,21]
    rc = ucode_lib_get_num_ext_isa_libs(coretype, out)       // *(0xc96a98) — bound pointer, slot 19
    if rc != SUCCESS:                                        // consts {1,2,8} = SUCCESS/UNKNOWN/INVALID
        nlog_write("Failed to get number of ext isa libs, error: %u", rc)
        return NRT_FAILURE
    return NRT_SUCCESS
```

The thinnest wrappers drop even the arch map and tail-jump straight into the bound pointer: `ucode_context_get_userdata` @`0x226740` / `ucode_context_set_userdata` @`0x226750` / `ucode_core_get_context` @`0x226760` are one-instruction thunks (`jmp *0x…(%rip)`) over slots 8/9/7. `ucode_get_q7_extram` @`0x2264f0` additionally treats provider result `2 UNKNOWN_IMAGE` as "no extram present" → returns `{NULL,0}` + `SUCCESS` (the `-3`-biased compare visible as const `18446744073709551613`), so a silicon with no EXTRAM blob is not an error.

### Function Map

| Function | Addr | Bound slot | Role | Confidence |
|---|---|---|---|---|
| `ucode_get_num_ext_isa_libs` | `0x226870` | 19 `get_num_ext_isa_libs` | arch→coretype, forward, log+`NRT_FAILURE` | HIGH |
| `ucode_get_q7_lib` | `0x2265a0` | 4 `get_ext_isa` | fill `nrtucode_extkernel_t` for this arch | HIGH |
| `ucode_get_q7_extram` | `0x2264f0` | 3 `get_memory_image` (EXTRAM) | `UNKNOWN_IMAGE` → `{NULL,0}`+SUCCESS | HIGH |
| `ucode_core_print_logs` | `0x226770` | 16 `core_print_logs` | forward; log+`NRT_FAILURE` | HIGH |
| `ucode_core_dge_set_priority_class_map` | `0x2268e0` | 26 | forward `(classes, n)` | HIGH |
| `ucode_core_get_dge_mailbox_addr` | `0x226940` | 27 | forward `(idx, &addr)` | HIGH |
| `ucode_context_get_userdata` | `0x226740` | 8 | thunk `jmp *` | HIGH |
| `ucode_context_set_userdata` | `0x226750` | 9 | thunk `jmp *` | HIGH |
| `ucode_core_get_context` | `0x226760` | 7 | thunk `jmp *` | HIGH |
| `CSWTCH.113` (`.rodata`) | `0x86ada8` | — | `[6,13,21]` arch-2 → coretype | HIGH |

### Considerations

The static call-graph radically *understates* the facade's coupling to the provider, and a reimplementer reading it must compensate. IDA's callgraph sees only the in-`libnrt` callees (`nlog_write`, `al_hal_tpb_get_arch_type`, libc) — every actual provider call is `call *0x…(%rip)` through a `.bss` slot resolved at `dlsym` time, invisible until runtime. The true out-edges of this TU are the 30 bound `nrtucode_*` functions in `libnrtucode_extisa.so`; the facade is far more tightly coupled than its visible edges suggest. (The two `ucode_lib_core_*_ib_bounds_check` wrappers @`0x226bf0`/`0x226c40` bind slots 28/29 but sit just past the `ucode.c` cell's last symbol and belong to an adjacent TU; recorded here only because they consume the table.)

---

## 3. The Provider↔libnrt Callback Contract

### Purpose

The provider never touches the device. It performs host↔device I/O only by calling *back* through function tables `libnrt` installs at bring-up — so the device-facing half of the GPSIMD stack lives entirely in `libnrt`'s `platform_*` callbacks, and a reimplementer must supply exactly these. `ucode_core_create` @`0x226610` installs them: it passes a `nrtucode_platform_rw_t*` as `context_create`'s second argument and a `nrtucode_platform_memhandle_t*` to `set_memhandle_impl`. Both are static `.data.rel.ro` vtables of `platform_*` function pointers, and both `ucode_nx_core_create` @`0x269620` and `ucode_pooling_q7_core_create` @`0x2697d0` reference the identical pair (`0xbf6c40`, `0xbf6c80`) — the callback set is the same for the NX (sequencer/scalar) and Q7-pool cores.

### The two vtables

The vtable contents are byte-decoded from `.data.rel.ro` (the `0xbf6c40`/`0xbf6c80` relocation tables): the rw table is 4 slots, the memhandle table 5 slots. This is the concrete, byte-anchored form of the accessor the [provider page §2](extisa-provider.md) could only infer at MED confidence from call-offset patterns.

```c
// nrtucode_platform_rw_t  @0xbf6c80  (.data.rel.ro)  — installed at context_create
struct nrtucode_platform_rw_t {
    fn read_device;        // +0x00 = platform_rw_read_device       0x268320
    fn write_device;       // +0x08 = platform_rw_write_device      0x268420
    fn log;                // +0x10 = platform_rw_log               0x2681a0
    fn log_level_enabled;  // +0x18 = platform_rw_log_level_enabled 0x268550
};

// nrtucode_platform_memhandle_t  @0xbf6c40  (.data.rel.ro)  — installed at set_memhandle_impl
struct nrtucode_platform_memhandle_t {
    fn device_malloc;          // +0x00 = platform_memhandle_device_malloc          0x2685d0
    fn device_free;            // +0x08 = platform_memhandle_device_free            0x269200
    fn read_memhandle;         // +0x10 = platform_memhandle_read_memhandle         0x268710
    fn write_memhandle;        // +0x18 = platform_memhandle_write_memhandle        0x269270
    fn get_memhandle_soc_addr; // +0x20 = platform_memhandle_get_memhandle_soc_addr 0x2691d0
};
```

### The contract table

Each callback recovers its `libnrt`-side context by calling the provider thunk `ucode_context_get_userdata` (which the facade earlier set via `context_set_userdata`), then issues a concrete device primitive. What the provider *needs* each callback for — read straight from each callback's own callee set — is the central contract:

| Callback (addr) | What the provider needs it for | `libnrt` primitive it wraps | Confidence |
|---|---|---|---|
| `platform_rw_read_device` (`0x268320`) | read a device CSR/mailbox word (claim magic, log head) | `al_reg_read32` + `db_physical_core_get_mla_and_tpb` | HIGH |
| `platform_rw_write_device` (`0x268420`) | write a device CSR/mailbox word (claim, log ring, DGE, pc-bounds) | `al_reg_write32` (RMW via `al_reg_read32`) | HIGH |
| `platform_rw_log` (`0x2681a0`) | emit a provider diagnostic line into the runtime log | `nlog_write` | HIGH |
| `platform_rw_log_level_enabled` (`0x268550`) | gate provider logging by the runtime's level | `nlog_get_log_level` | HIGH |
| `platform_memhandle_device_malloc` (`0x2685d0`) | allocate device DRAM for a staged library / log ring | `dmem_alloc_aligned` + `get_default_hbm_index` | HIGH |
| `platform_memhandle_device_free` (`0x269200`) | release a device-DRAM allocation at teardown | `dmem` free (asserts handle) | HIGH |
| `platform_memhandle_read_memhandle` (`0x268710`) | copy device→host through a memhandle (read back staged image) | `dmem_buf_copyout` | HIGH |
| `platform_memhandle_write_memhandle` (`0x269270`) | copy host→device (install microcode body / payload) | `dmem_buf_copyin` | HIGH |
| `platform_memhandle_get_memhandle_soc_addr` (`0x2691d0`) | translate a memhandle to a device SOC address (load-record `payload_ptr`) | leaf (46 B); asserts + returns SOC addr | HIGH |

### Algorithm — `ucode_core_create` installs both tables

```c
// ucode_core_create(core**, ctx*, coretype, userdata*, rw_impl, memhandle_impl,
//                    friendly_name, iram_base, dram_base, apb_base)   // sub @0x226610
function ucode_core_create(out_core, …, coretype, userdata, rw, mh, name, iram, dram, apb):
    rc = ucode_lib_context_create(3, rw, &ctx)              // *(0xc96b08)  API-level 3 + rw vtable
    if rc != SUCCESS:  nlog("Failed to create core context, error: %u", rc); return NRT_FAILURE
    ucode_lib_context_set_memhandle_impl(ctx, mh)           // *(0xc96ae0)  install memhandle vtable
    rc = ucode_lib_core_create(ctx, coretype, iram, mailbox, apb, &core)   // *(0xc96ad0)
    if rc != SUCCESS:  nlog("Failed to create q7 core, error: %u", rc); return NRT_FAILURE
    ucode_lib_context_set_userdata(ctx, userdata)           // *(0xc96ae8)  ← every callback reads this
    ucode_lib_core_set_friendly_name(core, name)            // *(0xc96aa0)
    rc = ucode_lib_core_on_ucode_booted(core, DEFAULT)      // *(0xc96ad8)  claim handshake
    if rc != SUCCESS:  nlog("Failed to boot q7 core on ucode, error: %u", rc); return NRT_FAILURE
    rc = ucode_lib_core_enable_logs(core)                   // *(0xc96ac0)
    if rc != SUCCESS:  nlog("Failed enable q7 core logs, error: %u", rc); return NRT_FAILURE
    *out_core = core
    return NRT_SUCCESS
```

The provider-call order is read from `ucode_core_create`'s data-refs `{0xc96b08, 0xc96ae0, 0xc96ad0, 0xc96ae8, 0xc96aa0, 0xc96ad8, 0xc96ac0}` (slots 5,10,12,9,18,11,14) and the DWARF arg types at `0x226634` (`nrtucode_platform_rw_t*`) and `0x22664a` (`nrtucode_platform_memhandle_t*`). The two callers differ only in how they fill the create args: `ucode_nx_core_create` reads sequencer params (`aws_hal_get_seq_params`, `tdrv_arch_get_nx_core_type`, friendly-name kinds `TENSOR/SCALAR/GPSIMD/VECTOR`), `ucode_pooling_q7_core_create` reads pool params (`aws_hal_get_q7_params`, `tdrv_arch_get_q7_pool_core_type`, kind `Q7_POOL`) — both then call `ucode_core_create` with the same two vtables.

> **CORRECTION (L-FACADE) —** the [provider page §2](extisa-provider.md) reconstructs the host↔device accessor as a *single* `rw_impl` vtable of **6** inferred slots (`read[0]/write[+8]/copy[+0x10]/alloc[+0x18]/map[+0x20]/free`), MED confidence, "concrete impl in `libnrt`." The `libnrt` side resolves this exactly and splits it into **two** typed vtables: `nrtucode_platform_rw_t` @`0xbf6c80` (4 slots: read/write/log/log_level) and `nrtucode_platform_memhandle_t` @`0xbf6c40` (5 slots: malloc/free/read/write/soc_addr). The provider's inferred "alloc" and "map" slots are the memhandle table's `device_malloc` and `get_memhandle_soc_addr`; its "copy" pair is `read/write_memhandle`. A reimplementer must install **two** tables — one at `context_create`, one at `set_memhandle_impl` — not one 6-entry vtable. CONFIDENCE: **HIGH** (both vtable layouts byte-decoded from `.data.rel.ro` `0xbf6c40`/`0xbf6c80`; the install sites are typed by DWARF in `ucode_core_create`).

> **NOTE —** every callback is a *closure* over the `libnrt` core: the provider holds only an opaque `userdata` (set via `context_set_userdata` @`0x226750`'s slot 9), and each `platform_*` callback's first act is to call the provider thunk `ucode_context_get_userdata` (slot 8) to recover the `libnrt` device handle before touching `al_reg_*`/`dmem_*`. This is why slots 7/8/9 are bound and wrapped as bare thunks (§2) — they are the back-channel the callbacks ride, not runtime business logic. CONFIDENCE: **HIGH** (each `platform_*` body lists `ucode_context_get_userdata` among its callees).

---

## 4. The Per-Op Load Path (request → load → upload)

### Purpose

When a NEFF carries a custom GPSIMD op, the runtime must fetch the right microcode library for the running silicon, stage it, and push it to device IRAM. The facade owns the *request* and *serialize* steps — all one-line forwards to bound provider pointers — while the relocation/prelink that turns a blob into a device image is the provider's ([microcode-loader](microcode-loader.md)) and the device write is the memhandle callbacks of §3. The path mirrors the NCFW upload's fetch-then-DMA shape ([upload-path §2](../firmware/upload-path.md)), but here the "fetch" is split across `get_num_ext_isa_libs` / `ll_create` / `ll_get_load_sequence`.

### Entry Point

```text
INIT-TIME IMAGE FETCH (HAL bring-up)
  tpb_eng_init_hals_v2 / xt_cc_init
    └─ ucode_set_seq_ucode_bins | ucode_set_q7_ucode_bins        ── fill IRAM/DRAM bins per engine
         └─ ucode_lib_get_memory_image(coretype, IRAM|DRAM, DEFAULT)   (slot 3)
  ucode_alloc_new_staged_libs → ucode_get_q7_extram               (slot 3, EXTRAM)
  kelf_load_from_neff(...)     → ucode_get_q7_lib                 (slot 4, get_ext_isa)

PER-OP LOAD (tdrv / instruction build)
  tdrv_init
    └─ ucode_get_num_ext_isa_libs(&n)               @0x226870  slot 19
    └─ ucode_ll_create(ctx, lib_index, &ll, nd, nc) @0x2269a0  slot 20 → snprintf name → slot 21
  ib_create_one_block / ib_add_extisa_lib_prefetch / translate_one_pseudo_instr_v2
    └─ ucode_ll_get_load_sequence(ll, core, …)      @0x226ad0  slot 24  ── ONE 0x40-byte record
    └─ ucode_ll_get_unload_sequence(ll, core, …)    @0x226b60  slot 25
  tdrv_destroy
    └─ ucode_core_print_logs → ucode_ll_destroy → ucode_core_destroy
```

### Algorithm — the loadable-library request

```c
// ucode_ll_create(ctx, lib_index, ll**, u8 device_id, u8 tpb_idx)   // sub @0x2269a0
function ucode_ll_create(ctx, lib_index, out_ll, nd, nc):
    arch = al_hal_tpb_get_arch_type()
    coretype = CSWTCH_113[arch - 2]                         // 0x86ada8
    rc = ucode_lib_ll_create(ctx, coretype, DEFAULT, lib_index, out_ll)   // *(0xc96a90) slot 20
    if rc != SUCCESS:
        nlog_write("ucode_lib_ll_create failed, error: %u", rc)           // const set {1,2,8,50,72}
        return NRT_FAILURE
    snprintf(name, 50, "ND[%d]NC[%d].loadable_library[%ld]", nd, nc, lib_index)   // 50-byte buf
    ucode_lib_ll_set_friendly_name(*out_ll, name)           // *(0xc96a88) slot 21
    return NRT_SUCCESS
```

```c
// ucode_ll_get_load_sequence(ll, core, max_instrs, preamble_buf, num_out)   // sub @0x226ad0
function ucode_ll_get_load_sequence(ll, core, max, buf, num_out):
    if ll == NULL:                                          // explicit NULL-guard
        nlog_write("Error: loadable library is NULL"); return NRT_FAILURE
    rc = ucode_lib_ll_get_load_sequence(ll, core, max, buf, num_out)   // *(0xc96a70) slot 24
    if rc != SUCCESS:
        nlog_write("ucode_lib_ll_get_load_sequence failed, error: %u", rc)
        return NRT_FAILURE
    return NRT_SUCCESS                                      // *num_out = 1 (the provider emits ONE record)
```

The provider's `ll_get_load_sequence` writes a single `0x40`-byte "write block" record (`tag 0x1095`) into `buf`; that record's `payload_ptr` is produced by `platform_memhandle_get_memhandle_soc_addr` (§3) and the body is later installed by `platform_memhandle_write_memhandle` (`dmem_buf_copyin`). The record format and the relocation that fills the body are owned by [extisa-provider §3](extisa-provider.md) / [microcode-loader](microcode-loader.md) — the facade only forwards the call and supplies the device-write callback. `ucode_set_seq_ucode_bins` @`0x225dc0` and `ucode_set_q7_ucode_bins` @`0x2261e0` carry per-arch hard-coded `nrt fallback` IRAM/DRAM blobs used when the provider returns no image for the running silicon; they hash both images via `ucode_print_hash` @`0x225710` (MD5) and log `"SEQ UCODE IMGs from %s …"` — but those fallback tables and the MD5 helper are out of this facade page's scope (the facade's job is the bind + forward + callback contract).

### Function Map

| Function | Addr | Bound slot | Role | Confidence |
|---|---|---|---|---|
| `ucode_ll_create` | `0x2269a0` | 20 `ll_create` (+21 name) | request loadable lib; `snprintf` friendly name | HIGH |
| `ucode_ll_destroy` | `0x226a70` | 22 `ll_destroy` | forward; log+`NRT_FAILURE` | HIGH |
| `ucode_ll_get_load_sequence` | `0x226ad0` | 24 | NULL-guard + forward; → ONE record | HIGH |
| `ucode_ll_get_unload_sequence` | `0x226b60` | 25 | NULL-guard + forward | HIGH |
| `ucode_core_create` | `0x226610` | 5,10,12,9,18,11,14 | context+core bring-up; installs both vtables (§3) | HIGH |
| `ucode_core_destroy` | `0x2267d0` | 7,13,6 | `get_context → core_destroy → free(userdata) → context_destroy` | HIGH |
| `ucode_set_seq_ucode_bins` | `0x225dc0` | 3 `get_memory_image` | seq IRAM/DRAM fetch + `nrt fallback` (out of scope) | MEDIUM |
| `ucode_set_q7_ucode_bins` | `0x2261e0` | 3 `get_memory_image` | Q7-pool IRAM/DRAM fetch + fallback (out of scope) | MEDIUM |

### Considerations

The facade is the boundary at which a transient device-DRAM exhaustion *should* surface as a clean `NRT_RESOURCE`, but the staging counterpart in the sibling TU `tdrv/ucode_lib.c` has a confirmed success-on-error defect: `ucode_alloc_new_staged_libs` @`0x310660` returns `NRT_SUCCESS` on every post-`calloc` allocation failure while leaving `*ulib_set_info` unwritten — the full disasm proof is the [microcode-loader §6 GOTCHA](microcode-loader.md). It is *not* in this `ucode.c` facade TU, but a reimplementer building the load path must propagate every allocation failure as non-zero, because the facade's wrappers correctly return `NRT_FAILURE` on a provider error and would be undermined by a staging layer that swallows OOM.

---

## Related Components

| Name | Relationship |
|---|---|
| The ext-ISA Provider ([extisa-provider](extisa-provider.md)) | The 52 `nrtucode_*` exports this facade `dlsym`s 30 of; the handles, mailbox, and accessor it drives |
| The Microcode Loader ([microcode-loader](microcode-loader.md)) | The relocation/prelink behind `ll_create`, and the `ucode_lib.c` staging success-on-error bug |
| Dispatch Tables ([dispatch-tables](dispatch-tables.md)) | The `arch_id → coretype` routing behind `CSWTCH.113`; both coretype numbering schemes in full |
| Q7 GPSIMD Overview ([overview](overview.md)) | The provider → loader → device map and the lifecycle this facade choreographs |
| Firmware Upload Path ([upload-path](../firmware/upload-path.md)) | The NCFW carrier facade `encd_libncfw_init` — the sibling `dlopen`/version-gate/fptr-cache pattern |
| FW-IO MiscRAM Mailbox ([kernel](../kernel/fw-io.md)) | The device CSR region the `platform_rw_*` callbacks' `al_reg_*` writes land in |

## Cross-References

- [The ext-ISA Provider (`nrtucode_*` API)](extisa-provider.md) — the provider half of this contract: the 52 exports, the handles, the device mailbox, the claim handshake
- [The Microcode Loader (RELA + FLIX, UCPL)](microcode-loader.md) — what `ll_create` invokes behind the facade; the `ucode_lib.c` staging defect
- [ext-ISA Dispatch Tables and arch-id Scheme](dispatch-tables.md) — `CSWTCH.113 = [6,13,21]`, the arch→coretype map every wrapper rides
- [Overview: the Q7 GPSIMD Engine](overview.md) — the provider→loader→device flow and the init→bringup→load→teardown lifecycle
- [Firmware Upload Path (DKMS → Device DRAM)](../firmware/upload-path.md) — the sibling NCFW carrier facade; same `dlopen` + version-gate + fptr-cache shape
- [FW-IO MiscRAM Mailbox](../kernel/fw-io.md) — the Q7 management/control plane the `platform_rw_*` callbacks reach
- [back to index](../index.md)
