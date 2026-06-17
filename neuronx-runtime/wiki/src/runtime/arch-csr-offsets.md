# Per-Arch Device Layer: CSR and Register-Offset Accessors

> *All addresses, offsets, symbol names, register CSR offsets, and per-arch base/offset return values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; all four `PT_LOAD` segments are identity-mapped, so `.text`/`.rodata` are VMA == file offset (`.data` has a `+0x400000` VMA→file delta; `.bss` is `NOBITS`). The vendored HAL package is `KaenaHal-2.31.0.0` (Amazon `brazil-pkg-cache`, `AL2_x86_64/generic-flavor`); the per-arch offset/register TUs root in `.../KaenaHal-2.31.0.0/.../src/src/{sunda,cayman,mariana}/arch/aws_hal_arch_offsets_<arch>.c` and `aws_hal_arch_regs_<arch>.c`, with the register-offset *magics* sourced from the vendored `CaymanArchHeaders-1.0.826.0` arch-headers. The `csr_*` MMIO primitives root in `/opt/workspace/KaenaRuntime/tdrv/csr.c` and the DVE dynamic-config loaders in `tdrv/<arch>/dve_dynamic_config_<arch>.c`. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every leaf getter is `nm`/DWARF-pinned by symbol+address and its return value is `objdump`/`xxd`-decoded from the body's immediates or the backing `.rodata` table; the `csr_*` region-table walk and `ndl_bar` routing are decompiler+`objdump`-confirmed; the per-arch divergences (SUNDA NO-OP stubs, the HBM-size and DGE table-offset deltas) are read directly from the leaf bodies, not inferred. · Part IV — Userspace Runtime Core · [back to index](../index.md)*

## Abstract

This page owns the **concrete per-arch CSR/register-offset *values*** of the KaenaHal device layer — the numbers the dispatch mechanism resolves *to*. Its sibling [hal-registers](hal-registers.md) documents the arch-gate-then-dispatch indirection (`assert(gate()); fp = kaena_khal.khal_arch.<member>; return fp(args)`) and explicitly defers the actual base addresses and offsets to here; [arch-stpb](arch-stpb.md) likewise routes its concrete CSR-value questions to this page. What `aws_hal_get_tpb_base(PE)` returns on SUNDA versus CAYMAN versus MARIANA, where the DVE table offsets live on each generation, which SDMA config offsets exist at all — those are the `*_sunda`/`*_cayman`/`*_mariana` leaf-getter return values decoded below. The mechanism is hal-registers'; the matrix of numbers is this page's job.

Two distinct bands meet here. The first is the **per-arch offset-getter band** — the `kaena_khal.khal_arch` leaves installed by `kaena_khal_register_funcs_v{2,3,4}` (SUNDA/CAYMAN/MARIANA). These are pure, side-effect-free constant getters: they take an index or engine ordinal, bounds-check it (assert-on-OOB), and either index a flat `.rodata` qword table (TPB/PSUM/PREPROC/HBM bases, IRAM sizes) or run a `switch` that emits literal device addresses (sequencer triplets, Q7 params, HW-decode table bases, DVE table offsets/sizes). The TPB NX/XT local-register accessors (`aws_hal_arch_<arch>_write/get_offset_*`) sit in the same band: tiny wrappers that add a fixed byte offset to a per-engine register-window base and read or write one 32-bit CSR. The second band is the **`csr_*` MMIO primitive layer** in `tdrv/csr.c` — `csr_read`/`csr_write`/`axi_write` (`@0x315680`/`@0x315820`/`@0x315850`) — the userspace BAR-access mechanism every register transaction in the runtime ultimately bottoms into. The `al_reg_{read,write}32` Alpine-HAL wrappers ([hal-adapter §2](hal-adapter.md)) the offset accessors write through resolve to exactly these three entry points.

The per-arch story is dominated by a **mesh address structure** and a set of **capability gates**. CAYMAN (Trn2) and MARIANA (Trn3) share an identical 8-tile TPB base table and a 4-channel HBM map, both carrying a `+0x800000000000` high-bit mirror on the upper half (tiles/channels 4..7 / 2..3 are the mesh second half); SUNDA (Trn1-class, arch 2) has a flat 2-channel HBM map and no mesh bit. Several engine features are *absent* on the earlier generations and their getters return `0` (SUNDA: all five SDMA/FP8/EMAX cfg getters; CAYMAN: the same five) or hard-abort (SUNDA `get_eng_hw_decode_table_params` is `__noreturn`). The numeric deltas that bite a reimplementer — HBM size (`0x400000000`/`0x600000000`/`0x900000000`), DVE param-RAM base (`0x2B80000`/`0x2B87000`/`0x2B80000`), the DVE table-offset triples, and the TopSP NX-local register positions that differ between SUNDA and CAYMAN — are tabulated in §2 and the family×arch matrix in §3.

For reimplementation, the contract is:

- **The `csr_*` MMIO primitives** — `csr_read`/`csr_write`/`axi_write` walk the per-device registered-CSR-region table (obtained via the `tdrv_arch_ops.csr_get_regions` vtable slot `+432`), bounds-check the target VA against `[start_addr, end_addr-4]`, then route the access to the kernel through `ndl_bar_read`/`ndl_bar_write` on BAR 0. The offset accessors do **not** route through these themselves — they compute an absolute VA and hand it to `al_reg_{read,write}32`, which is the layer that calls `csr_*`.
- **The per-arch base/offset matrix** — for each register family (TPB / PSUM / PREPROC / HBM / SDMA-cfg / DVE-table / sequencer / Q7 / IRAM / TopSP-NX), the concrete value each `*_sunda`/`*_cayman`/`*_mariana` getter returns, the source getter symbol+address, and which generation gates the feature to zero or to an abort.
- **The offset-computation pattern** — the accessors split into *base getters* (table-indexed or switch-emitted absolute device addresses, some mesh-rebased by subtracting `0x800000000`) and *local-register accessors* (a fixed byte offset added to a per-engine window base, then a single `al_reg_write32`/`al_reg_read32`). Reproduce the rebase arithmetic and the offset literals exactly.
- **The DVE dynamic-config table loader** — `dve_dynamic_config_init_{sunda,cayman,mariana}` SHA-256-hash the DVE micro-tables, DMA them into device HBM via a one-shot vring, and use the offset getters above (`get_dve_table_offsets`, `get_dve_parameter_ram_params`) to place them. The SUNDA variant loads 4 tables and **skips control-slow**; CAYMAN/MARIANA load all 5. FNV-1a (`fnv_64a_buf @0x3200c0`, prime `0x100000001B3`) backs the kbin hashtable beside it.

