# Overview and Heavy-Frame Census

> **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` → `libnrt.so.1` → `libnrt.so.2.31.24.0` · **Version** `2.31.24.0-0b044f4ce` (`.nrt_brazil_version` @`0xad41f0` = `"2.31.24.0"`) · **BuildID[sha1]** `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` · ELF64 LSB DYN x86-64, SONAME `libnrt.so.1` · **122,956,336 bytes** on disk (~117 MiB) · **NOT stripped** — `.symtab` (25,112 syms) + full DWARF v4 present. All four `PT_LOAD` segments are identity-mapped (`p_offset == p_vaddr`), so **every** PROGBITS section — `.text`, `.rodata`, **and `.data`** — has VMA == file offset; `.bss` is `SHT_NOBITS` and has no file content.
>
> **CORRECTION —** earlier revisions of this page (and `runtime/*` siblings) claimed `.data`/`.bss` differ from their VMA by a `0x400000` delta. That is **wrong** for `libnrt.so`. `readelf -lW libnrt.so.2.31.24.0` shows the RW `LOAD` segment at `Offset 0xbeeaa0 == VirtAddr 0xbeeaa0` (delta **zero**), and `readelf -SW` shows `.data` at `Address 0xc07e00 / Off 0xc07e00` — identical. The `0x400000` delta is a fact about a *different* binary (the libtpu / Kaena-profiler image), not libnrt. Read `.data`-resident globals at their VMA directly.
>
> **Part II — Binary Anatomy & Forensics** / Section map · **Evidence grade:** byte-anchored (counts cross-checked `nm`/`readelf`/`objdump` vs IDA `*_functions.json`/`*_frames.json`/`*_callgraph.json` sidecars) · [back to index](../index.md)

## Abstract

This page is the **orientation map for reading `libnrt.so` as an artifact**. The runtime ships as a single 117 MiB shared object that statically vendors every heavy dependency it touches: `objdump -p` lists `DT_NEEDED` only for `libgcc_s`/`libutil`/`librt`/`libpthread`/`libdl`/`libstdc++`/`libm`/`libc`/`ld-linux` — there is **no** NEEDED entry for protobuf, Abseil, simdjson, zlib, or the Rust runtime, so all of those live inside this `.text`. Reading it is the familiar exercise of opening a large stripped-but-here-*unstripped* C++/Rust monolith: most of the byte mass is mechanical (vendored OSS object code and compiler-generated glue you must recognize and skip), and a minority is the first-party `KaenaRuntime` logic that the rest of this Part dissects.

The job of this page is to draw that line quantitatively. DWARF exposes **331 compile units**: **203 first-party `KaenaRuntime` TUs (4,407 defined functions)** built from the single source root `/opt/workspace/KaenaRuntime/`, plus **122 vendored/third-party TUs (4,384 functions)** — Abseil, the Rust `std`/`core`/`alloc` crates, the `KaenaProfilerFormat` protobuf, Libarchive, simdjson, `KaenaDriverLib`, zlib. Those 8,791 DWARF subprograms-with-`low_pc` are the *named* code; IDA recovers **17,372 functions** total, the balance being PLT thunks, `.cold` split fragments, and outlined trampolines that carry no DWARF subprogram of their own. By byte budget the heavy mass splits roughly **41.3% vendored / 58.7% first-party** (3,148,991 B / 4,469,317 B over the 7,618,308-byte `.text`).

The page then quantifies the four "heavy" axes — byte size, stack-frame size, in-degree, fan-out — to point a reimplementer at where the work concentrates. The runtime's weight is **not** in its small public C API (`nrt-api proper` is ~0.20 MB / 341 fns); it is in **ISA instruction validation** (~1.42 MB / 369 fns, the largest single first-party region), **collective-algorithm composition** (~1.06 MB / 1,129 fns), and **NEFF parsing** (few but enormous per-function). Each deep region is a sibling page in this Part or in the subsystem Parts; this overview links them and does not duplicate their derivations.

For reimplementation, the contract this page establishes is:

- **The provenance split** — which functions are first-party `KaenaRuntime` (worth reimplementing), which are vendored OSS (link the real library instead), and which are compiler-generated (reproduce structurally, never byte-for-byte). All three are quantified below with real counts.
- **The heavy-frame census** — the byte-size, stack-frame, in-degree, and fan-out outliers, each anchored to symbol + address, so a port knows which functions are line-count sinks, which are stack-overflow hazards, and which leaves are reached by a third of the call graph.
- **The reading order** — a pointer table to every Part-II sub-page and to the subsystem Parts where each densest region is reimplemented in full.

| | |
|---|---|
| **On-disk size / `.text` size** | 122,956,336 B (~117 MiB) / 7,618,308 B |
| **DWARF compile units** | 331 (203 first-party + 122 vendored + builtins) |
| **DWARF subprograms (`low_pc`)** | 8,791 (4,407 first-party + 4,384 vendored) |
| **IDA-recovered functions** | 17,372 (balance = PLT/thunk/`.cold`/outlined) |
| **First-party / vendored byte split** | 4,469,317 B (58.7%) / 3,148,991 B (41.3%) |
| **`.symtab` symbols** | 25,112 (`nm --defined-only`) |
| **Toolchains** (`.comment`) | GCC 14.2.1 (C++17), GCC 7.3.1/8.5.0 (legacy C), rustc 1.91.1, clang 21.1.0-rc2 |
| **RTTI** | 201 `_ZTV` / 244 `_ZTI` / 245 `_ZTS`; **no** multiple/virtual inheritance |
| **`.init_array`** | 77 ctors @`0xbf2b08` (616 B) |
| **Source root** | `/opt/workspace/KaenaRuntime/` (`DW_AT_comp_dir` `…/build/private/develop/almalinux/RelWithDebInfo`) |

> **NOTE —** the "117 MB" and "122,956,336 bytes" figures are the same object measured two ways (117.27 MiB). The disk size is dominated by DWARF (`.debug_*`) and the embedded ucode/dve/hw_decode data blobs; executable code is only 7.6 MB. A reimplementer sizing the *logic* should reason about the `.text` budget, not the on-disk size.

---

## 1. Provenance split — the pie, as a table

Every function in `libnrt.so` falls into one of three provenance classes. The split is the single most useful fact for orienting a reimplementation: it says what to rebuild, what to link, and what to regenerate. Counts below are from the DWARF CU partition (`readelf --debug-dump=info`, `DW_AT_name`/`DW_AT_comp_dir`) for the function/CU columns and from the IDA `*_functions.json` `.size` aggregate for the byte budget; the two methods are reconciled in §1.3.

### 1.1 By DWARF compile unit (the authoritative function partition)

`fn` = `DW_TAG_subprogram` with a `DW_AT_low_pc` inside the CU, deduped by `low_pc`. This is the count of *defined bodies*, and it is HIGH confidence — every path is a verbatim `DW_AT_name` string.

| Provenance | CUs | fns | What it is | Class |
|---|---:|---:|---|---|
| **`KaenaRuntime` first-party** | 203 | **4,407** | The runtime itself: `tdrv`/`enc`/`nrt`/`kelf`/`kmgr`/`nds`/`ucode`/`utils`/`nlog`/… (§2) | FIRST-PARTY |
| Rust `std` | 1 | 1,208 | `library/std` cgu — backtrace/symbolization, TLS, panic machinery | VENDORED |
| Abseil `lts_20230802` | 91 | 1,177 | C++ support runtime: cord/status/strings/log/synchronization/time/swisstable | VENDORED |
| `KaenaProfilerFormat` protobuf | 2 | 730 | `ntff.pb.cc` (535) + `neuron_trace.pb.cc` (195) — **generated** accessors | GENERATED (AWS schema) |
| Rust `core` | 1 | 543 | `library/core` cgu | VENDORED |
| Libarchive `3.8.0dev` | 13 | 362 | NEFF = gzip-tar: tar-format + gzip-filter readers + `archive_entry` | VENDORED |
| `KaenaDriverLib 2.27.4.0` (`ndl.c`) | 1 | 123 | userspace driver shim (IOCTL/mmap wrappers) | VENDORED (AWS) |
| Rust `alloc` | 1 | 100 | `library/alloc` cgu | VENDORED |
| simdjson `0.9.0` | 1 | 83 | NEFF JSON-metadata parser (dual-arch westmere+haswell) | VENDORED |
| Rust crates (memchr/hashbrown/panic_unwind/std_detect) | 4 | 55 | `memchr-2.7.5` (41) + `hashbrown-0.15.5` (6) + panic_unwind (5) + std_detect (3) | VENDORED |
| zlib `1.3.1` (builtin) | 6 | 36 | inflate/inftrees/inffast backing Libarchive gzip | VENDORED |
| Rust compiler-builtins / compiler-rt | 7 | 4 | `__*` intrinsics | VENDORED |
| **Vendored total** | **122** | **4,384** | (object code linked in, kept separate per [SBOM](vendored-sbom.md)) | VENDORED |

> **CORRECTION (F-3PVER) —** the source-map cell originally noted "no protobuf-runtime CU is in libnrt … Abseil is the C++ support runtime instead", reading the absence of a protobuf-runtime DWARF CU as absence of the runtime. The runtime **is** statically linked: `nm` shows **3,003 defined `google::protobuf` text symbols** including `SerialArena`/`ThreadSafeArena`/`ArenaStringPtr` and `VerifyVersion` @`0x6fb690`. The DWARF gap is a `-g`-less build of the runtime TUs (no line tables), not missing code. Abseil is *also* present — both are; they are not alternatives. See [Vendored-Library SBOM](vendored-sbom.md).

### 1.2 By byte budget (where the line-count actually lives)

The CU table counts *functions*; this table counts *bytes*, which is the better proxy for reimplementation effort. Attribution is by demangled name-prefix over all 17,372 IDA records (MED–LOW on the edges, since `.constprop`/`.isra`/`.part` thunks can mis-bucket by a few percent; the totals are HIGH from `objdump -p` + `.size` sums).

| Bucket | Bytes | fns | Class | Note |
|---|---:|---:|---|---|
| **isa-validate (1P)** | 1,423,391 | 369 | FIRST-PARTY | **largest single 1P region** — per-opcode legality DSL |
| protobuf | 1,164,525 | 3,096 | VENDORED | TcParser/Arena/DescriptorBuilder |
| **collectives/enc (1P)** | 1,061,879 | 1,129 | FIRST-PARTY | mesh/hier/ring composers + schedulers |
| rust-std | 776,161 | 2,730 | VENDORED | backtrace_rs/gimli symbolization mass |
| abseil | 746,900 | 1,913 | VENDORED | cctz/str_format/cord |
| libstdc++ | 554,365 | 1,616 | VENDORED (hdr-only inst.) | template instantiations; `.so.6` is dynamic |
| **nrt-api proper (1P)** | 201,744 | 341 | FIRST-PARTY | the public C API surface is *small* |
| simdjson | 145,045 | 117 | VENDORED | ×2: westmere + haswell stage2 copies |
| **AL-HAL (1P)** | 87,455 | 471 | FIRST-PARTY* | AnnapurnaLabs `al_*` HAL (distinct upstream) |
| **neff-parse (1P)** | 44,990 | 10 | FIRST-PARTY | few functions, huge each |
| **tdrv (1P)** | 41,903 | 260 | FIRST-PARTY | device-driver core |
| **neuron_rustime (1P-logic)** | 30,438 | 43 | FIRST-PARTY via Rust | sys_trace crate (AWS-authored Rust) |
| zlib | 25,238 | 33 | VENDORED | inflate/deflate |
| **act-tables (1P)** | 20,892 | 55 | FIRST-PARTY | activation-table staging |

> **QUIRK —** the public C API (`nrt_*`) that consumers link against is one of the *smallest* first-party regions (0.20 MB, 341 fns). The runtime's mass is behind that thin façade: NEFF parsing, ISA validation, and collective-algorithm composition. A reimplementer who scopes effort from the export list (149 `NRT_*` symbols) will badly under-budget. The exports are thunks into a 4.5 MB first-party body.

> **NOTE —** AL-HAL (`al_*`, AnnapurnaLabs/Annapurna Labs HAL) is marked `FIRST-PARTY*` because it ships only inside AWS Neuron but is a distinct upstream component vendored from the driver tree. The HAL *functions* installed into `kaena_khal` are owned by [Part IV's HAL pages](../runtime/hal-adapter.md); this Part owns the *struct/singleton* layout in [Globals Atlas](globals-atlas.md).

### 1.3 Reconciling the two counts (DWARF 8,791 vs IDA 17,372)

The DWARF subprogram count (8,791) and the IDA function count (17,372) differ by 8,581. That gap is not error; it is the mechanical residue:

```text
17,372  IDA-recovered functions (functions.json)
 8,791  DWARF subprograms-with-low_pc  (= 4,407 first-party + 4,384 vendored)
 ──────
 8,581  PLT stubs + .plt.sec thunks
        + .cold split fragments (e.g. nrt_config_parse_init_config.cold @0x3f18e, 0xf82)
        + compiler-outlined .constprop/.isra/.part clones
        + static-data trampolines  — none carry their own DWARF subprogram
```

A reimplementer should treat the DWARF set as the **logical** function inventory (what to reproduce) and the IDA delta as **toolchain artifacts** (regenerated automatically by any optimizing compiler). The `.cold` split in particular means a single source function appears as two IDA records: the hot half keeps the `nm t` symbol; the cold half is a separate chunk. Byte sizes in §3 are the *full extent including* `.cold`.

---

## 2. First-party subsystem tree

The 4,407 first-party functions are built from one source root, `/opt/workspace/KaenaRuntime/`, in 203 TUs. The fn-weighted tree below is the second-level map a reader uses to decide which subsystem Part to open; each leaf is reimplemented in the named Part, not here.

```text
KaenaRuntime/                                              CUs    fns   → owning Part
 ├─ tdrv/    device driver + instruction-block builders     84   2413   Part IV / Part VI
 │    ├─ cayman/ mariana/ sunda/   (per-silicon arch+isa+dve)
 │    └─ encd/ (+ encd/archs/)     execution-engine descriptor compiler  → Part IX
 ├─ enc/     host collectives engine                         52   1062   Part IX
 │    ├─ async_sr/                 async P2P sendrecv (OFI/rendezvous)
 │    └─ switch_platform/          switch-fabric collective composer
 ├─ nrt/     public C API + config/profile/sys_trace         27    377   Part IV / Part XIII
 ├─ kelf/    KELF/KBIN/NEFF container parse + numpy/HLO stats  5    318   Part V
 ├─ kmgr/    model/kernel manager (dlr, xu worker queue)      10    117   Part VII
 ├─ nds/     Neuron DataStore (folded from libnds.a)           6     36   Part XIV
 ├─ ucode/   microcode loader glue                             1     26   Part XI
 ├─ utils/   md5/sha256/instance/time helpers                  5     23   —
 ├─ nlog/    logging + backtrace + symbol resolution           3     15   Part IV
 ├─ ndebug/  ndebug_stream                                     1     10   Part XIII
 ├─ dve_config/  DVE config + per-arch                         4      7   Part IV
 ├─ dx/      dx/ring.c (notification ring)                     1      2   Part VIII
 ├─ hw_decode/  hw_decode table glue                           1      1   Part VI
 └─ build/private/.../*_bins.c  GENERATED blob stubs           3      0   (data, no code)
                                                            ────  ─────
                                                             203   4407
```

The densest single TUs (by DWARF fn-count) are the per-arch instruction-block builders and the collectives core, which is exactly where §3's byte-size outliers land:

| TU (verbatim under `/opt/workspace/KaenaRuntime/`) | fns | Role |
|---|---:|---|
| `enc/enc.cc` | 352 | collective primitive core (largest C++ TU) |
| `tdrv/instruction_block_mariana.c` | 297 | per-arch instruction-block builder (largest TU overall) |
| `tdrv/instruction_block_cayman.c` | 248 | per-arch instruction-block builder |
| `tdrv/instruction_block_sunda.c` | 238 | per-arch instruction-block builder |
| `tdrv/encd.c` | 229 | execution-engine descriptor compiler |
| `enc/enc_primitive.cc` | 200 | collective primitives (send/recv leaves) |
| `kelf/kelf.cpp` | 184 | NEFF/KELF container decode |
| `kelf/kelf2kbin.cpp` | 126 | JSON → KBIN lowering |
| `tdrv/encd/archs/{arch,cayman,mariana,sunda}.c` | 101/95/95/88 | per-arch descriptor emitters |

> **GOTCHA —** the per-arch fan-out is **triplicated, not parameterized**. `cayman`/`mariana`/`sunda` each get their own `instruction_block_*.c` (248/297/238 fns), `tdrv_arch_*.c`, `encd/archs/*.c`, and `dve_dynamic_config_*.c`. A reimplementation that factors the three arches into one templated builder will not match the binary's call graph or its per-arch `tdrv_arch_ops` (47-slot @`0xc97180`) and `kaena_khal` (~200-slot @`0xcaeb80`) dispatch structs — those are installed by separate `register_funcs_v{2,3,4}` paths. See [Dispatch-Table Taxonomy](dispatch-tables.md).

---

## 3. Heavy-frame census

Four independent "heavy" axes matter to a port, and all four are present in the data. Each is anchored to symbol + address; byte sizes are HIGH (spot-checked `nm`: `0x8a1d0`=`0x5a30`=23,088 ✓, `0x2f2350`=`0x4620`=17,952 ✓, `0x45fd50`=`0x73e`=1,854 ✓).

### 3.1 Axis A — byte-size outliers (line-count sinks)

These are where reimplementation line-count concentrates. The top of the distribution is one config parser, then a wall of collectives composers/schedulers and ISA validators. `SIZE` is full extent including `.cold`; `BLK`/`INSN` from `functions.json`; `NCLR` = distinct callers.

| Addr | Size | Blk | Insn | Symbol | Subsystem | Conf |
|---|---:|---:|---:|---|---|---|
| `0x8a1d0` | 23,088 | 713 | 4,318 | `nrt_config_parse_init_config` | nrt-config 1P | CERTAIN |
| `0x190820` | 22,217 | 506 | 3,891 | `enc_mesh_primitive::__compose_allreduce_trn2` | collectives 1P | HIGH |
| `0x11f790` | 21,252 | 473 | 3,765 | `enc_post_operation(encd_context*, enc_op_list&)` | collectives 1P | HIGH |
| `0x1e3710` | 21,017 | 584 | 4,066 | `rdh_scheduler::schedule_16_dev_reduce_scatter_vnc1` | collectives 1P | HIGH |
| `0x1eedd0` | 20,169 | 558 | 3,874 | `rdh_scheduler::schedule_16_dev_reduce_scatter` | collectives 1P | HIGH |
| `0x2f2350` | 17,952 | 530 | 3,555 | `is_valid_neuron_instruction` | isa-validate 1P | CERTAIN |
| `0x4bc620` | 16,969 | 535 | 3,062 | `parse_one_dma_block(mla_resources&, simdjson::…)` | neff-parse 1P | HIGH |
| `0x1eac40` | 16,781 | 537 | 3,192 | `rdh_scheduler::schedule_4_dev_reduce_scatter` | collectives 1P | HIGH |
| `0x3a60b0` | 16,606 | 499 | 3,288 | `is_valid_neuron_isa_instruction` | isa-validate 1P | HIGH |
| `0x43f810` | 16,087 | 488 | 3,202 | `ib_is_valid_engine_instruction_v4` | isa-validate 1P | HIGH |
| `0x246ae0` | 14,779 | 352 | 3,064 | `encd_load_executable` | neff-parse 1P | HIGH |
| `0x510116` | 14,933 | 480 | 3,036 | `std::backtrace_rs::symbolize::gimli::Cache::with_global` | rust-std V | HIGH |
| `0x69aa60` | 12,335 | 605 | 2,596 | `protobuf::internal::TailCallTableInfo::ctor` | protobuf V | HIGH |
| `0x26a310` | 11,922 | 427 | 2,752 | `tdrv_init` | tdrv 1P | HIGH |
| `0x4c0870` | 11,639 | 389 | 2,260 | `kelf_load_from_neff(neff*, …, mla_resources&)` | neff-parse 1P | HIGH |
| `0x9aec0` | 10,054 | 374 | 2,132 | `event_to_proto(nrt_sys_trace_event&, …)` | sys_trace 1P | HIGH |

Across the top-50 by size: **28 are collectives** (`enc_*`/`rdh_*`/`inter_rdh_*`), 6 NEFF-parse, 4 ISA-validate, 2 nrt-config, and 6 vendored (protobuf ×2, abseil ×2, simdjson ×4). The collectives composer/scheduler family is the dominant heavy region.

> **NOTE —** simdjson appears **twice** in the heavy list — `simdjson::westmere::dom_parser_implementation::stage2` (`0x4fa790`, 10,451 B) and `simdjson::haswell::…stage2` (`0x4f5820`, 10,171 B). Both are present because simdjson 0.9.0 ships per-microarchitecture copies and selects at runtime by CPU dispatch. A reimplementation that links one simdjson build will not reproduce the dual copy; that is correct, not a discrepancy.

### 3.2 Axis B — stack-frame outliers (clean-room hazards)

These are the functions a naïve port will get *wrong at runtime*, not at compile time: they reserve enormous stack frames that overflow a default thread stack. `frames.json` `vars[]` enumerate the exact large locals.

| Addr | Frame | Symbol | Hazard | Conf |
|---|---:|---|---|---|
| `0x12a0c0` | **6,754,304** | `enc_find_cc_context_by_signature` | single 6,753,648-B struct local `enc_ctx` — **blows an 8 MB stack if nested** | HIGH |
| `0x319a30` | **685,160** | `act_local_storage_tbls_setup` | 4 device-table staging buffers (524 KB + 2×64 KB + 16 KB) on stack | HIGH |
| `0x318d30` | ~163,840 | `stage_act_tbl_v2` | 160 KB activation-table staging | HIGH |
| `0x2f7e50` | **141,096** | `ib_create_one_block` | 69 locals; per-block instruction builder | HIGH |
| (per-op) | 31,744–40,960 | `dbg_is_valid_<op>_{,_0,_1}` family | each per-opcode validator carries a 1,281-B diagnostic buffer + 50–69 locals | MED |

> **GOTCHA —** `enc_find_cc_context_by_signature` (`0x12a0c0`) reserves a **6.75 MB stack frame** for one `enc_ctx` struct local. On a default 8 MB pthread stack this is one nested call from overflow; on the smaller stacks used by worker pools it overflows immediately. Any reimplementation **must heap-allocate** `enc_ctx` (and the `act_local_storage_tbls_setup` staging buffers) rather than reproduce the on-stack layout. The binary gets away with it because these run on the main thread early in bring-up; a port that calls them from a `kmgr/xu` worker will crash. Census: **8 functions exceed a 64 KB frame; 341 exceed 8 KB.** Full list → [TDRV Bring-Up](../runtime/tdrv-lifecycle.md).

### 3.3 Axis C — in-degree outliers (the leaves to port first)

The most-called functions are the leaves a reimplementation must stub or port *first*, because a third of the call graph reaches them. Runtime intrinsics are filtered out to surface the first-party hot leaves. Two metrics: distinct-caller count (`NCLR`) and total call-site count.

| Symbol | Distinct callers | Call sites | Size / Addr | Role |
|---|---:|---:|---|---|
| `nlog_write` | 1,193 | 3,795 | 135 B @`0x224d40` | **the central log sink** — ~1/3 of all fns reach it |
| `al_hal_log` | 446 | 1,056 | 163 B @`0x265b80` | AnnapurnaLabs HAL log |
| `al_hal_tpb_get_arch_type` | 332 | — | — | per-core arch dispatch |
| `al_reg_read32` / `al_reg_write32` | 193 / 177 | — / 729 | 127 B @`0x265c50` | HAL CSR read/write |
| `al_abort_program` | 159 | — | — | HAL fatal abort |
| `neuron_isa_tpb_new_dbg_bool_1` / `_0` | 107 / 104 | 1,025 / 967 | 119 B @`0x3aad80` / `0x323e40` (leaf) | ISA-validate bool helper (arch variants) |
| `neuron_isa_tpb_merge_dbg_bool_1` / `_0` | — | 857 / 804 | 392 B @`0x3aae00` / `0x323ec0` (leaf) | ISA-validate merge helper |
| `dbg_has_valid_neuron_header_1`/`_0`, `…events_1`/`_0` | 100/98 | — | — | ISA header/event validators |
| `tdrv_arch_ops_init` / `ensure_arch_ops_initialized` | 78 / 78 | — | — | tdrv arch-vtable init |

> **QUIRK —** `nlog_write` and the `al_*` HAL primitives are *tiny* (119–163 B) but pervasive — they are the first leaves any port must stub. The `neuron_isa_tpb_*_dbg_bool`/`merge_bool` leaves come in `_0`/`_1`/base triplets (three arch variants of the same 119/392-B validation primitive); the 369-function ISA-validate tree is built entirely from these. Reference intrinsic toppers (filtered above): `__assert_fail` 2,308 distinct callers, `_Unwind_Resume` 1,751, `operator delete` 1,382, `operator new` 1,185, `memcpy` 738.

### 3.4 Axis D — fan-out outliers (the deepest orchestration chains)

The widest functions — most distinct callees — are the collective schedulers and the config/NEFF parsers. These are the deepest orchestration roots; a port must reproduce their call structure, not just their bodies.

| Callees | Addr | Symbol | Chain |
|---:|---|---|---|
| 583 | `0x1e3710` | `rdh_scheduler::schedule_16_dev_reduce_scatter_vnc1` | collectives compose |
| 538 | `0x1eedd0` | `rdh_scheduler::schedule_16_dev_reduce_scatter` | collectives compose |
| 523 | `0x8a1d0` | `nrt_config_parse_init_config` | config parse/dispatch |
| 432 | `0x9aec0` | `event_to_proto` | trace serialization |
| 290 | `0x4b36b0` | `parse_one_variable` | NEFF parse |
| 280 | `0x4bc620` | `parse_one_dma_block` | NEFF parse |
| 244 | `0x26a310` | `tdrv_init` | device bring-up |
| 241 | `0x2f7e50` | `ib_create_one_block` | instruction-block build |

Three named hot-path roots tie the axes together:

```text
NEFF LOAD       nrt_load → parse_neff_json (0xe1d10) → {parse_one_dma_block 0x4bc620,
                  parse_one_engine_instr 0x4b7e30, parse_one_variable 0x4b36b0}
                  → kelf_load_from_neff (0x4c0870) → encd_load_executable (0x246ae0)
                  ↳ bottoms out in simdjson westmere/haswell stage2 (0x4fa790 / 0x4f5820)

COLLECTIVES     enc_post_operation (0x11f790) → enc_init_comm (0x135d60)
                  → init_hierarchical_groups (0x12c820) → alg_mesh_build_full_mesh (0x125fb0)
                  → enc_*_primitive::__compose_* (28 composers)
                  → rdh_scheduler::schedule_* (deepest fan-out, 538–583 callees)

ISA VALIDATE    is_valid_neuron_instruction (0x2f2350) / is_valid_neuron_isa_instruction (0x3a60b0)
                  / ib_is_valid_engine_instruction_v4 (0x43f810)
                  → ~50 dbg_is_valid_<op>_{,_0,_1} per-opcode validators (31–41 KB frame each)
                  → neuron_isa_tpb_*_dbg_bool/merge_bool leaves (§3.3)

DEVICE BRING-UP tdrv_init (0x26a310) — 244 callees; installs tdrv_arch_ops + kaena_khal
```

---

## 4. Mechanical vs first-party-interesting

The governing distinction for reading this binary: **most bytes are mechanical and should be recognized-and-skipped; the minority that is first-party `KaenaRuntime` is what the rest of this book reimplements.** Three classes of mechanical mass:

| Mechanical class | Bytes / fns | How to treat it | Anchor |
|---|---|---|---|
| **Vendored OSS** (protobuf 5.26.1, Abseil lts_20230802, simdjson 0.9.0, Libarchive 3.8.0dev, zlib 1.3.1, Rust std/core/alloc + crates) | ~3.15 MB / 8,499 fns | Link the real library at the pinned version; do not reverse it | [Vendored SBOM](vendored-sbom.md) |
| **Compiler-generated protobuf** (`ntff.pb.cc` / `neuron_trace.pb.cc`, 730 fns) | — | Regenerate from the recovered `.proto`; the accessors are `protoc` output | [NTFF format](../trace/ntff-format.md) |
| **CRT / static-init / PLT glue** (77 ctors, `__static_initialization_and_destruction_0` @`0x75250`, PLT thunks, `.cold` clones) | — | Reproduce structurally; any optimizing toolchain emits equivalents | [Static-Init](static-init.md), [CRT/PLT](crt-plt.md) |

The **first-party-interesting** mass — the 4.47 MB / 8,873 fns worth deep pages — is the ISA-validation DSL, the collective composers/schedulers, the NEFF parse pipeline, the `tdrv` device layer, the per-arch instruction-block builders, the `nrt_config` parser, and the `neuron_rustime` sys_trace bridge. The RTTI surface confirms the partition independently: of 201 vtables, **97 are `google::protobuf`, 16 Abseil, 13 `std`, 11 simdjson** (all vendored), while only **40 `ntff::` (AWS-authored schema) and 24 first-party global-namespace classes** (`enc_*`/`mem_ref*`/`dma_desc*`/`alg_mesh_*`) carry domain logic — and there is **no multiple/virtual inheritance anywhere** (244 `_ZTI` = 53 `__class_type_info` root + 191 `__si_class_type_info`, 0 `__vmi_class_type_info`). See [RTTI Class Hierarchy](rtti-class-hierarchy.md).

> **NOTE —** the `ntff::` 40-message set sits on the mechanical/interesting boundary: the *accessors* are generated (mechanical, regenerate), but the *schema* (40 message classes, the `.ntff` trace wire format) is AWS-authored and reimplementation-relevant. Treat the `.pb.cc` bodies as `protoc` output and the message field layout as a first-party artifact — covered in [NTFF Trace Format](../trace/ntff-format.md).

---

## 5. Reading order — the Part-II sub-pages

This overview is the index for Part II. Each sibling page takes one forensic axis to byte-level depth; open them in roughly this order.

| Page | Covers | Key anchors |
|---|---|---|
| [ELF Anatomy](elf-anatomy.md) | Section table, segment layout (all four `PT_LOAD` identity-mapped, so `.data` is also VMA==offset — no `0x400000` delta), DWARF presence | `readelf -S`/`-l`/`-n`; build-id `8bb57aba…` |
| [Vendored-Library SBOM](vendored-sbom.md) | The 14-row version-pinned SBOM; protobuf 24-vs-26 and Rust 1.89-vs-1.91 conflict resolutions | `GOOGLE_PROTOBUF_VERSION 5026001`, `/rustc/ed61e7d7e…`, `VerifyVersion` @`0x6fb690` |
| [Static-Init Pipeline](static-init.md) | The 77-entry `.init_array` order, which globals each ctor touches, what is lazy vs eager | `.init_array` @`0xbf2b08`; ctors #10–#29 first-party |
| [Globals and Singletons Atlas](globals-atlas.md) | The first-party control plane: `kaena_khal`, `tdrv_arch_ops`, `nrt_config`, `ngc`, `vcores`, `async_sr_ctxs`, `ucode_func_symbols` | `kaena_khal` @`0xcaeb80`; `nrt_config` @`0xc5c480` |
| [Dispatch-Table Taxonomy](dispatch-tables.md) | The two coexisting per-arch vtables (`kaena_khal` ~200 slots vs `tdrv_arch_ops` 47 slots), seed tables, `register_funcs_v{2,3,4}` | seed `off_BF3698`/`0xbf3778`; `kaena_khal_init` selector |
| [C++ Class Hierarchy and RTTI](rtti-class-hierarchy.md) | 201 vtables / 244 typeinfo; `mem_ref` 10-class tree, `dma_desc` 4-class, `enc_ins`/`enc_proxy_task`, `ntff::` 40 messages | `_ZTV` @`0xbf6958`–`0xbf8dc8` (1P); SI-only |
| [String Domain](string-domain.md) | Provenance markers: `KaenaRuntime` `__FILE__` manifest, vendored OSS names, Rust panic strings, protobuf symbols, the env-var catalog | `/opt/workspace/KaenaRuntime/…` paths; `.nrt_brazil_version` |
| [CRT / PLT / Loader Surface](crt-plt.md) | `DT_NEEDED` (9 libs), PLT/GOT, `.init`/`.fini`, `__cxa_atexit` registrations, the dlopen targets (libncfw / libnrtucode_extisa) | `ucode_init_module` @`0x225940`; `encd_libncfw_init` @`0x251cc0` |

---

## Related Components

| Subsystem Part | Relationship to the heavy regions on this page |
|---|---|
| [Part IV — Runtime Core](../runtime/overview.md) | Owns `tdrv_init`, `nrt_config_parse_init_config`, the public C API thunks, the HAL function bodies |
| [Part V — NEFF / KBIN](../neff/overview.md) | Owns the `parse_one_*` / `kelf_load_from_neff` / `encd_load_executable` NEFF pipeline (Axis A/D) |
| [Part VI — TPB ISA](../isa/overview.md) | Owns the 369-fn ISA-validate DSL (`is_valid_neuron_instruction`, `dbg_is_valid_<op>`) — the largest 1P region |
| [Part IX — On-Device Collectives](../collectives/overview.md) | Owns the `enc_*`/`rdh_*` composer/scheduler family (28 of top-50 by size; deepest fan-out) |
| [Part XIII — Profiling & Trace](../trace/overview.md) | Owns `event_to_proto`, the `neuron_rustime` sys_trace bridge, the `ntff::` generated schema |

## Cross-References

- [ELF Anatomy](elf-anatomy.md) — section/segment table, DWARF presence, and proof that all `PT_LOAD` segments are identity-mapped (`.data` is VMA==offset — no `0x400000` delta)
- [Vendored-Library SBOM](vendored-sbom.md) — version pins for the 41.3% vendored byte mass; protobuf/Rust conflict resolutions
- [Static-Init Pipeline](static-init.md) — the 77-ctor `.init_array` order and lazy-vs-eager global init
- [Globals and Singletons Atlas](globals-atlas.md) — the first-party singleton control plane (`kaena_khal`, `nrt_config`, …)
- [Dispatch-Table Taxonomy](dispatch-tables.md) — the per-arch vtable pair behind the triplicated `cayman`/`mariana`/`sunda` code
- [C++ Class Hierarchy and RTTI](rtti-class-hierarchy.md) — the 24 first-party + 40 `ntff::` classes vs the 137 vendored vtables
- [String Domain](string-domain.md) — the `KaenaRuntime` `__FILE__` provenance markers that ground the source map
- [CRT / PLT / Loader Surface](crt-plt.md) — `DT_NEEDED`, PLT/GOT, and the libncfw/libnrtucode dlopen edges
- [Part IV — Runtime Core](../runtime/overview.md) — where the first-party-interesting `tdrv`/`nrt`/HAL mass is reimplemented
- [Part VI — TPB ISA](../isa/overview.md) — where the largest single first-party region (ISA validation) is reimplemented
- [Part IX — Collectives](../collectives/overview.md) — where the heaviest algorithmic mass (composers/schedulers) is reimplemented
- [back to index](../index.md)
