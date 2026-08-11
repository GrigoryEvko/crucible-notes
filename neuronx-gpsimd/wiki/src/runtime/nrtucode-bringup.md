# The nrtucode Subsystem + Device Bring-Up

This page reconstructs the **host-runtime ucode bridge** inside `libnrt.so` and the
**end-to-end device bring-up** of the eight Q7 "Pool" GPSIMD cores per NeuronCore:
how the host dlopen-binds the device microcode library, builds each Q7 core over BAR0,
installs the IRAM/DRAM images (with a silent custom-op override), performs the
device→host boot/claim handshake, starts the cores, wires the `pool_stdio` host↔Q7
`printf` ring, and tears the whole thing down.

Two distinct code bodies share the `nrtucode` name and analysts routinely conflate
them. `libnrt.so` (the production Neuron RunTime, this page's host side) defines **no**
`nrtucode_*` symbol — it carries a thin `ucode_*` shim that `dlopen`s the device-side
library `libnrtucode_extisa.so` and calls into it through a 30-slot function-pointer
table. The device-side library (`libnrtucode.{a,so}`, shipped in the GPSIMD custom-op
package) is what *defines* `nrtucode_core_create` / `nrtucode_core_on_ucode_booted` /
`nrtucode_get_memory_image` and friends. This page documents **both ends of that seam**
and the actual wire-level bring-up between them.

> **Coherence pack (Part 8 — host runtime).** Host binary
> `libnrt.so.2.31.24.0` = x86-64 ELF, ~122 MB, **not stripped** (`debug_info`),
> SONAME `libnrt.so.1`, BuildID `8bb57aba…`, version `2.31.24.0` (git `0b044f4ce`).
> Sections confirmed via `readelf -SW`: `.text` @`0x3dbc0`, `.rodata` @`0x7cf000`,
> `.data.rel.ro` @`0xbf2d80`, `.data` @`0xc07e00`, `.bss` @`0xc37c40` — `.text`/`.rodata`
> are VMA==file-offset; the 30 dlsym slots live in `.bss`. The device side is the
> Cadence Vision-Q7 NX "Cairo" 512-bit FLIX DSP (`ncore2gp`); generations
> SUNDA(NC-v2)/CAYMAN(NC-v3)/MARIANA(NC-v4)/MARIANA_PLUS(NC-v4+); v2–v4 byte-grounded,
> v5/`arch_id 36` inferred. `NUM_POOL_CORES = 8`. AWS Annapurna Trainium — no NVIDIA
> content anywhere in this stack.

> **Cross-links.** The host symbol inventory is the
> [libnrt Surface Map](libnrt-surface.md); the device-programming HAL is the
> [`aws_hal_q7_*` HAL](aws-hal-q7.md); the device-side cold-boot of the Q7 image is
> [Boot / Reset Sequence](../uarch/boot-reset.md); the handle/context graph is the
> [nrtucode Object Model Graph](object-model-graph.md); the on-device console ring is
> the [On-Device Virtual File-I/O Manager](../firmware/kernels/file-io-manager.md). The
> device-side `nrtucode_core_t` introspection lives in
> [nrtucode_core_t Struct](nrtucode-core.md); the PI relocator that stages the ext-ISA
> `.so` into Q7 IRAM/DRAM is the [Host Prelinker](prelinker-ucpl.md).

---

## 0. Master spine — eleven steps from `nrt_init` to `tdrv_destroy`

Confidence tags: **HIGH/MED/LOW** × **OBSERVED** (disasm/reloc/string read from
the binary) / **INFERRED** (derived) / **CARRIED** (from a cited sibling page).
The page default is **HIGH·OBSERVED**; claims that depart from it carry an
explicit tag. Orchestrator = `tdrv_init_one_mla_phase2` (build string
`/opt/workspace/KaenaRuntime/tdrv/init.c`).

| # | Phase | Driver symbol (`libnrt.so` VA) | What physically happens | Tag |
|---|---|---|---|---|
| 0 | register | `nrt_set_pool_eng_ucode` @`0xc1630` | *(optional, before `nrt_init`)* store custom Q7 image into four globals | HIGH·OBSERVED |
| 1 | bridge | `ucode_init_module` @`0x225940` | `dlopen` `libnrtucode_extisa.so`, bind 30 `nrtucode_*` via `dlsym`, assert `api_level==3`, liveness-probe default image | HIGH·OBSERVED |
| 2 | build NX | `ucode_nx_core_create` @`0x269620` ×5 | the 5 NX engine cores (PE/ACT/SP/DVE/POOL) | HIGH·OBSERVED |
| 3 | build Q7 | `ucode_pooling_q7_core_create` @`0x2697d0` ×8 | the 8 Q7 GPSIMD pool cores, each over BAR0 | HIGH·OBSERVED |
| 4 | build LL | `ucode_ll_create` @`0x2269a0` ×N | device loadable-library handles (1/SUNDA, 4 else) | HIGH·OBSERVED |
| 5 | install | `tpb_eng_init_hals_v2` @`0x2687e0` | load DEFAULT Q7 image, **silent** custom override, commit to `tpb` | HIGH·OBSERVED |
| 6 | DMA | `aws_hal_q7_ucode_eng_init` @`0x451080` → `write_padded` @`0x473eb0` | BAR0/MMIO write of IRAM/DRAM bytes into the Q7 | HIGH·OBSERVED |
| 7 | claim | `nrtucode_core_on_ucode_booted` (device lib @`0x9b0ab0`) | poll ready `0x6099cb34`, write claim `0x502b2da1`, set `boot_state=1` | HIGH·OBSERVED |
| 8 | stdio | `pool_stdio_block_init` @`0x300c70` | build HBM stdout/stderr ring, point Q7 file-IO table at it | HIGH·OBSERVED |
| 9 | dispatch | device pool dispatcher (`kernel_info_table` @ image `0x02000380`) | execute kernels; host drains logs + `printf` ring | HIGH·CARRIED |
| 10 | teardown | `tdrv_destroy` @`0x269a70` | print logs, destroy cores/LL/stdio, `ucode_free_lib_set`, `dlclose` | HIGH·OBSERVED |

> **Boot vs. claim are two different acts.** Step 6 *lands the image bytes* into Q7
> IMEM/DRAM over BAR0; the Q7 then runs its reset vector and writes a "ready" magic into
> its `.globstruct[0]`. Step 7 is the *host* reading that magic back through the
> register-I/O callback and poking a claim word in — it does **not** copy any code. The
> physical engine reset/boot is the device-side
> [Boot / Reset Sequence](../uarch/boot-reset.md); step 7 is purely the host↔device
> ownership handshake.

> **VERIFICATION GOTCHAS used throughout.** (1) `libnrt.so` is 122 MB — a whole-binary
> `objdump -d`/un-piped `nm` hangs for minutes. Every disasm below is a **bounded**
> `objdump -d --start-address=0xX --stop-address=0xY … | head`, addresses taken from the
> IDA `*_functions.json` sidecar. (2) `.text`/`.rodata` are VMA==file-offset (so
> `python3` byte-seeks on `.rodata` strings are direct); `.data`/`.bss` are not — slot
> addresses come from RIP-relative comments in the disasm, not raw seeks. (3) The 30
> bridge entry-points are `.bss` pointers (NULL until `dlopen`); their *binding* is
> proven here, their *bodies* live in `libnrtucode_extisa.so`.

---

## 1. The bridge — `ucode_init_module` + the 30-slot dlsym table

### 1.1 The shared-library name

`ucode_shared_library_name` @`0x225930` (an 8-byte leaf) returns a `.rodata` string
at `0x84432a`. A direct byte-seek confirms it:

```
0x84432a: "libnrtucode_extisa.so\0nrt fallback\0Q7\0nrtucode_g…"
```

So the device-side body is installed as **`libnrtucode_extisa.so`** and that is the
`dlopen` target; the adjacent `"nrtucode_g…"` run is the head of the dlsym name pool.

### 1.2 `ucode_init_module` flow

`ucode_init_module` @`0x225940` (1066 B, called from `nrt_init`). Callee set confirmed
from the sidecar: `.dlopen`, `.dlsym`, `.strdup` (plus `nlog_write`). The flow, with
real symbols:

```c
// libnrt.so @0x225940 — host→device bridge bring-up.
NRT_STATUS ucode_init_module(const char *ucode_lib_path) {
    uint32_t arch = al_hal_tpb_get_arch_type();
    if (ucode_lib_path == NULL)
        ucode_lib_path = ucode_shared_library_name(arch);   // "libnrtucode_extisa.so"
    nlog("Using shared microcode library from %s", ucode_lib_path);

    // dlopen RTLD_NOW|RTLD_GLOBAL ; on failure log dlerror() and return NRT_FAILURE.
    ucode_lib_handle = dlopen(ucode_lib_path, RTLD_NOW | RTLD_GLOBAL);   // -> @0xc96a40
    if (!ucode_lib_handle) { nlog(dlerror()); return NRT_FAILURE; }

    // Bind 30 entry-points. ucode_func_symbols @0xbf2ea0 (.data.rel.ro): 30 x
    // {void **slot; const char *name}, 0x1e0 bytes. Names from the .rodata pool @0x844350+.
    for (entry in ucode_func_symbols /* 30 */) {
        void *v = dlsym(ucode_lib_handle, entry.name);
        if (!v) {
            nlog("Failed to get ucode symbol `%s`", entry.name);
            zero_all_30_slots();                            // leave no partial table
            return NRT_INVALID;
        }
        *entry.slot = v;
    }

    // ABI gate: the device lib must advertise compatibility id 3.
    if (ucode_lib_get_api_level() != 3) {                   // -> nrtucode_get_api_level
        nlog("Incompatible version … Compatibility id: %d, expected: 3", ...);
        return NRT_INVALID;
    }

    // Liveness probe: fetch ONE default image so a broken lib fails here, not mid-bringup.
    if (arch == SUNDA /*2*/)
        ucode_lib_get_ext_isa(CSWTCH_94[arch-2] /*=6*/,
                              NRTUCODE_FLAVORS_DEFAULT, &extkernel);
    else
        ucode_lib_get_memory_image(CSWTCH_96[arch-2] /*={3,10,18}=NX_PE*/,
                                   NRTUCODE_IMAGE_IRAM, NRTUCODE_FLAVORS_DEFAULT,
                                   &ptr, &size);
    if (probe_failed)
        nlog("External library \"%s\" missing at least one ucode image", ...);

    ucode_external_lib_path = strdup(ucode_lib_path);       // @0xc96a38
    ucode_build_version     = strdup(ucode_lib_get_build_version());  // @0xc96a30
    ucode_gitsha            = strdup(ucode_lib_get_git_version());    // @0xc96a28
    return NRT_SUCCESS;
}
```

`ucode_teardown_module` @`0x225790` reverses this: `dlclose` + `free` the three strdup'd
strings.

### 1.3 The 30 bound entry-points

`ucode_func_symbols` @`0xbf2ea0` (`.data.rel.ro`) pairs each `.bss` slot with the
`dlsym` name. The full pairing (libnrt `.bss` slot → `nrtucode_*` symbol resolved in
`libnrtucode_extisa.so`):

| `.bss` slot | libnrt name | dlsym name (`nrtucode_*`) |
|---|---|---|
| `c96b30` | `ucode_lib_get_api_level` | `nrtucode_get_api_level` |
| `c96b28` | `ucode_lib_get_build_version` | `nrtucode_get_build_version` |
| `c96b20` | `ucode_lib_get_git_version` | `nrtucode_get_git_version` |
| `c96b18` | `ucode_lib_get_memory_image` | `nrtucode_get_memory_image` |
| `c96b10` | `ucode_lib_get_ext_isa` | `nrtucode_get_ext_isa` |
| `c96b08` | `ucode_lib_context_create` | `nrtucode_context_create` |
| `c96b00` | `ucode_lib_context_destroy` | `nrtucode_context_destroy` |
| `c96af8` | `ucode_lib_core_get_context` | `nrtucode_core_get_context` |
| `c96af0` | `ucode_lib_context_get_userdata` | `nrtucode_context_get_userdata` |
| `c96ae8` | `ucode_lib_context_set_userdata` | `nrtucode_context_set_userdata` |
| `c96ae0` | `ucode_lib_context_set_memhandle_impl` | `nrtucode_context_set_memhandle_impl` |
| `c96ad8` | `ucode_lib_core_on_ucode_booted` | `nrtucode_core_on_ucode_booted` |
| `c96ad0` | `ucode_lib_core_create` | `nrtucode_core_create` |
| `c96ac8` | `ucode_lib_core_destroy` | `nrtucode_core_destroy` |
| `c96ac0` | `ucode_lib_core_enable_logs` | `nrtucode_core_enable_logs` |
| `c96ab8` | `ucode_lib_core_disable_logs` | `nrtucode_core_disable_logs` |
| `c96ab0` | `ucode_lib_core_print_logs` | `nrtucode_core_print_logs` |
| `c96aa8` | `ucode_lib_core_get_friendly_name` | `nrtucode_core_get_friendly_name` |
| `c96aa0` | `ucode_lib_core_set_friendly_name` | `nrtucode_core_set_friendly_name` |
| `c96a98` | `ucode_lib_get_num_ext_isa_libs` | `nrtucode_get_num_ext_isa_libs` |
| `c96a90` | `ucode_lib_ll_create` | `nrtucode_ll_create` |
| `c96a88` | `ucode_lib_ll_set_friendly_name` | `nrtucode_ll_set_friendly_name` |
| `c96a80` | `ucode_lib_ll_destroy` | `nrtucode_ll_destroy` |
| `c96a78` | `ucode_lib_ll_get_libraries_from_opcodes` | `nrtucode_ll_get_libraries_from_opcodes` |
| `c96a70` | `ucode_lib_ll_get_load_sequence` | `nrtucode_ll_get_load_sequence` |
| `c96a68` | `ucode_lib_ll_get_unload_sequence` | `nrtucode_ll_get_unload_sequence` |
| `c96a60` | `ucode_lib_core_dge_set_priority_class_map` | *(dge priority map)* |
| `c96a58` | `ucode_lib_core_get_dge_mailbox_addr` | *(dge mailbox addr)* |
| `c96a50/48` | `ucode_lib_core_{enable,disable}_pc_bounds_check` | *(PC-bounds check pair)* |

The first 25 names are read verbatim from the `.rodata` pool; the bodies are decoded on
the device-lib side ([nrtucode_core_t](nrtucode-core.md), [Object Model](object-model-graph.md),
[Host Prelinker](prelinker-ucpl.md)). This page closes the seam those pages can only
cite as "the host runtime".

---

## 2. `ucode_core_create` — the common core builder

`ucode_core_create` @`0x226610` (302 B). Both `ucode_nx_core_create` and
`ucode_pooling_q7_core_create` tail-call it (sidecar `callers` field confirms exactly
those two, at `0x269764` and `0x26993b`). It calls **only** through the §1.3 table.

```c
// libnrt.so @0x226610 — builds one nrtucode core (NX engine or Q7 pool) via the device lib.
NRT_STATUS ucode_core_create(
        nrtucode_core_t       **out,
        nrtucode_context_t     *nrtucode_context,   // = tpb->nrtucode_context
        nrtucode_coretype_t     core_type,          // the NRTUCODE_CORE_* enum (§4)
        nrtucode_userdata_t    *userdata,           // {soc_addr_base, tpb_allocator, pcore}
        const nrtucode_platform_rw_t       *rw_impl,   // = &platform_rw_impl   (§5)
        const nrtucode_platform_memhandle_t *mh_impl,  // = &platform_memhandle_impl (§5)
        const char             *friendly_name,      // "ND[%d]NC[%d].%s[%d]"
        uint64_t iram_base, uint64_t dram_base, uint64_t apb_base)
{
    nrtucode_context_t *ctx = nrtucode_context;
    if (ucode_lib_context_create(3 /*api_level*/, rw_impl, &ctx))   // device lib
        { nlog("Failed to create core context"); return 1; }
    ucode_lib_context_set_memhandle_impl(ctx, mh_impl);

    if (ucode_lib_core_create(ctx, core_type, iram_base, dram_base, apb_base, out) || !*out)
        { nlog("Failed to create q7 core"); return 1; }
    ucode_lib_context_set_userdata(ctx, userdata);
    ucode_lib_core_set_friendly_name(*out, friendly_name);

    if (ucode_lib_core_on_ucode_booted(*out, NRTUCODE_FLAVORS_DEFAULT))   // <== the CLAIM (§7)
        { nlog("Failed to boot q7 core"); return 1; }
    if (ucode_lib_core_enable_logs(*out))
        { nlog("Failed enable q7 core logs"); return 1; }
    return 0;
}
```

So a "core" = (context bound to the BAR0 `rw_impl` + the device-memory `mh_impl`) + the
device-side `nrtucode_core_t` built at the given IRAM/DRAM/APB windows + a friendly name
+ the boot/claim step + a log channel. The boot step inside `on_ucode_booted` is where
the device→host handshake of §7 actually fires.

---

## 3. `ucode_pooling_q7_core_create` — the Q7-Pool core over BAR0

`ucode_pooling_q7_core_create` @`0x2697d0` (377 B). The call chain is byte-confirmed by
bounded disasm — exactly: `db_physical_core_get_mla_and_tpb` → `tdrv_arch_get_q7_pool_core_type`
→ `snprintf` → `aws_hal_get_q7_params` → `aws_get_tpb_addr` → `aws_get_apb_base` →
`malloc` → tail `call 226610 <ucode_core_create>`.

```c
// libnrt.so @0x2697d0 — builds ONE of the 8 Q7 GPSIMD pool cores.
NRT_STATUS ucode_pooling_q7_core_create(const physical_core_t *pcore,
                                        nrtucode_core_t **out, uint8_t idx)
{
    mla_t *mla; tpb_t *tpb;
    if (db_physical_core_get_mla_and_tpb(pcore, &mla, &tpb)) return rc;

    int q7_pool_core_type = tdrv_arch_get_q7_pool_core_type();   // §4: SUNDA 6 / CAYMAN 13 / MARIANA 21
    if (q7_pool_core_type < 0) return NRT_INVALID /*2*/;          // arch with no Q7 pool -> bail

    char name[0x1E];
    snprintf(name, 0x1E, "ND[%d]NC[%d].%s[%d]",                   // fmt @0x8463a1
             pcore->device_id, pcore->device_tpb_idx, "Q7_POOL" /*@0x846399*/, idx);

    uint64_t q7_off, iram_size, dram_size, iram_rsvd, dram_rsvd, num_q7;
    aws_hal_get_q7_params(/*AL_HAL_POOLING_Q7=*/2, &q7_off, &iram_size, &dram_size,
                          &iram_rsvd, &dram_rsvd, &num_q7);        // per-arch IRAM/DRAM geometry

    uint64_t tpb_addr  = aws_get_tpb_addr(pcore);
    uint64_t iram_base = q7_off + tpb_addr;
    uint64_t apb_base  = aws_get_apb_base(pcore);
    uint64_t stride    = iram_rsvd + dram_rsvd + iram_size + dram_size;   // per-core stride
    uint64_t iram_win  = iram_size + iram_rsvd;

    nrtucode_userdata_t *ud = malloc(0x18);   // {soc_addr_base, tpb_allocator, pcore}
    ud->soc_addr_base  = tpb_addr;
    ud->tpb_allocator  = tpb->tpb_allocator;
    ud->pcore          = pcore;

    return ucode_core_create(out, tpb->nrtucode_context, q7_pool_core_type, ud,
                             &platform_rw_impl, &platform_memhandle_impl, name,
                             /*iram_base=*/ iram_base + idx*stride,
                             /*dram_base=*/ iram_base + idx*stride + iram_win,
                             apb_base);
}
```

Each of the 8 cores is placed at `iram_base + idx*stride`; its DRAM window starts one
IRAM-window past its own IRAM base. The `&platform_rw_impl` / `&platform_memhandle_impl`
vtables (§5) are what give the device lib BAR0 access **through** libnrt — this is the
"build the core over BAR0" the task names.

> **NOTE — the `AL_HAL_POOLING_Q7 = 2` owner constant.** Bounded disasm shows
> `mov $0x2,%eax` at `0x269818` and `mov $0x2,%edi` at `0x269866` feeding
> `aws_hal_get_q7_params`. The same `2` is the `owner` argument to `ucode_set_q7_ucode_bins`
> (§6). The Q7-Pool engine is HAL owner index 2 in every generation.

`ucode_nx_core_create` @`0x269620` is the sibling for the 5 NX engine cores: identical
shape but uses `aws_hal_get_seq_params` (not `get_q7_params`), `tdrv_arch_get_nx_core_type`,
and a 5-way table that selects the engine-name token in the friendly name. Those tokens
are read byte-exact at `0x84636b`: `"TENSOR\0SCALAR\0GPSIMD\0VECTOR\0…"` (the NX engine
display names; "GPSIMD" is the POOL engine's display name).

---

## 4. The `NRTUCODE_CORE_*` coretype dispatch (byte-exact)

The coretype passed to `ucode_lib_core_create` encodes (arch, engine). It is produced
per-arch through the `tdrv_arch_ops` vtable @`0xc97180` (`+0x98` =
`get_q7_pool_core_type`, `+0xb8` = `get_q7_params`).

### 4.1 Q7-Pool coretype — three constant leaves

Bounded disasm of each per-arch leaf:

```
0x30af70 tdrv_arch_get_q7_pool_core_type_sunda :  b8 06 00 00 00   mov $0x6,%eax ; c3 ret   -> 6
0x30ba70 tdrv_arch_get_q7_pool_core_type_cayman:  b8 0d 00 00 00   mov $0xd,%eax ; c3 ret   -> 13
0x30cba0 tdrv_arch_get_q7_pool_core_type_mariana: b8 15 00 00 00   mov $0x15,%eax; c3 ret   -> 21
```

The same triplet `{6, 13, 21}` is `CSWTCH_94`/`CSWTCH_113` used by `ucode_init_module`'s
`get_ext_isa` probe and by `ucode_set_q7_ucode_bins`.

### 4.2 NX engine coretype — 5-entry int32 tables (eng order PE/ACT/SP/DVE/POOL)

| Arch | PE | ACT | SP | DVE | POOL |
|---|---|---|---|---|---|
| SUNDA (@`0x9de3e0`) | 3 | 0 | 2 | 1 | 4 |
| CAYMAN (@`0x9de900`) | 10 | 7 | 9 | 8 | 11 |
| MARIANA (@`0x9defe0`) | 18 | 15 | 17 | 16 | 19 |

`CSWTCH_96 = {3, 10, 18}` = `{SUNDA, CAYMAN, MARIANA}_NX_PE` — the NX_PE coretype is the
image-table key `ucode_init_module` uses for its default-image liveness probe.

### 4.3 The full `NRTUCODE_CORE_*` enum (reconstructed)

Enum name shape `NRTUCODE_CORE_<ARCH>_<NX|Q7>_<ENGINE>`; arch enum `2=SUNDA 3=CAYMAN
4=MARIANA` (CSWTCH tables index by `arch_type - 2`):

| Arch | NX_ACT | NX_DVE | NX_PE | NX_POOL | NX_SP | NX_TOPSP | Q7_POOL | Q7_CCE |
|---|---|---|---|---|---|---|---|---|
| SUNDA | 0 | 1 | 3 | 4 | 2 | 5\? | 6 | *(none)* |
| CAYMAN | 7 | 8 | 10 | 11 | 9 | 12\? | 13 | 14\? |
| MARIANA | 15 | 16 | 18 | 19 | 17 | 20\? | 21 | 22\? |

> **NOTE — which rows are OBSERVED vs INFERRED.** The `NX_{ACT,DVE,PE,SP,POOL}` and
> `Q7_POOL` columns are **byte-OBSERVED** (the tables of §4.1–4.2). The `NX_TOPSP`
> (5/12/20) and `Q7_CCE` (14/22) indices are **MED·INFERRED** by contiguity from the
> embedded `NRTUCODE_CORE_*` string set: SUNDA ships `Q7_POOL` only (no `Q7_CCE`), while
> CAYMAN/MARIANA add `Q7_CCE` — matching the device archive's image histogram
> (`Q7_CC_TOP` present only on CAYMAN/MARIANA/MARIANA_PLUS).

> **GOTCHA — mask `0x102020204` for the DGE mailbox gate.** On the device side,
> `nrtucode_core_get_dge_mailbox_addr` requires `boot_state==1` *and* a coretype in the
> HOST core-validity mask `0x102020204` (bits **2/9/17/25/32** = the five `NX_POOL`
> coretypes SUNDA/CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK). Do not confuse the **NX_POOL**
> host coretype set with the **Q7_POOL** GPSIMD coretype set `{6,13,21,29,37}` — the
> DMA-gather mailbox is an NX_POOL feature, not a Q7-Pool one.
>
> **CORRECTION — the mask is `0x102020204` = `{2,9,17,25,32}`, not `0x2020204` = `{2,9,17,25}`.**
> An earlier draft of this GOTCHA cited mask `0x2020204` (dropping bit 32, MAVERICK) and
> further mislabelled the bit positions as "4/11/19/27". **Byte evidence** (`libnrtucode_internal.so`):
> the gate at `0x9b1013` is `cmp $0x20,%rax ; ja` then `0x9b101c: movabs $0x102020204,%rsi ; bt %rax,%rsi ; jae` —
> a **REX.W** 64-bit immediate with bit 32 set, so the set is the 5-element HOST core-validity
> set `{2,9,17,25,32}`, matching the `nrtucode-core` §3.5 legality gate and the `dge-host-api`
> priority gate `@0x9b1000`. MAVERICK/bit-32 is `[INFERRED interior]` for v5 silicon but the
> bit is byte-OBSERVED in the immediate.

---

## 5. The `platform_rw` / `platform_memhandle` vtables (how the device lib reaches BAR0)

`ucode_core_create` hands the device lib two const vtables so
`libnrtucode_extisa.so` can touch device memory **through** libnrt. Both live in
`.data.rel.ro`; targets read from `R_X86_64_RELATIVE` relocs.

`platform_rw_impl` @`0xbf6c80` — register/log I/O:

| slot | target VA | function |
|---|---|---|
| `+0x00` | `0x268320` | `platform_rw_read_device` → `al_reg_read32` (BAR0 read) |
| `+0x08` | `0x268420` | `platform_rw_write_device` (BAR0 write) |
| `+0x10` | `0x2681a0` | `platform_rw_log` |
| `+0x18` | `0x268550` | `platform_rw_log_level_enabled` |

`platform_rw_read_device` resolves `ucode_context_get_userdata → pcore →
db_physical_core_get_mla_and_tpb → al_reg_read32(...)`. So a device CSR/IRAM read issued
by the device lib is serviced by libnrt's `al_reg_read32` over the mapped BAR0 window for
that physical core. This is the `slot[0]` callback the boot handshake of §7 calls.

`platform_memhandle_impl` @`0xbf6c40` — the 5-slot device-memory vtable:

| slot | target VA | function |
|---|---|---|
| `+0x00` | `0x2685d0` | `platform_memhandle_device_malloc` |
| `+0x08` | `0x269200` | `platform_memhandle_device_free` |
| `+0x10` | `0x268710` | `platform_memhandle_read_memhandle` |
| `+0x18` | `0x269270` | `platform_memhandle_write_memhandle` |
| `+0x20` | `0x2691d0` | `platform_memhandle_get_memhandle_soc_addr` |

These are libnrt's concrete implementation of the abstract 5-slot vtable the device lib
declares (`nrtucode_platform_memhandle_dummy.c.o` ships the no-op variant).

---

## 6. `ucode_set_q7_ucode_bins` — the default Q7 images

`ucode_set_q7_ucode_bins` @`0x2261e0` (777 B). For `owner == AL_HAL_POOLING_Q7 (2)` it
calls `ucode_lib_get_memory_image` (= `nrtucode_get_memory_image`) **twice** through the
§1.3 table:

```c
// libnrt.so @0x2261e0 — fill one aws_hal_stpb_ucode_one_eng from the BUILT-IN Q7 image.
NRT_STATUS ucode_set_q7_ucode_bins(aws_hal_stpb_ucode_one_eng *out, al_hal_q7_owner owner) {
    int coretype = /* SUNDA -> 6 ; CAYMAN -> 0xd (also pins ext_isa idx 0x11) ;
                      arch4 -> tdrv_get_hw_revision branch */;
    rc = ucode_lib_get_memory_image(coretype, NRTUCODE_IMAGE_IRAM /*0*/,
                                    NRTUCODE_FLAVORS_DEFAULT, &out->iram.bin, &out->iram.size);
    rc = ucode_lib_get_memory_image(coretype, NRTUCODE_IMAGE_DRAM /*1*/,
                                    NRTUCODE_FLAVORS_DEFAULT, &out->dram.bin, &out->dram.size);
    ucode_print_hash(...);   // logs the image hash with a "Q7" tag
    return rc;
}
```

The `(coretype, region, flavor, &ptr, &size)` call shape matches the device-lib's
`nrtucode_get_memory_image` resolver exactly — this is the host runtime consuming that
resolver for the built-in Q7 ucode image. `ucode_get_q7_lib` @`0x2265a0` →
`ucode_lib_get_ext_isa` (the ucode *library* image, opcode-`0x85` ext-ISA path);
`ucode_get_q7_extram` @`0x2264f0` → the EXTRAM region.

---

## 7. The install seam — `nrt_set_pool_eng_ucode` → silent override → BAR0 → claim

### 7.1 `nrt_set_pool_eng_ucode` — the public register entry (one success window)

`nrt_set_pool_eng_ucode` @`0xc1630` (a `NRT_2.0.0` export). Sidecar constants
`{1, 2, 3, 8, 13, 14}` and callee `tdrv_set_pool_eng_ucode` confirm the state machine:

| `nrt_init_state` | action | return |
|---|---|---|
| `0` (uninitialized) | `tdrv_set_pool_eng_ucode(info)` | `0` — **the only success window** |
| `NRT_STATE_INIT` | nlog "already initialized" | `0xd` |
| `NRT_STATE_CLOSED (3)` | nlog "NRT already closed" | `0xe` |
| other | nlog "Incompatible runtime state: %s" | `1` |

So the custom-op Q7 image **must** be registered before `nrt_init`; afterward it is
rejected.

### 7.2 `tdrv_set_pool_eng_ucode` — store four globals

`tdrv_set_pool_eng_ucode` @`0x2695f0` (44 B):

```c
// libnrt.so @0x2695f0
void tdrv_set_pool_eng_ucode(const nrt_ucode_info *info) {   // nrt_ucode_info = 32 B
    pool_eng_iram_bin      = info->iram.bin;    // @0xc97030
    pool_eng_iram_bin_size = info->iram.size;   // @0xc97028
    pool_eng_dram_bin      = info->dram.bin;    // @0xc97020
    pool_eng_dram_bin_size = info->dram.size;   // @0xc97018
}
```

### 7.3 The SILENT override — `tpb_eng_init_hals_v2`

`tpb_eng_init_hals_v2` @`0x2687e0` (build string `…/KaenaRuntime/tdrv/init.c`). Bounded
disasm of `0x269053–0x2690b6` shows the four `pool_eng_*` globals loaded onto the stack
(`rsp+0xc0/0xc8/0xd0/0xd8`), then `mov $0x8a,%ecx ; rep movsq` (0x8a = 138 qwords =
0x450 bytes) committing the HAL config block into the `tpb`:

```c
// libnrt.so @0x2687e0 (decompile lines 223-243; asm @0x269052-0x2690c9).
rc = ucode_set_q7_ucode_bins(&hal_cfg[96], AL_HAL_POOLING_Q7);    // (1) load SIGNED default
if (rc == 0) {
    if (pool_eng_iram_bin) {                                       // (2) UNCONDITIONAL override
        hal_cfg[96]  = pool_eng_iram_bin;                          //     — no size/sig/arch check,
        hal_cfg[104] = pool_eng_iram_bin_size;                     //     — no warning log.
    }
    if (pool_eng_dram_bin) {
        hal_cfg[112] = pool_eng_dram_bin;
        hal_cfg[120] = pool_eng_dram_bin_size;
    }
    qmemcpy(&tpb->sunda, hal_cfg, 0x450);                          // rep movsq 0x8a qwords -> tpb+0x4288
    aws_hal_stpb_init(&tpb->sunda, &tpb->notification.ens,
                      notifications_performance_mode, program_tpb);
}
```

> **GOTCHA — this override is unguarded.** The runtime first loads the signed/shipped
> default Q7 pool image, then — if a caller registered one via `nrt_set_pool_eng_ucode`
> — overwrites the `(bin, size)` pair with the user blob with **no** size, signature, or
> arch check and **no** warning log. A custom-op author who registers a malformed image
> will see the failure only later, at the §7.4 BAR0 write or the §7.5 claim. Confirmed at
> the instruction level (the global loads + `rep movsq` above).

### 7.4 The physical device write — `aws_hal_q7_ucode_eng_init` → `write_padded`

`aws_hal_q7_ucode_eng_init` @`0x451080` per-arch dispatches (e.g.
`…_sunda` @`0x46b9a0` → `aws_hal_get_q7_params_sunda` → `write_padded` @`0x473eb0`).
Bounded disasm of `write_padded` confirms the MMIO path:

```
0x473ee3:  call 265cd0 <al_zalloc>        # padded buffer
0x473efd:  call 3ca90  <memcpy@plt>       # copy image bytes in
0x473f1a:  call *%r12                      # indirect device-write callback
0x473f26:  call 265ce0 <al_free>
0x473f6e:  call 265990 <al_mem_write_buf>  # MMIO/DMA the padded buffer into the Q7
```

The IRAM/DRAM bytes are written into the Q7 over BAR0 (`al_mem_write_buf` + the indirect
`*%r12` callback). The device-side landing in Q7 IMEM/DRAM and the subsequent reset are
the [Boot / Reset Sequence](../uarch/boot-reset.md).

### 7.5 The boot/claim handshake — `nrtucode_core_on_ucode_booted` (device lib)

Once the image is resident and the Q7 has run its reset vector, the *host* claims the
core. This happens inside `ucode_lib_core_on_ucode_booted` (§2), whose body is the
device-lib `nrtucode_core_on_ucode_booted` @`0x9b0ab0`. Bounded disasm of that body
confirms the magic exchange byte-exact:

```
0x9b0acc:  mov 0x20(%rbx),%rsi            # rw_impl
0x9b0ad8:  mov $0x4,%edx                  # len = 4
0x9b0add:  call *(%rax)                   # rw_impl->read_device  (slot[0])  -> reads device word
0x9b0aee:  cmp $0x6099cb34,%r9d           # ready magic == device .globstruct[0]
0x9b0af7:  cmp $0x502b2da1,%r9d           # already-claimed?  -> "claimed by another" ; ret 8
0x9b0b2f:  movl $0x502b2da1,0xc(%rsp)     # CLAIM word
0x9b0b4b:  call *0x8(%rax)                # rw_impl->write_device (slot[+8]) -> pokes claim back
0x9b0b52:  movl $0x1,0x30(%rbx)           # core->boot_state = 1 (BOOTED)
```

```c
// libnrtucode_extisa.so @0x9b0ab0 — device->host boot/claim handshake.
NRT_STATUS nrtucode_core_on_ucode_booted(nrtucode_core_t *core) {
    uint32_t v = 0;
    core->rw_impl->read_device(core->rw_impl, target, /*len=*/4, &v);  // slot[0]
    if (v == 0x6099cb34) {                                             // device READY
        uint32_t claim = 0x502b2da1;
        core->rw_impl->write_device(core->rw_impl, target, /*len=*/4, &claim);  // slot[+8]
        core->boot_state = 1 /*BOOTED*/;
        return 0;
    }
    if (v == 0x502b2da1) { context_log("Core is claimed by another nrtucode_core_t instance"); return 8; }
    context_log("Magic value `%x` mismatch (booted image is incompatible)");           return 8;
}
```

The ready magic `0x6099cb34` equals the carved ext-ISA image's `.globstruct[0]` (bytes
`34 cb 99 60` LE); the claim word `0x502b2da1` is poked back through `write_device`. This
is the precise device↔host ownership seam, and `ucode_core_create` runs it for every NX
and Q7 core during build.

> **CORRECTION (vs. the "boot copies the image" reading).** The function named
> `on_ucode_booted` does **not** boot or copy anything — by the time it runs, the image
> is already resident (§7.4) and the Q7 has self-reported readiness. It is a 4-byte
> read + a 4-byte write through the `platform_rw` vtable. The actual image install is
> §7.4; the actual engine reset is the device-side boot page.

---

## 8. The `pool_stdio` host↔Q7 ring — the GPSIMD `printf` channel

The GPSIMD (Pool-Q7) `printf`/`perror` output is a pair of HBM-resident ring queues the
Q7 firmware writes and the host drains. The ring is built once per NeuronCore by
`tpb_eng_sw_init` and pointed at the Q7 via the file-IO table.

> **Cross-link.** The *device* end of this ring — the Q7 firmware's queue producer and
> the file-IO descriptor table it reads — is the
> [On-Device Virtual File-I/O Manager](../firmware/kernels/file-io-manager.md). This
> section is the *host* consumer/initializer.

### 8.1 Fixed entry size

`pool_stdio_get_entry_size` @`0x300a00` is a 6-byte leaf — bounded disasm:
`b8 00 01 00 00  mov $0x100,%eax ; c3 ret` → **256 B** fixed entry.

### 8.2 `pool_stdio_block_init`

`pool_stdio_block_init` @`0x300c70` (726 B). Bounded disasm confirms the structure:
`cmpb 0x0,0x100(%rdx)` (already-init check), `get_default_hbm_index`, `push $0x800`
(align), `mov $0x440,%edx ; call dmem_alloc_aligned` (the 0x440 descriptor block),
`dmem_memset 0x440`, `dmem_buf_copyin … 0x280` (header copyin),
`db_physical_core_get_mla_and_tpb`, `aws_hal_q7_swap_file_io_table`,
`movb $0x1,0x100(%rbp)` (initialized=1), `pool_stdio_queue_init`.

```c
// libnrt.so @0x300c70 — build the host<->Q7 stdout/stderr HBM ring.
NRT_STATUS pool_stdio_block_init(const physical_core_t *pcore, dmem_allocator_t *allocator,
                                 pool_stdio_block_t *sb,
                                 uint32_t entries_stdout, uint32_t entries_stderr)
{
    if (sb->initialized) return error("already initialized");
    sb->pcore = pcore;
    int hbm_idx = get_default_hbm_index(pcore->device_tpb_idx);

    // The 0x440-B descriptor block (queue info + read indexes), 0x800-aligned, in HBM.
    dmem_alloc_aligned(allocator, &sb->hbm_space_dmem, /*size=*/0x440,
                       TONGA_DRAM, hbm_idx, 0, DMA_MEM_USAGE_TYPE_POOL_STDIO,
                       "Pool stdout and stderr queue information and read indexes", /*align=*/0x800);
    dmem_memset(block, 0, 0x440);

    SUNDA_UCODE_FILE_IO_INFO_TABLE info_table = {0};
    if (entries_stdout) pool_stdio_queue_init(allocator, sb->stdout_queues, sb,
                            info_table.stdout_queue_descriptions, entries_stdout, /*base=*/1024);
    if (entries_stderr) pool_stdio_queue_init(allocator, sb->stderr_queues, sb,
                            info_table.stderr_queue_descriptions, entries_stderr, /*base=*/1056);
    dmem_buf_copyin(block, &info_table, 0, 0x280);   // 0x280-B header -> device

    db_physical_core_get_mla_and_tpb(pcore, &mla, &tpb);
    aws_hal_q7_swap_file_io_table(tpb->tpb_mem_base, block->_pa + block->align_offset, 1);  // POINT Q7 at ring
    sb->initialized = 1;
    return 0;
}
```

Call site = `tpb_eng_sw_init` (`tdrv_init` line 1597), **skipped** when
`tdrv_arch_is_cmdk()` (simulator/cmodel). `entries_stdout =
tpb_init_config->pool_stdout_queue_size_bytes / 0x100`; `entries_stderr = 0x400/0x100 = 4`.

### 8.3 The per-queue structs (host + device descriptor)

`pool_stdio_queue_init.constprop.0` @`0x300a10`: entries **must** be a power of two
(`(n & (n-1)) == 0`, else error log); allocs `count*0x100 + 4` (capped 0x40000). Two
structs are written:

Host-side `pool_stdio_queue_t`:

| off | field |
|---|---|
| `+0x00` | `sb` back-ptr |
| `+0x08` | `initialized = 1` |
| `+0x0c` | read-byte cursor |
| `+0x10` | capacity (entry count) |
| `+0x18` | device data VA |
| `+0x20` | device head-ptr VA |
| `+0x28` | device write-cursor-struct VA |
| `+0x30` | local read index (= 0) |

On-device descriptor (the `…_queue_descriptions` slot):

| off | field |
|---|---|
| `+0x00` | `u16` MAGIC = `0x201` |
| `+0x02` | `u8` = 0 |
| `+0x0c` | `u32` capacity |
| `+0x10` | device data base PA |
| `+0x18` | head VA |
| `+0x20` | cursor VA (via `DMEM_GET_VA`) |

### 8.4 Drain path

`pool_stdio_queue_available_count` @`0x3010f0`: reads the device write-index at
`queue+0x28`, subtracts the local read index at `+0x30`, clamps to capacity (`+0x10`);
returns the count of pending 256-B entries.

`pool_stdio_queue_consume_all_entries` @`0x3011d0`: `n = available_count()`; mallocs
`(n+1)*0xf0` host records (0xf0 = decoded record incl. a 0xf-byte preamble); for each
entry `dmem_buf_copyout` 256 B from `data VA + (read_idx % cap)*0x100 + 0x10 header` into
the host record, handling ring wraparound in two contiguous segments; advances the read
index. This is the host-side drain of the GPSIMD `printf` ring (called by the log/poll
path).

### 8.5 Destroy

`pool_stdio_block_destroy` @`0x300f50`: `aws_hal_q7_swap_file_io_table(tpb_mem_base, 0, 1)`
detaches the Q7 file-IO table, then `dmem_free`s each queue's host + device buffers, frees
the 0x440 HBM block, and zeroes the struct.

> **NOTE — `pool_stdio` is host-only.** The device custom-op archive (`libnrtucode.a`)
> carries **no** `pool_stdio_*` symbol. The ring lives entirely in `libnrt.so` `tdrv` (the
> per-engine `tpb_t.pool_stdio_block`); the Q7 firmware writes its `printf`/stdio into the
> ring region whose descriptor the host installed via `aws_hal_q7_swap_file_io_table`. The
> device side owns the *producer*, not the ring metadata.

---

## 9. End-to-end bring-up (build → install → claim → dispatch → teardown)

The orchestrator is `tdrv_init_one_mla_phase2` @`0x26a310` (per NeuronCore).

```c
// (0) optional, BEFORE nrt_init — register a custom-op Q7 image.
nrt_set_pool_eng_ucode(info);   // -> tdrv_set_pool_eng_ucode: store pool_eng_{iram,dram}_bin{,_size}

// (1) BRIDGE — nrt_init -> ucode_init_module(neuron_ucode_path):
//     dlopen libnrtucode_extisa.so; dlsym 30 nrtucode_* into the §1.3 table;
//     assert api_level==3; liveness-probe the default image; cache path/build/git.

// (2) BUILD — per NeuronCore, tdrv_init_one_mla_phase2:
for (e in {PE,ACT,SP,DVE,POOL})            // 5 NX engine cores @ tpb+0x9918[]
    ucode_nx_core_create(pcore, &tpb->nx_core[e], e);
    // for POOL: ucode_core_dge_set_priority_class_map(core, gconf->dma_prio_pkt_size[1], 4);
for (idx = 0; idx < 8; idx++)              // 8 Q7 GPSIMD pool cores @ tpb+0x9940[]
    ucode_pooling_q7_core_create(pcore, &tpb->pooling_q7_core[idx], idx);
    // assert tpb->pooling_q7_nrtucode_core[0] != NULL (init.c)
ucode_get_num_ext_isa_libs(&n);            // SUNDA->1, CAYMAN/MARIANA->4
ctx = ucode_core_get_context(q7core[0]);
for (li = 0; li < n; li++)                 // device loadable-library handles @ tpb+0x9980[]
    ucode_ll_create(ctx, li, &ll[li], device_id, tpb_idx);
ndl_nc_init_set_state(... NEURON_CINIT_STATE_COMPLETED);

// (3+6) INSTALL + DMA — tpb_eng_init_hals_v2:
//     ucode_set_seq_ucode_bins x5 (NX seq images) + ucode_set_q7_ucode_bins(&cfg[96], AL_HAL_POOLING_Q7);
//     SILENT pool_eng_* override; qmemcpy cfg -> tpb->sunda; aws_hal_stpb_init ->
//     aws_hal_q7_ucode_eng_init -> write_padded: BAR0 write of IRAM/DRAM into the Q7.

// (7) CLAIM — embedded in each ucode_core_create above: on_ucode_booted reads ready
//     magic 0x6099cb34 and writes claim 0x502b2da1; boot_state=1; enable_logs.

// (4/8) STDIO — tpb_eng_sw_init: pool_stdio_block_init builds the HBM stdout(N)/stderr(4)
//     ring; aws_hal_q7_swap_file_io_table points the Q7 file-IO table at it;
//     aws_hal_q7_swap_table swaps in the ucode lib/extram; ulib_staging_lock + ht_init.

// (5/9) DISPATCH — at runtime the device pool dispatcher (kernel_info_table @ image
//     0x02000380) executes loaded kernels; the 0x1095-magic 64-B load/unload records are
//     produced by ucode_ll_get_load_sequence / _unload_sequence (slots c96a70/c96a68).
//     Host drains logs via ucode_core_print_logs and the printf ring via
//     pool_stdio_queue_consume_all_entries.

// (10) TEARDOWN — tdrv_destroy @0x269a70:
//     hw_exec_queue_add_halt_request; for the 5 NX + 8 Q7 cores: ucode_core_print_logs
//     then ucode_core_destroy; ucode_ll_destroy x count + free; notification_destroy;
//     pool_stdio_block_destroy; mutex(ulib_staging_lock) -> ucode_free_lib_set;
//     ht_destroy; dmem_free q7 scratch + dmem_allocator_destroy.
//     Module level: ucode_teardown_module dlclose libnrtucode_extisa.so.
```

> **QUIRK — the custom-op ext-ISA `.so` is staged, not installed by `write_padded`.** The
> `write_padded` path (§7.4) lands the *base* DKL ucode image (the one carrying the
> dynamic-kernel-load loader/dispatcher/relocator). A custom op's per-kernel ext-ISA
> `.so` — a 32-bit Xtensa ELF, `e_machine 94`, `e_entry 0x01005610`,
> `kernel_info_table` @`0x02000380` — is validated (`xtlib_verify_magic`) and
> relocated into a free IRAM/DRAM region of the already-booted core by the
> [Host Prelinker](prelinker-ucpl.md)'s PI relocator (`reloc_addr` two-segment remap),
> *after* the claim. The device then linear-scans the now-extended `kernel_info_table`
> (opcode@`+3`, spec@`+2`, funcVA@`+4`; 0xF0 carries a 5-way spec sub-selector) and
> `callx8`s the matched handler.

---

## 10. Struct / symbol appendix (libnrt-resident unless noted)

`nrt_ucode_info` (arg to `nrt_set_pool_eng_ucode` / `tdrv_set_pool_eng_ucode`), 32 B:

| off | field |
|---|---|
| `+0x00` | `const void* iram.bin` |
| `+0x08` | `size_t iram.size` |
| `+0x10` | `const void* dram.bin` |
| `+0x18` | `size_t dram.size` |

`pool_eng` globals: `pool_eng_dram_bin_size@c97018` / `pool_eng_dram_bin@c97020` /
`pool_eng_iram_bin_size@c97028` / `pool_eng_iram_bin@c97030`.

`nrtucode_userdata_t` (malloc 0x18 in q7/nx core_create): `{soc_addr_base@0,
tpb_allocator@8, const physical_core_t* pcore@0x10}`.

Bridge globals: `ucode_lib_handle@c96a40` (dlopen handle); `ucode_external_lib_path@c96a38`;
`ucode_build_version@c96a30`; `ucode_gitsha@c96a28`; `ucode_func_symbols@0xbf2ea0`
(`.data.rel.ro`, 30 × `{void** slot, char* name}`).

`tdrv_arch_ops@0xc97180` vtable: `+0x98` `get_q7_pool_core_type`, `+0xa0`
`supports_embedded_sem` (mariana=1, sunda/cayman=0), `+0xb8` `get_q7_params`.

`pool_stdio_block_t` (sb): `+0x100` `u8 initialized`; `+0x108` `const physical_core_t* pcore`;
`+0x110..` `stdout_queues`; `+0x2d0..` `stderr_queues`; `+0x490` `hbm_space_dmem` (the 0x440
HBM block).

Key function addresses (libnrt VAs, all sidecar-confirmed):

| Symbol | VA | Symbol | VA |
|---|---|---|---|
| `ucode_init_module` | `0x225940` | `ucode_teardown_module` | `0x225790` |
| `ucode_shared_library_name` | `0x225930` | `ucode_set_q7_ucode_bins` | `0x2261e0` |
| `ucode_get_q7_lib` | `0x2265a0` | `ucode_get_q7_extram` | `0x2264f0` |
| `ucode_core_create` | `0x226610` | `ucode_core_destroy` | `0x2267d0` |
| `ucode_core_print_logs` | `0x226770` | `ucode_nx_core_create` | `0x269620` |
| `ucode_pooling_q7_core_create` | `0x2697d0` | `ucode_ll_create` | `0x2269a0` |
| `ucode_ll_destroy` | `0x226a70` | `ucode_free_lib_set` | `0x311060` |
| `nrt_set_pool_eng_ucode` | `0xc1630` | `tdrv_set_pool_eng_ucode` | `0x2695f0` |
| `tpb_eng_init_hals_v2` | `0x2687e0` | `tdrv_init` | `0x26a310` |
| `tdrv_destroy` | `0x269a70` | `tdrv_arch_get_q7_pool_core_type` | `0x309330` |
| ↳ sunda/cayman/mariana | `0x30af70`/`0x30ba70`/`0x30cba0` | `tdrv_arch_get_nx_core_type` | `0x3092e0` |
| `pool_stdio_block_init` | `0x300c70` | `pool_stdio_block_destroy` | `0x300f50` |
| `pool_stdio_queue_init` | `0x300a10` | `_available_count` | `0x3010f0` |
| `_consume_all_entries` | `0x3011d0` | `pool_stdio_get_entry_size` | `0x300a00` |
| `aws_hal_q7_ucode_eng_init` | `0x451080` | `aws_hal_get_q7_params` | `0x44bfb0` |
| `aws_hal_q7_swap_file_io_table` | `0x451220` | `aws_hal_q7_swap_table` | `0x451120` |
| `write_padded` | `0x473eb0` | `platform_rw_read_device` | `0x268320` |

Vtables: `platform_rw_impl@0xbf6c80`, `platform_memhandle_impl@0xbf6c40` (§5). Device-lib
boot/claim: `nrtucode_core_on_ucode_booted@0x9b0ab0` (in `libnrtucode_internal.so`).

---

## 11. Confidence / open items

- **HIGH·OBSERVED:** the `dlopen` + 30-dlsym bridge and the
  name `libnrtucode_extisa.so` (`.rodata` byte-seek); `ucode_core_create` /
  `ucode_pooling_q7_core_create` call chains and the tail into `ucode_core_create`
  (bounded disasm); the Q7-Pool coretype triplet `{6, 13, 21}` (three leaf disasms); the
  silent override (global loads + `rep movsq 0x8a`); the BAR0 write (`write_padded`
  `al_zalloc`/`memcpy`/`*%r12`/`al_mem_write_buf`); the boot/claim magics
  `0x6099cb34`/`0x502b2da1` (on_ucode_booted disasm + constants); the full `pool_stdio`
  ring (0x100 entry, 0x440 block, 0x280 copyin, `swap_file_io_table`).
- **MED·INFERRED:** the `NX_TOPSP` (5/12/20) and `Q7_CCE` (14/22) coretype indices
  (contiguity from the embedded string set; the `NX_{ACT,DVE,PE,SP,POOL}` + `Q7_POOL`
  rows are byte-OBSERVED). The custom-op ext-ISA staging *ordering* (each step OBSERVED,
  the sequence assembled across objects).
- **LOW:** the exact bit layout of `SUNDA_UCODE_FILE_IO_INFO_TABLE` beyond the 0x280
  copyin span and the per-queue descriptor (magic `0x201` + 5 fields); the remaining
  `0x440 − 0x280` region is read-index scratch (inferred from the alloc comment).
- **v5 / MAVERICK (NC-v5) and `arch_id 36`:** inferred only — no byte-grounded coretype
  leaf or image table for v5 is present in this `2.31.24.0` host binary.
- **Cross-report consistency:** `NUM_POOL_CORES = 8` confirmed by the 8-iteration
  `ucode_pooling_q7_core_create` loop and the 8-slot `tpb+0x9940` array; the device-lib
  `nrtucode_*` bodies (boot/claim, image resolver, ll sequence) are decoded on the
  [nrtucode_core_t](nrtucode-core.md) / [Object Model](object-model-graph.md) /
  [Host Prelinker](prelinker-ucpl.md) pages and only the libnrt *binding* is proven here.