| | |
|---|---|
| **What this page owns** | the concrete per-arch CSR/offset *values* the [hal-registers](hal-registers.md) `khal_arch` trampolines and [arch-stpb](arch-stpb.md) leaves resolve to |
| **MMIO primitives** | `csr_read @0x315680` · `csr_write @0x315820` · `axi_write @0x315850` (`tdrv/csr.c`) → `ndl_bar_read @0xC3590` / `ndl_bar_write @0xC35E0` on BAR 0 |
| **Region table slot** | `tdrv_arch_ops.csr_get_regions` (vtable `+432`); `csr_deregister_device` (`+440`); `csr_register_device` (`+424`) |
| **Arch enum** | `SUNDA=2` (Trn1-class) · `CAYMAN=3` (Trn2 / `NEURON_ARCH_V3`) · `MARIANA=4` (Trn3 / V4) |
| **Registrars** | `kaena_khal_register_funcs_v2 @0x468740` (SUNDA) · `_v3 @0x46ed70` (CAYMAN) · `_v4 @0x4622e0` (MARIANA) → fill `kaena_khal @.bss 0xCAEB80` |
| **SUNDA offset band** | `.text 0x478d80..0x47924f` (`aws_hal_arch_offsets_sunda.c` + `aws_hal_arch_regs_sunda.c`) |
| **CAYMAN offset band** | `.text 0x47aa50..0x47b309` (`aws_hal_arch_offsets_cayman.c` + `aws_hal_arch_regs_cayman.c` + NX/XT local-reg) |
| **MARIANA offset band** | `.text 0x473c70..0x47776d` (`aws_hal_arch_offsets_mariana.c`) |
| **DVE config loaders** | `dve_dynamic_config_init_{sunda,cayman,mariana}` `@0x31dfc0`/`@0x31ea40`/`@0x31f580` (`tdrv/<arch>/dve_dynamic_config_<arch>.c`) → `tdrv_arch_ops +168` |
| **FNV-1a** | `fnv_64a_buf @0x3200c0` / `fnv_64a_str @0x320140`, prime `0x100000001B3` (backs kbin `ht_buf_*`) |
| **Mesh high-bit** | `+0x800000000000` on CAYMAN/MARIANA TPB tiles 4..7 / HBM channels 2..3; rebase delta `-0x800000000` (host-visible) |

> **CORRECTION (CSR-OFF-01) —** the engine-dispatch survey ([hal-tpb-shims CORRECTION](hal-tpb-shims.md), echoed by [hal-adapter §4](hal-adapter.md)) at one point folded "the `aws_reg_*` accessors and their per-arch register/offset return values" into a single owner. Split three ways: the **trampoline mechanism** (gate + slot + tail-call) is [hal-registers](hal-registers.md); the **STPB engine-init leaf bodies** that *consume* these offsets are [arch-stpb](arch-stpb.md); the **per-arch leaf getters and the values they return** are this page. The `csr_*` primitives the whole stack bottoms into are documented in summary in [hal-adapter §2](hal-adapter.md) (the `al_reg_*` wrappers) and in *mechanism* detail here (the region-table walk).

---

## 1. The `csr_*` MMIO Primitives

### Purpose

`csr_read` (`@0x315680`), `csr_write` (`@0x315820`), and `axi_write` (`@0x315850`) are the userspace MMIO entry points in `tdrv/csr.c` — the layer where an abstract "register at VA *p*" becomes a kernel BAR transaction. Every register touch on this page (and across the runtime) bottoms here: the per-arch offset accessors compute an absolute device VA, hand it to `al_reg_{read,write}32` ([hal-adapter §2](hal-adapter.md)), and those wrappers call `csr_read`/`csr_write`. Bulk device writes (`al_mem_write_buf`) take the `axi_write` path instead. The primitives are themselves **not** per-silicon — the only arch-specific input they consume is the registered region table, whose contents the per-device `csr_get_regions` impl supplies.

### Entry Point

```text
<offset accessor computes absolute VA>  (this page §2/§3)
  └─ al_reg_write32 (0x265c50)  / al_reg_read32 (0x2658a0)      [hal-adapter §2]
       └─ csr_write (0x315820)  / csr_read (0x315680)           tdrv/csr.c
            ├─ [vtable] tdrv_arch_ops.csr_get_regions (+432)    ── per-device region array
            ├─ region range-check  VA ∈ [start_addr, end_addr-4]
            └─ ndl_bar_write (0xC35E0) / ndl_bar_read (0xC3590) ── BAR 0, kernel IOCTL
al_mem_write_buf (0x265990)  ──► axi_write (0x315850)           ── absolute-addr burst, BAR 0
tdrv_destroy  ──► csr_deregister (0x315550)  ──► [vtable +440] csr_deregister_device
```

### Algorithm

`csr_read`/`csr_write` are thin 1-element wrappers over `csr_read_array`/`csr_write_array`; the array forms do the region lookup and bounds-check. The region table is an array of 48-byte `csr_region_t` records (ordinal 9092) obtained through the `tdrv_arch_ops` vtable slot `+432`:

```c
// csr_region_t (48 B) — one registered MMIO window
struct csr_region_t {
    bool             used;          // +0
    volatile void   *start_addr;    // +8   window start VA
    volatile void   *end_addr;      // +16  window end VA  (access range = [start, end-4])
    uint64_t         device_addr;   // +24  device-physical base of the window
    uint32_t         device_id;     // +32
    ndl_device_t    *ndl_device;    // +40  passed to ndl_bar_read/write
};

// csr_read_array @0x315560 — tdrv/csr.c — region-resolved bulk MMIO read (BAR 0)
function csr_read_array(ptrs[], out values[], count):
    regions = tdrv_arch_ops.csr_get_regions()           // [vtable +432]; per-device array
    if regions == NULL:
        nlog_write("Failed to get registered csr regions")   // @0x81b160
        return -2                                        // region table unavailable
    r = find_region_for(regions, ptrs[0])               // first region whose [start,end) contains ptrs[0]
    if r == NULL:
        return -2                                        // "Region not found for address %p"
    for i in 0..count-1:                                 // every ptr must land in the SAME window
        if ptrs[i] < r.start_addr or ptrs[i] > r.end_addr - 4:
            // "Invalid address %p %p-%p"  @0x847381
            return -2
    return ndl_bar_read(r.ndl_device, /*bar=*/0, r, ptrs, values, count)   // 0xC3590

// csr_read @0x315680 — 1-element wrapper
function csr_read(ptr, out value):
    return csr_read_array(&ptr, value, 1)

// csr_write @0x315820 — mirror of csr_read; csr_write_array @0x3156a0
//   same region lookup + [start, end-4] bounds-check, then ndl_bar_write(BAR0).  0x315820 → 0x3156a0
```

`axi_write` differs: it writes `count` words to an **absolute** device CSR/AXI `start_addr` (not a patched VA), does a single-region lookup for that address, and bursts through `ndl_bar_write` on BAR 0:

```c
// axi_write @0x315850 — tdrv/csr.c — absolute-address AXI burst (al_mem_write_buf path)
function axi_write(start_addr, src_words[], count):
    regions = tdrv_arch_ops.csr_get_regions()           // [vtable +432]
    if regions == NULL: return -2                        // "Failed to get registered csr regions"
    r = find_region_for(regions, start_addr)
    if r == NULL: return -2
    return ndl_bar_write(r.ndl_device, /*bar=*/0, start_addr, src_words, count)   // 0xC35E0
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `csr_read` | `0x315680` | 1-element read wrapper → `csr_read_array` | CERTAIN |
| `csr_read_array` | `0x315560` | region lookup + `[start,end-4]` bounds-check + `ndl_bar_read(BAR0)`; `-2` on miss | CERTAIN |
| `csr_write` | `0x315820` | 1-element write wrapper → `csr_write_array` | CERTAIN |
| `csr_write_array` | `0x3156a0` | region lookup + bounds-check + `ndl_bar_write(BAR0)`; logs region/address errors | CERTAIN |
| `axi_write` | `0x315850` | absolute-addr word burst → `ndl_bar_write(BAR0)` (`al_mem_write_buf` path) | CERTAIN |
| `csr_deregister` | `0x315550` | tail-jump `tdrv_arch_ops.csr_deregister_device` (vtable `+440`) | CERTAIN |

### Considerations

> **NOTE —** the `csr_*` primitives are arch-agnostic; the *per-silicon* part is the `csr_get_regions` implementation they dispatch to (vtable slot `+432`, owned by the per-arch registration cells), which populates the `csr_region_t` array with that generation's BAR windows. A reimplementer builds one `csr_*` layer and one region table per device, not one per arch.

> **GOTCHA —** the bounds-check upper limit is `end_addr - 4`, not `end_addr` — the last addressable dword starts four bytes before the window end. A port that checks against `end_addr` admits a 1-dword overread/overwrite at the very top of every CSR window. And in `csr_read_array`/`csr_write_array`, *all* `count` pointers are validated against the **single** region resolved from `ptrs[0]`; a batch that straddles two windows is rejected wholesale (`-2`), not split.

> **QUIRK —** device *writes* burst through `axi_write` (`al_mem_write_buf`), but device *reads* (`al_mem_read_buf`) are a plain inlined dword loop with no `axi_read` companion ([hal-adapter §2](hal-adapter.md)). The asymmetry is real: there is no `axi_read` entry point in `tdrv/csr.c`. A reimplementer who mirrors the write path onto reads adds a burst engine the original does not use.

---

## 2. The Per-Arch Offset-Getter Pattern

### Purpose

Every value in the §3 matrix comes from one of two getter shapes. **Base getters** return an absolute device address — either by indexing a flat `.rodata` qword table (bounds-checked) or by running an engine-keyed `switch` that emits literal addresses, some of which are rebased to a host-visible offset by subtracting `0x800000000`. **Local-register accessors** take a per-engine register-window base pointer and add a fixed byte offset, then issue one `al_reg_write32`/`al_reg_read32` (or a read-modify-write for bitfield updaters). Reproducing the matrix means reproducing these two patterns plus the exact literals.

### Algorithm

The two getter shapes, modelled on real bodies. First, a table-indexed base getter (the canonical `aws_hal_get_tpb_base_<arch>`) and a switch-emitting one (the sequencer-params getter); the rebase arithmetic (`-0x800000000`) is the host-visible conversion:

```c
// aws_hal_get_tpb_base_cayman @0x47aa90 — aws_hal_arch_offsets_cayman.c — TABLE-INDEXED base getter
function get_tpb_base_cayman(idx):                       // sunda/mariana siblings identical shape
    if idx >= 8:                                         // AL_HAL_NUM_TPB_CAYMAN = 8
        al_hal_log(1, "%s:%d:%s: Assertion failed! (%s)", "tpb_idx < AL_HAL_NUM_TPB_CAYMAN", line 13)
        al_abort_program(-1)                             // OOB → abort, no return
    return cayman_tpb_base[idx]                          // .rodata @0x855080, 8 qwords (see §3)

// aws_hal_get_seq_params_cayman @0x47ad80 — SWITCH base getter with host-visible rebase
function get_seq_params_cayman(eng, host_visible, out seq, seqtop, hostvis, sz1, sz2):
    switch eng:                                          // eng 0..3 emit literal device addrs
        case 0: base = 0x802600000                       // PE  sequencer aperture
        case 1: base = 0x802400000                       // ACT
        case 2: base = 0x803000000                       // POOL
        case 3: base = 0x802B00000                       // DVE
        case 4: base = 0; seqtop = 0x20000; hostvis = 0x40000
                if host_visible: add 0x802800000 to the triple
        default: __assert_fail(..., line 0x8F)           // OOB → abort
    seq = base; seqtop = base + 0x20000; hostvis = base + 0x40000
    sz1 = 0x20000; sz2 = 0x10000
    if host_visible: subtract 0x800000000 from seq/seqtop/hostvis   // device → host-visible rebase
```

Second, the local-register accessor pattern — a fixed byte offset added to the per-engine window base `a1`, then one CSR transaction. The offset is folded inline (from `CaymanArchHeaders` macros); the write is a tail-jump to `al_reg_write32`:

```c
// offset-computation pattern: absolute_VA = engine_window_base + register_byte_offset
// aws_hal_arch_cayman_write_tpb_nx_local_reg_start_ctrl @0x47b300 — aws_hal_arch_regs_cayman.c
function write_nx_local_reg_start_ctrl(base, val):
    return al_reg_write32(base + 0x4, val)              // NX START_CTRL @+0x004; tail-jmp → csr_write

// aws_hal_arch_cayman_update_tpb_notific_sw_queue_num2_events_semaphores_nt @0x47b0b0 — RMW updater
function update_notific_num2_evt_sem(base, v):
    r = al_reg_read32(base + 0xa08)                     // NOTIFIC_SW_QUEUE_NUM2 @+0xa08
    w = (r & 0xfff0ffff) | ((v << 16) & 0xf0000)        // EVENTS_SEMAPHORES_NT field [mask 0xf0000, shift 16]
    al_reg_write32(base + 0xa08, w)

// aws_hal_arch_cayman_get_offset_tpb_xt_local_reg_memcopy_dmas @0x47b230 — pure OFFSET getter (no I/O)
function get_offset_xt_memcopy_dmas():
    return 0x13a0                                        // caller builds (offset, value) pairs from this
```

### Considerations

> **NOTE —** base getters compute an *absolute* device VA; the `csr_*` layer (§1) then resolves which BAR window contains it. Local-register accessors are handed a window base by the caller (typically `aws_hal_arch_<arch>_get_xt_local_reg_offset`, [arch-stpb §3](arch-stpb.md)) and only add a small fixed offset. The two never compose into a single getter — the engine-window base is itself a *prior* offset-getter result.

> **GOTCHA —** the `-0x800000000` rebase and the `+0x800000000000` mesh high-bit are **different** constants serving different roles. The mesh bit (`0x8000_0000_0000`, bit 47) distinguishes the second mesh half in the *base tables* (TPB tiles 4..7, HBM channels 2..3 on CAYMAN/MARIANA). The rebase (`0x8_0000_0000`, bit 35) is subtracted by the *switch getters* (`seq_params`, `hw_decode`, `xt_local`) to convert a device-full address to a host-visible offset when the `host_visible` flag is set. Confusing the two corrupts every upper-half tile address.

> **QUIRK —** the SUNDA TopSP-NX-local register block sits at a *different* origin and different relative offsets than CAYMAN's. SUNDA's window base is `0x60000` with `stop_signal` at `0x6084c` and `basic_block_switch` at `0x6083c`; CAYMAN's `stop_signal` is `0x615c0` and `basic_block_switch` is `0x615e0`. The CAYMAN `host_trigger` getter returns `0x615A0` (one un-registered private leaf, `@0x47b080`). A reimplementer cannot reuse SUNDA's TopSP offsets on CAYMAN — they are not a constant delta apart (§3 TopSP-NX rows).

---

## 3. The Register-Family × Arch Matrix

### Purpose

This is the page's core: for each register family, the concrete value each generation's getter returns, anchored to the getter symbol+address. Where a value comes from a `.rodata` table the table address is given; where a getter returns `0` or aborts, that is the feature-gate. The matrix is decoded — every number below is read from the binary (table `xxd` byte-decode or body immediate), not raw-dumped.

### Address-Map Bases — `.rodata` qword tables

The TPB / PSUM / PREPROC base tables are **shared** between CAYMAN and MARIANA (identical `.rodata` at `0x855080`/`0x8550C0`/`0x855100`); SUNDA has no such tables in this band (its TPB/PSUM/PREPROC bases are owned by a neighbouring cell). Tiles 4..7 carry the `+0x800000000000` mesh high-bit.

| Family | Getter (symbol · addr) | SUNDA (2) | CAYMAN (3) | MARIANA (4) | Table | Conf |
|---|---|---|---|---|---|---|
| TPB base[0] (PE tile 0) | `get_tpb_base_<arch>` · cay `0x47aa90` / mar `0x477220` | — *(other cell)* | `0x2000000000` | `0x2000000000` | `@0x855080` (shared) | CERTAIN |
| TPB base[3] | " | — | `0x7000000000` | `0x7000000000` | " | CERTAIN |
| TPB base[4] (mesh-2nd-half) | " | — | `0x802000000000` | `0x802000000000` | " | CERTAIN |
| TPB base[7] | " | — | `0x807000000000` | `0x807000000000` | " | CERTAIN |
| PSUM base[0] | `get_tpb_psum_base_<arch>` · cay `0x47ab30` / mar `0x4772c0` | — | `0x2802000000` | `0x2802000000` | `@0x8550C0` (shared) | CERTAIN |
| PSUM base[7] | " | — | `0x807802000000` | `0x807802000000` | " | CERTAIN |
| PREPROC base[0] | `get_preproc_base_<arch>` · cay `0x47abd0` / mar `0x477360` | — | `0x1200000000` | `0x1200000000` | `@0x855100` (shared) | CERTAIN |
| PREPROC base[2] (mesh) | " | — | `0x801200000000` | `0x801200000000` | " | CERTAIN |

> **NOTE —** PSUM base = TPB base + `0x802000000` per tile (the PSUM aperture delta), verified across all eight slots. The shared table means a reimplementer can use one TPB/PSUM/PREPROC table for both Trn2 and Trn3; only the *count* of live engines and the per-engine sub-apertures (sequencer/Q7) differ.

### HBM Map and IRAM Sizes — `.rodata` qword tables

HBM is the cleanest per-arch axis: channel count, channel base table, and per-channel size all differ. SUNDA is 2-channel/16 GiB; CAYMAN 4-channel/24 GiB; MARIANA 4-channel/36 GiB. IRAM sizes diverge too — SUNDA's non-PE engines are 8 MiB, CAYMAN/MARIANA's are 32 KiB.

| Family | Getter (symbol · addr) | SUNDA (2) | CAYMAN (3) | MARIANA (4) | Conf |
|---|---|---|---|---|---|
| HBM base[0] | `get_hbm_base_<arch>` · sun `0x478df0` / cay `0x47ad10` / mar `0x4774b0` | `0x0` | `0x0` | `0x0` | CERTAIN |
| HBM base[1] | " | `0x1000000000` | `0x4000000000` | `0x4000000000` | CERTAIN |
| HBM base[2] (mesh) | " | *(idx>1 → abort)* | `0x800000000000` | `0x800000000000` | CERTAIN |
| HBM base[3] (mesh) | " | *(abort)* | `0x804000000000` | `0x804000000000` | CERTAIN |
| HBM table | — | `sunda_hbms @0x9f2610` (2 qw) | `cayman_hbms @0x9f4080` (4 qw) | `mariana_hbms @0x9f1a60` (4 qw) | CERTAIN |
| HBM channel count | (table size) | **2** | **4** | **4** | CERTAIN |
| HBM size (per channel) | `get_hbm_size_<arch>` · sun `0x478e50` / cay `0x47ad70` / mar `0x477510` | `0x400000000` (16 GiB) | `0x600000000` (24 GiB) | `0x900000000` (36 GiB) | CERTAIN |
| IRAM size[0] (PE) | `get_tpb_eng_iram_size_<arch>` · sun `0x478ff0` / cay `0x47af00` / mar `0x4776a0` | `0x20000` (128 KiB) | `0x20000` | `0x20000` | CERTAIN |
| IRAM size[1..4] | " | `0x800000` (8 MiB) | `0x8000` (32 KiB) | `0x8000` (32 KiB) | CERTAIN |
| IRAM table | — | `sunda_iram_sizes @0x9f2560` (5 qw) | `cayman_iram_sizes @0x9f3fc0` | `mariana_iram_sizes @0x9f19a0` | CERTAIN |

> **GOTCHA —** SUNDA's `get_hbm_size` (`0x400000000`) and CAYMAN's (`0x600000000`) are the per-channel/per-VNC *usable* HBM byte size the runtime reports, **not** the BAR window size — the kernel-side `CAYMAN_PCIE_BAR4_HBM_0_SIZE` is `0x1000000000`/channel, a different quantity. A reimplementer wiring `dmem_get_memory_stats` reads the getter value; a reimplementer mapping the BAR uses the kernel constant. They must not be cross-substituted. (Reconciliation MED — the getter value is HIGH.)

### DVE Tables, ACT Tables, Parameter RAM — switch/out-param getters

The DVE table offsets/sizes and the DVE parameter-RAM base differ between SUNDA and CAYMAN/MARIANA. SUNDA returns only *sizes* (it has no table-offset getter — offsets are implicit); CAYMAN and MARIANA expose both, and their offset triples differ from each other.

| Family | Getter (symbol · addr) | SUNDA (2) | CAYMAN (3) | MARIANA (4) | Conf |
|---|---|---|---|---|---|
| ACT table params (4 out) | `get_act_table_params_<arch>` · cay `0x47ac50` / mar `0x4773e0` | — *(other cell)* | `0x2500000`,`0x2520000`,`0x2540000`,`0x2580000` | `0x2500000`,`0x2520000`,`0x2540000`,`0x2580000` | CERTAIN |
| DVE param-RAM base + size | `get_dve_parameter_ram_params_<arch>` · cay `0x47ac70` / mar `0x477400` | — | `0x2B87000`, `0x400` | `0x2B80000`, `0x400` | CERTAIN |
| DVE table offsets (4 out) | `get_dve_table_offsets_<arch>` · cay `0x47ac80` / mar `0x477410` | *(none — sizes only)* | `0x2B82000`,`0x2B80000`,`0x2B81000`,`0x2B86000` | `0x2B7B000`,`0x2B79000`,`0x2B7A000`,`0x2B7F000` | CERTAIN |
| DVE table sizes (4 out) | `get_dve_table_sizes_<arch>` · sun `0x478d80` / cay `0x47aca0` / mar `0x477430` | `0x2000`,`0x800`,`0x800`,`0x800` | `0x4000`,`0x1000`,`0x1000`,`0x400` | `0x4000`,`0x1000`,`0x1000`,`0x400` | CERTAIN |

> **NOTE —** the DVE table-offset prefix `0x2B8xxxx` aligns with the DVE engine sequencer aperture low word (`0x802B00000` → `0x2B00000`), so the four DVE tables and the param-RAM live inside the DVE tile's local window. CAYMAN and MARIANA differ in the *exact* offsets (`0x2B82000` vs `0x2B7B000`, …) despite identical sizes — a reimplementer must key the offset triple on arch, not assume Trn2 == Trn3 here. The DWARF `dve_config_dma_state_t` member order maps these to opcode / control-fast / control-slow / datapath (name↔slot pairing MED; offsets/sizes HIGH).

### SDMA / FP8 / EMAX Config Offsets — capability-gated

These are the feature-gate family. On SUNDA **and** CAYMAN, all five return `0` (feature absent on Trn1/Trn2; the consuming caller treats `0` as "unsupported"). Only MARIANA returns real offsets. The HW-decode-table-params getter is the harshest gate: SUNDA `__noreturn`-aborts.

| Family | Getter (symbol · addr) | SUNDA (2) | CAYMAN (3) | MARIANA (4) | Conf |
|---|---|---|---|---|---|
| SDMA CCE user offset | `get_sdma_cce_user_offset_<arch>` · sun `0x478da0` / cay `0x47acc0` / mar `0x477450` | `0` | `0` | `0x2000` (8192) | CERTAIN |
| SDMA data-conv non-OCP cfg | `get_sdma_data_conv_non_ocp_cfg_offset_<arch>` · sun `0x478db0` / cay `0x47acd0` / mar `0x477460` | `0` | `0` | `0xC04`/`0xC14` (flag) | CERTAIN |
| SDMA data-conv FP8-OCP cfg | `get_sdma_data_conv_fp8_ocp_cfg_offset_<arch>` · sun `0x478dc0` / cay `0x47ace0` / mar `0x477470` | `0` | `0` | `0xC00`/`0xC10` (flag) | CERTAIN |
| DVE sequencer EMAX cfg | `get_dve_sequencer_emax_cfg_offset_<arch>` · sun `0x478dd0` / cay `0x47acf0` / mar `0x477480` | `0` | `0` | `0x32C` (812) | CERTAIN |
| ENG FP8 cfg offset (per eng) | `get_eng_fp8_cfg_offset_<arch>` · sun `0x478de0` / cay `0x47ad00` / mar `0x477490` | `0` | `0` | `{0xF00,0xF08,0xF0C,0xF04}[eng]` | CERTAIN |
| ENG HW-decode table params | `get_eng_hw_decode_table_params_<arch>` · sun `0x479030` / cay `0x47af40` / mar `0x4776e0` | **`__noreturn` abort** | real (4 out + rebase) | real (4 out + rebase) | CERTAIN |

> **GOTCHA —** the five `→ 0` getters return literal zero, which a naive caller could read as "offset 0" rather than "feature absent." The consuming code treats `0` as the unsupported sentinel — consistent with FP8/OCP and the DVE-EMAX config being Trn3/MARIANA-only features. A reimplementer must propagate the *sentinel* meaning (gate the feature off), not write to `base+0` on SUNDA/CAYMAN. The MARIANA `eng_fp8_cfg` table `{0xF00,0xF08,0xF0C,0xF04}` (`.rodata CSWTCH.14 @0x9f1840`) is notably *not* monotone — engine 2's offset (`0xF0C`) exceeds engine 3's (`0xF04`).

> **QUIRK —** SUNDA's `get_eng_hw_decode_table_params` is `__noreturn` — it hard-aborts via `__assert_fail` rather than returning `0`. This is the matching half of the [arch-stpb](arch-stpb.md) finding that all four SUNDA `*_hw_decode_table_init` leaves are `xor eax,eax; ret` stubs: SUNDA has no HW-decode silicon, so the params getter is wired to abort if ever reached (it never is, because the SUNDA orchestrators skip the call). CAYMAN/MARIANA return four out-params with the `-0x800000000` host-visible rebase (base map: eng0 `0x802670000`, eng1 `0x802470000`, eng2 `0x803070000`, eng3 `0x802B88000`; hdr 4096, blk `0x2000`).

### Sequencer / Q7 Triples — switch getters

Sequencer apertures are nearly arch-symmetric (CAYMAN and MARIANA emit the same per-engine bases); SUNDA uses a lower aperture set. Q7 (Pooling/vision core) params share addresses across arches, differing only in the instance count.

| Family | Getter (symbol · addr) | SUNDA (2) | CAYMAN (3) | MARIANA (4) | Conf |
|---|---|---|---|---|---|
| SEQ base eng0 (PE) | `get_seq_params_<arch>` · sun `0x478e60` / cay `0x47ad80` / mar `0x477520` | `0x2600000` | `0x802600000` | `0x802600000` | CERTAIN |
| SEQ base eng1 (ACT) | " | `0x2400000` | `0x802400000` | `0x802400000` | CERTAIN |
| SEQ base eng2 (POOL) | " | `0x2900000` | `0x803000000` | `0x803000000` | CERTAIN |
| SEQ base eng3 (DVE) | " | `0x2B00000` | `0x802B00000` | `0x802B00000` | CERTAIN |
| SEQ sizes (sz1, sz2) | " | `0x10000`, `0x4000` | `0x20000`, `0x10000` | `0x20000`, `0x10000` | CERTAIN |
| Q7 base (eng 2 / 5) | `get_q7_params_<arch>` · sun `0x478f90` / cay `0x47ae70` / mar `0x477610` | `0x2980000` | `0x3100000` | `0x3100000` | CERTAIN |
| Q7 instance count | " | `8` (eng2 only) | `8` (eng2) / `4` (eng5) | `8` (eng2) / `4` (eng5) | CERTAIN |
| PE-SEQ TopSP host-vis relbase | `get_pe_seq_top_host_visible_relbase_<arch>` · sun `0x479060` / cay `0x47afd0` | `0x5B0000` | `0x140000` | *(via mariana band)* | HIGH |

> **NOTE —** the SUNDA seq bases (`0x2600000`…) are the CAYMAN/MARIANA bases (`0x802600000`…) with the mesh high word stripped — SUNDA is single-mesh, so it never carries the `0x800000000` device-full prefix. The chaining is identical: `seqtop = base + 0x20000`, `hostvis = base + 0x40000`. A reimplementer can derive SUNDA seq addresses by masking the CAYMAN ones, but the *sizes* differ (`0x10000`/`0x4000` vs `0x20000`/`0x10000`) and must be keyed on arch.

### TPB NX/XT Local-Register Offsets — fixed byte offsets

These are the offsets added to a per-engine register-window base by the `aws_hal_arch_<arch>_write/get_offset_*` accessors (§2 pattern). CAYMAN's full NX/XT map is byte-decoded; SUNDA's TopSP-NX block sits at a different origin. Offsets are relative to the engine window base `a1` unless an absolute TopSP value is noted.

| Register | CAYMAN offset · getter/writer addr | SUNDA equivalent | Field / op | Conf |
|---|---|---|---|---|
| NX START_CTRL | `+0x004` · write `0x47b300` | — | program start control | CERTAIN |
| EVENTS_SEMAPHORES NOTIFIC_CTRL | `+0x808` · write `0x47b0f0` | `+0x808` · `0x4791c0` | notifications-enable ctrl (u8) | CERTAIN |
| NOTIFIC_SW_QUEUE_NUM2 | `+0xa08` · update `0x47b0b0` | `+0xa08` · `0x479160` | EVENTS_SEMAPHORES_NT `[0xf0000>>16]` | CERTAIN |
| NOTIFIC_SW_QUEUE_NUM3 | `+0xa0c` · update `0x47b100` | `+0xa0c` · `0x479200` | ERRORS_NT `[0xf>>0]` | CERTAIN |
| NOTIFIC_SW_QUEUE_NUM4 | `+0xa10` · update `0x47b130` | *(next cell)* | HAM_THROTTLE `[0xf0>>4]`, _EN `[0x100000>>20]` | CERTAIN |
| NX START_ADDR_LO / _HI | `+0x1060` / `+0x1080` · `0x47b2e0`/`0x47b2f0` | — | program start addr lo/hi | CERTAIN |
| NX DMA_CTRL | `+0x10a0` · write `0x47b180` | — | NX DMA control | CERTAIN |
| NX DMA_REGS (lo A/B, hi) | `+0x10c0`/`+0x10e0`/`+0x1100` · `0x47b190` | — | 64-bit DMA addr split (flag selects lo) | HIGH |
| XT MEMCOPY_DMAS / _QUEUES / _ALL | `+0x13a0` / `+0x13c0` / `+0x13e0` · `0x47b230`/`0x47b250`/`0x47b270` | — | memcopy cfg (offset getters) | CERTAIN |
| NX SW_DGE_CARVEOUT / _DMA_MAPPING | `+0x14e0` / `+0x1500` · `0x47b210`/`0x47b1f0` | — | sw descriptor-gen carveout/mapping | CERTAIN |
| XT Q7_RELEASE_RUN_STALL | `+0x3000` · write `0x47b290` | — | Q7 run-stall release | CERTAIN |
| XT HW_DECODE_CONTROL | `+0x4000` · set `0x47b2a0` | — | disable_hw_decode (bit0) | CERTAIN |
| TopSP NX STOP_SIGNAL | `0x615c0` (abs) · `0x47b090` | `0x6084c` (abs) · `0x479140` | TopSP stop-signal reg offset | CERTAIN |
| TopSP NX BASIC_BLOCK_SWITCH | `0x615e0` (abs) · `0x47b0a0` | `0x6083c` (abs) · `0x479150` | TopSP basic-block-switch reg | CERTAIN |
| TopSP NX HOST_TRIGGER | `0x615A0` (abs) · `0x47b080` | `0x60848` (abs) · `0x479120` | TopSP host-trigger reg | CERTAIN |
| TopSP NX init_signal (write) | — | `+0x83c` (rel) · `0x4790f0` | TopSP set-init-signal | CERTAIN |

> **NOTE —** the CAYMAN NX/XT register-offset *magics* (`AWS_REG_CAYMAN_TPB_*_OFFSET`) originate from the vendored `CaymanArchHeaders-1.0.826.0` arch-headers (`tpb_nx.h`), cross-validated byte-for-byte against the `objdump` immediates — e.g. `AWS_REG_CAYMAN_TPB_NOTIFIC_SW_QUEUE_NUM2_OFFSET 0xa08` and its `EVENTS_SEMAPHORES_NT__MASK 0xf0000 / __SHIFT 16`. The accessor bodies are first-party AWS (`aws_hal_arch_regs_cayman.c`); only the offset constants are vendored. The `release_run_stall` NX thunk writes at `base+0x0` (the run-stall reg sits at the engine-window origin for NX; LOW — the symbolic macro was not located, treated as offset 0 per byte-decode).

### Considerations

> **QUIRK —** the MARIANA `get_tpb_eng_iram_size` assert string reads `"eng_type < AL_HAL_TPB_MAX_ENG_CAYMAN"` *inside the MARIANA TU* — a source copy-paste from the CAYMAN file. The bound (4) is correct, so it is harmless, but a reimplementer auditing the assert strings should not infer a MARIANA/CAYMAN engine-count difference from it: both are 5 engines (`AL_HAL_TPB_MAX_ENG_* = 5`).

---

## 4. The DVE Dynamic-Config Table Loader

### Purpose

The DVE (Data Vector Engine) dynamic-config loaders are the largest *consumer* of the offset getters in §3 — they read `get_dve_table_offsets` / `get_dve_table_sizes` / `get_dve_parameter_ram_params` to place the DVE micro-tables into device HBM. `dve_dynamic_config_init_{sunda,cayman,mariana}` (`@0x31dfc0`/`@0x31ea40`/`@0x31f580`, `tdrv/<arch>/dve_dynamic_config_<arch>.c`) are registered into the `tdrv_arch_ops` vtable slot `+168` by `tdrv_arch_register_<arch>` and invoked at NEFF load to stage the opcode / control-fast / control-slow / datapath tables plus a 256-entry reciprocal parameter RAM. They are the page's tie between the *offset values* (§3) and a real DMA program.

### Entry Point

```text
[vtable] tdrv_arch_ops.dve_dynamic_config_init (+168)        ── per-arch slot
  └─ dve_dynamic_config_init_{sunda|cayman|mariana}          ── THIS §
       ├─ get_dve_default_bins                               ── (if NEFF lacks tables / arch mismatch)
       ├─ sha256_init/update/final  → dve_tbl_hash           ── over 4 (sunda) or 5 (cay/mar) tables
       ├─ aws_hal_stpb_get_axi_offset                        ── TPB AXI dest base
       ├─ aws_hal_get_dve_table_offsets  (§3)                ── per-table dest offsets
       ├─ aws_hal_get_dve_parameter_ram_params (§3)          ── param-RAM base + 0x400
       ├─ per table: dmem_alloc(TONGA_DRAM) → dmem_buf_copyin → vring_add_desc_transfer
       └─ dma_pring_alloc(TX/RX) → vring_dump_to_pring → fill dve_config_dma_state_t
```

### Algorithm

The loader builds a one-shot DVE-table-load vring. The SUNDA-vs-CAYMAN/MARIANA divergence is the table count — SUNDA loads 4 and **explicitly skips control-slow** (sets its slot to `0`); CAYMAN and MARIANA load all 5 and are byte-identical to each other apart from name / assert-path / log-tag:

```c
// dve_dynamic_config_init_<arch>(ctx, neff_cfg, out dve_config_dma_state_t)
//   sunda @0x31dfc0 / cayman @0x31ea40 / mariana @0x31f580 — tdrv/<arch>/dve_dynamic_config_<arch>.c
function dve_dynamic_config_init(ctx, src, out state):
    tables = src ? src->tables : get_dve_default_bins(arch)   // NEFF tables or built-in defaults
    h = sha256_init()
    sha256_update(h, tables.opcode); sha256_update(h, tables.control_fast)
    if arch != SUNDA:                                         // <-- ARCH GATE
        sha256_update(h, tables.control_slow)                 // SUNDA omits control-slow entirely
    sha256_update(h, tables.datapath); sha256_update(h, tables.param_ram)
    state.dve_tbl_hash = sha256_final(h)                      // assert(dve_tbl_hash) :0x35

    get_dve_table_offsets_<arch>(&off_op, &off_cf, &off_cs, &off_dp)   // §3 — per-arch dests
    get_dve_parameter_ram_params_<arch>(&off_pr, &sz_pr)              // §3 — 0x400 bytes = 256 floats
    axi = aws_hal_stpb_get_axi_offset(ctx)                   // TPB AXI dest base for this DVE tile

    queue = model_dma_engine_and_queue_bundle_alloc("qDveTable")     // assert nqueues==1
    vring = vring_set_allocate("qDveTbl")                            // assert count==1
    for each (tbl, off) in {(opcode,off_op), (control_fast,off_cf),
                            arch!=SUNDA ? (control_slow,off_cs) : SKIP,   // <-- 4 vs 5 tables
                            (datapath,off_dp), (param_ram,off_pr)}:
        buf = dmem_alloc(TONGA_DRAM, tbl.size)
        dmem_buf_copyin(buf, tbl.buffer, tbl.size)
        vring_add_desc_transfer(vring, buf.pa_aligned, axi + off, tbl.size)
    // event-accel completion descriptor:
    vring_add_event_accel(vring, tdrv_arch_get_evt_accel_addr(ctx), tdrv_arch_get_evt_addr(ctx))
    dma_pring_alloc(TX/RX); vring_dump_to_pring(vring)
    fill state {opcode_table, control_fast_table,
                control_slow_table = (arch==SUNDA ? 0 : <buf>),       // SUNDA: NULL slot
                datapath_table, bn_param_ram, queue}

// FNV-1a 64-bit — fnv_64a_buf @0x3200c0 (backs kbin ht_buf_*); prime 0x100000001B3
function fnv_64a_buf(buf, len, hval):                        // seed hval passed in
    for b in buf[0..len): hval = (hval ^ b) * 0x100000001B3
    return hval
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `dve_dynamic_config_init_sunda` | `0x31dfc0` | SUNDA DVE loader — 4 tables, **skips control-slow** (`state.control_slow_table = 0`) | CERTAIN |
| `dve_dynamic_config_init_cayman` | `0x31ea40` | CAYMAN DVE loader — all 5 tables | CERTAIN |
| `dve_dynamic_config_init_mariana` | `0x31f580` | MARIANA DVE loader — byte-identical to cayman (name/path/tag aside) | CERTAIN |
| `fnv_64a_buf` | `0x3200c0` | FNV-1a 64 over `(buf,len)` seeded by `hval`; prime `0x100000001B3` | CERTAIN |
| `fnv_64a_str` | `0x320140` | FNV-1a 64 over NUL-terminated string (no in-image caller) | CERTAIN |

### Considerations

> **QUIRK —** SUNDA's loader sets `dve_config_dma_state_t.control_slow_table = 0` (struct offset `+16`) and loads only 4 tables; the control-slow micro-table does not exist on Trn1-class silicon. A reimplementer who unconditionally loads 5 tables programs a phantom control-slow region on SUNDA. CAYMAN and MARIANA bodies are byte-identical — the only differences are the assert TU-path string, the function name, and the nlog tag — so the *Trn2/Trn3* DVE-load path is genuinely shared, while *Trn1* is the special case. (The 256-entry param RAM, `0x400` bytes, is a reciprocal table `1/(i+1)` for `i=0..255` from the SIMD `_mm_div_pd` loop — math HIGH, the BatchNorm interpretation of `bn_param_ram` MED.)

> **NOTE —** the `dve_tbl_hash` SHA-256 over the tables is a content fingerprint, not a security check — it dedups identical DVE configs across NEFF loads (the same role FNV-1a plays for the kbin hashtable). The `+168` registration and the `qDveTbl` (`0x6C625465764471`) queue name are the wiring evidence; the loader is reached only through `tdrv_arch_ops`, never called directly.

## Related Components

| Name | Relationship |
|---|---|
| `al_reg_read32 @0x2658a0` / `al_reg_write32 @0x265c50` | the Alpine-HAL wrappers the §2 local-register accessors tail-jump into; they call `csr_read`/`csr_write` ([hal-adapter §2](hal-adapter.md)) |
| `ndl_bar_read @0xC3590` / `ndl_bar_write @0xC35E0` | the kernel BAR-0 IOCTL the `csr_*` primitives route every region-validated access to |
| `kaena_khal_register_funcs_v2/v3/v4` (`0x468740`/`0x46ed70`/`0x4622e0`) | install the §3 getter leaves into `kaena_khal.khal_arch` per arch (2→sunda, 3→cayman, 4→mariana) |
| `tdrv_arch_register_<arch>` | installs `dve_dynamic_config_init_<arch>` into `tdrv_arch_ops +168` and the `csr_get_regions`/`csr_*_device` slots (`+424`/`+432`/`+440`) |
| `aws_hal_arch_<arch>_get_xt_local_reg_offset` (sunda `0x479070` / cayman `0x47afe0` / mariana `0x4778f0`) | computes the per-engine window base the §2 local-register accessors add their fixed offset to |

## Cross-References

- [KaenaHal: Register and Reg-Offset Accessors](hal-registers.md) — owns the **accessor mechanism** (arch-gate + `khal_arch` slot + tail-call); this page owns the **per-arch values** those trampolines resolve to, fulfilling that page's explicit deferral
- [Per-Arch Device Layer: STPB Engine-Init, DGE and Pooling](arch-stpb.md) — the STPB leaf bodies that *consume* these offsets (DVE RNG block, DGE carveout cap, the SUNDA HW-decode-stub matching half of §3's `__noreturn` getter)
- [Per-Arch Device Layer: Geometry and Address Map](arch-geometry.md) — the per-generation region geometry (HBM/PSUM/SBUF maps, the engine bases PE `0x802600000` / DVE `0x802B00000`, the Trn3-mesh `+0x800000000` high bit) the §3 base tables index into
- [KaenaHal: Overview and Platform-Services Adapter](hal-adapter.md) — `al_reg_{read,write}32` and `al_mem_write_buf`, the wrappers that bottom into the `csr_read`/`csr_write`/`axi_write` primitives documented in §1
- [TDRV: arch-ops Dispatch and Sync-Event Accessors](tdrv-arch-ops.md) — owns the `tdrv_arch_ops` vtable (the `csr_get_regions +432` / `csr_deregister_device +440` / `dve_dynamic_config_init +168` slots §1 and §4 route through)
- [back to index](../index.md)
