# Dispatch-Table Taxonomy

> **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` → `libnrt.so.1` → `libnrt.so.2.31.24.0` · **Version** `2.31.24.0-0b044f4ce` (`.nrt_brazil_version` @`0xad41f0` = `"2.31.24.0"`) · **BuildID[sha1]** `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` · ELF64 LSB **DYN** x86-64, NOT stripped, DWARF v4.
>
> **Part II — Binary Anatomy & Forensics** / REFERENCE taxonomy · **Evidence grade:** every count below is re-derived from this exact binary — the IDA `…_data_tables.json` (222 records) and `…_switches.json` (460 records) sidecars bucketed by `.section` and by `func`-name provenance prefix; `nm -C --defined-only` for the `::_table_`/`tdrv_arch_ops`/`kaena_khal`/`ucode_func_symbols` symbol census; and `objdump -d`/`-s` at the cited addresses for the jump-table shapes. · [back to forensics index](overview.md)

## Abstract

This page is the **map of how `libnrt.so` decides where to jump** — the taxonomy of its dispatch surface, which is two coexisting populations: **222 data tables** (arrays of pointers or constants that a call site indexes) and **460 compiler-emitted switch statements** (range-checked `jmp *jumptable(,%idx,4)` constructs). The two are not the same mechanism. A data table is a *standing* structure in `.rodata`/`.data.rel.ro`/`.data` — a vtable, a seed array, a protobuf parse descriptor — that outlives any one call. A switch is *code*: a `cmp`/`ja`/`movslq`/`jmp *%rax` sequence whose jump-offset array the compiler spilled to `.rodata`, reachable only from the one function that owns it. Both resolve a key to a target; the value of separating them is that a reimplementer rebuilds the data tables as data and regenerates the switches as control flow.

The dispatch space classifies cleanly along three axes: **kind** (per-arch ops vtable, opcode/status jump table, protobuf TcParse table, dlsym binding table, error-code table, const lookup), **key type** (the device arch enum, an 8-bit ISA opcode, a status/algorithm/instance enum, a field number), and **backing section** (the **181** RELRO function-pointer tables vs the **37** `.rodata` const arrays vs the **4** `.data` third-party ifunc/AtomicHook tables — and, for switches, the `.rodata` jump-offset spill arrays). The most-referenced families are few: the per-arch HAL (`kaena_khal` ~245 slots, `tdrv_arch_ops` 488 B / 61 slots), the four 256-case ISA-opcode validators, the protobuf TcParse fast-path array, the 11-entry collective-algorithm jump table, and the status stringifier. This page leads with the dimension table that describes the whole space, then details only those central families with a real address and an annotated jump shape each; it does **not** dump 222 table rows or 460 switch rows — the dimension table is the value.

For reimplementation, the contract this page establishes is:

- **The two mechanisms, kept distinct** — 222 standing data tables (rebuild as data, bound per-arch by `register_funcs_v{2,3,4}` / `tdrv_arch_register_*`) versus 460 switches (regenerate as control flow; any optimizing compiler emits equivalent jump tables). A port that conflates them will hand-roll vtables the compiler would have spilled, or vice versa.
- **The key→target rule per family** — arch-enum→register-func selector, opcode-byte→handler (with deliberate default-fallthrough), status-enum→string, field-number→parse-thunk. Each is a `cmp upper-bound; ja default; indexed-jump` shape with the index measured from a named base.
- **The provenance split of the switches** — only **122** of the 460 are first-party NRT/HAL/ISA logic; **338** belong to statically-vendored crates (protobuf 144, gimli 87, std 30, …). A reimplementation links those, not reverses them.

| | |
|---|---|
| **Data tables (`…_data_tables.json`)** | **222** = 181 `.data.rel.ro` + 37 `.rodata` + 4 `.data` (re-derived; see [§ counts](#1-the-dispatch-dimension-table)) |
| **Switch statements (`…_switches.json`)** | **460** records over **323** distinct functions; **9,150** total cases; first-party **122** / vendored **338** |
| **Per-arch HAL vtable** | `kaena_khal` @`0xcaeb80` (`.bss`, ~245 qword slots) — bound by `kaena_khal_init` @`0x462290` |
| **TDRV arch-ops table** | `tdrv_arch_ops` @`0xc97180` (`.bss`, 488 B / 61 slots) → `tdrv_arch_ops_initialized` @`0xc97368` — bound by `tdrv_arch_ops_init` @`0x308e80` |
| **ISA opcode dispatch** | 4 × 256-case (`enum_variant_string_opcode` @`0x3abda0`, `debug_invalid_neuron_isa_instruction{,_0}` @`0x2ecbb0`/`0x39fec0`, `…engine_instruction` @`0x439ab0`) |
| **Collective-algorithm jump table** | `enc_get_algorithm_name` @`0xfef30` — 11-case, `int32[11]` offset array @`0x857a00` |
| **protobuf TcParse** | **71** `::_table_` `TcParseTableBase` objects (38 `ntff::`); fast-path fn-ptr array @`0xc04168` (110 entries) |
| **dlsym binding table** | `ucode_func_symbols` @`0xbf2ea0` (`.data.rel.ro`, 30 × {`void**`,`char*`}) |
| **Status error-code table** | `nrt_get_status_as_str` @`0xb95c0` — 3 switches (102 + 6 + 4 cases) |

> **NOTE — "table" and "switch" overlap but are not redundant.** A few constructs appear in *both* sidecars: the 11-entry collective dispatch is a *switch* (`enc_get_algorithm_name`) whose spilled `int32[11]` offset array is, structurally, a `.rodata` *table* at `0x857a00`. The convention this page uses: a construct is a **switch** if it is reached only through its owning function's `cmp/ja/jmp *` prologue, and a **data table** if a call site loads a pointer *out* of it and indirect-calls that pointer. The 222/460 figures count the two sidecars independently; they are not additive into a single "dispatch count".

---

## 1. The dispatch-dimension table

This is the centerpiece. Rather than list 222 tables and 460 switches, it describes the *shape* of the dispatch space: every kind of dispatch in `libnrt.so`, what key indexes it, which section backs it, how many instances exist, and which page owns the mechanism. Counts are re-derived (sidecar bucketing + `nm`); confidence is per-row. The "Count" column is instances of that kind, not slots within an instance.

| Dispatch kind | Key type | Backing section | Count | Owning page | Conf |
|---|---|---|---:|---|---|
| **Per-arch ops vtable** (`tdrv_arch_ops`, `kaena_khal`) | device arch enum {2 sunda, 3 cayman, 4 mariana} → register-func | `.bss` (slots), seeded from `.data.rel.ro` | 2 singletons | [TDRV Arch-Ops](../runtime/tdrv-arch-ops.md) | HIGH |
| **Per-arch P2P/topology op array** (sunda/cayman/mariana) | member index (named slots) | `.data.rel.ro` | 3 arrays (60/42/42) | [Collectives Algorithm Taxonomy](../collectives/algorithm-taxonomy.md) | HIGH |
| **dlsym binding table** (`ucode_func_symbols`) | iteration order → `dlsym(name)` | `.data.rel.ro` | 1 (30 entries) | [ext-ISA Dispatch](../gpsimd/dispatch-tables.md) | HIGH |
| **ISA opcode → handler/string** (256-case) | 8-bit ISA opcode | `.text` switch + `.rodata` offset array | 4 switches | [ext-ISA Dispatch](../gpsimd/dispatch-tables.md) | HIGH |
| **Pseudo-instr opcode → emit routine** | signed opcode (grouped) | `.text` switch + `.rodata` | 1 (90-case) | [ext-ISA Dispatch](../gpsimd/dispatch-tables.md) | HIGH |
| **Status/enum → string** (`nrt_get_status_as_str`, name stringifiers) | status / instance / family enum | `.text` switch + `.rodata` | ~7 switches | [Error/Status Codes](../runtime/error-codes.md) | HIGH |
| **Collective-algorithm jump table** | `enc_alg_type` enum 0–10 | `.text` switch + `.rodata` `int32[11]` @`0x857a00` | 1 (11-case) | [Collectives Algorithm Taxonomy](../collectives/algorithm-taxonomy.md) | HIGH |
| **HAL per-arch decode switch** (mirror trio) | engine/feature index | `.text` switch | ~30 switches | [TDRV Arch-Ops](../runtime/tdrv-arch-ops.md) | HIGH |
| **protobuf TcParse table** (`::_table_`) | wire field number → parse thunk | `.bss` (`TcParseTableBase`) + `.data.rel.ro` fast-path | 71 (+110-entry array) | [NTFF Format](../trace/ntff-format.md) | HIGH |
| **const / lookup table** (`data_type_sizes`, `CSWTCH.*`, nq-ids, coretype maps) | dtype/engine/queue enum | `.rodata` | ~37 in `.rodata` | [TDRV Arch-Ops](../runtime/tdrv-arch-ops.md) | HIGH–MED |
| **3rd-party ifunc / AtomicHook table** | CPU-feature / hook index | `.data` | 4 | [SBOM](vendored-sbom.md) | HIGH |
| **Vendored compiler switches** (gimli/protobuf/std/absl/…) | crate-internal | `.text` + `.rodata` | 338 switches | [SBOM](vendored-sbom.md) | HIGH |

The axes a reimplementer reads off this table: **key type** tells you the index expression (an arch enum is a 3-way selector; an 8-bit opcode is a 256-slot dense array with default-fallthrough; a wire field number is a protobuf-internal offset); **backing section** tells you where to place it (RELRO for function pointers that must be read-only after relocation; `.rodata` for immutable const arrays; `.bss` for the per-arch vtables that are *filled at bring-up*, empty in the file); and **count** tells you how much of each kind exists, so effort scopes to the 2 vtables + ~7 named switch families that carry first-party logic, not the 338 vendored switches.

> **GOTCHA — the per-arch vtables are empty in the file.** `kaena_khal` (`0xcaeb80`) and `tdrv_arch_ops` (`0xc97180`) are `.bss` symbols: zero bytes on disk. Their slot contents exist only as *stores* inside `kaena_khal_register_funcs_v{2,3,4}` and `tdrv_arch_register_{sunda,cayman,mariana}`, run lazily at device bring-up — **not** in `.init_array` ([Static-Init](static-init.md)). A reimplementer cannot hexdump these tables; they must be reconstructed from the register functions' decompiled stores, seeded from the `.data.rel.ro` source arrays at `0xbf3488`/`0xbf32c8`/`0xbf3108`. This is why they are absent from the RTTI `_ZTV` set ([RTTI Class Hierarchy](rtti-class-hierarchy.md)) — they are C function-pointer structs, not C++ vtables.

---

## 2. Per-arch ops vtables — `kaena_khal` and `tdrv_arch_ops`

The runtime's primary dispatch mechanism is a **pair of per-arch C function-pointer tables**, each a singleton selected once at bring-up by the device's architecture id. This is the C analogue of a C++ vtable, but hand-built: there is no `this`-pointer header and no RTTI; a call site references a named member directly (`tdrv_arch_ops.get_num_tpb(...)`), and the member was bound to the arch-specific implementation when `tdrv_arch_ops_init` ran. Think of it as the LLVM `TargetMachine` pattern — one indirection layer selecting silicon-specific code — except resolved by a `switch(arch)` over three register functions rather than virtual dispatch.

### The arch selector

Both tables are populated by the same 3-way arch switch. `tdrv_arch_ops_init` @`0x308e80` reads `al_hal_tpb_get_arch_type()` and routes; `kaena_khal_init` @`0x462290` takes the arch as an argument:

```c
// tdrv_arch_ops_init  @0x308e80
function tdrv_arch_ops_init():
    switch al_hal_tpb_get_arch_type():
        case 3: tdrv_arch_register_cayman()    // @0x30c7d0
        case 4: tdrv_arch_register_mariana()   // @0x30d900
        case 2: tdrv_arch_register_sunda()     // @0x30b6a0
        default: __assert_fail("0 && \"Unknown architecture\"",
                   ".../tdrv/tdrv_arch_type.c", 0x41, "tdrv_arch_ops_init")
    tdrv_arch_ops_initialized = 1              // guard @0xc97368

// kaena_khal_init  @0x462290 — arch arg, not queried
function kaena_khal_init(arch):
    kaena_khal[0] = arch                       // slot 0 = arch-type int
    if arch == 3: kaena_khal_register_funcs_v3(); return 0     // cayman @0x46ed70
    if arch == 4: kaena_khal_register_funcs_v4(); return 0     // mariana @0x4622e0
    if arch == 2: kaena_khal_register_funcs_v2(); return 0     // sunda  @0x468740
    return 0xFFFFFFFF                                          // unknown arch
```

### The two tables compared

`tdrv_arch_ops` is a *named* 488-byte struct (61 pointer-width slots, 47 top-level members + two nested op-structs); `kaena_khal` is a *flat* ~245-slot qword array with no recovered struct, bound member-by-member by the register function. The slot counts differ per arch because mariana is a feature superset.

| Table | Addr (`.bss`) | Shape | Slots bound (sunda/cayman/mariana) | Guard / selector | Conf |
|---|---|---|---|---|---|
| `tdrv_arch_ops` | `0xc97180` | 488-B named struct, 61 slots | all 47 members + nested 72-B `tpb_reg_offset` (+280) + 56-B `seq_refill` (+368) | `tdrv_arch_ops_initialized` @`0xc97368` | HIGH |
| `kaena_khal` | `0xcaeb80` | flat qword array, ~0x7a8 B | 229 / 230 / 244 slots set | slot 0 = arch int; no separate guard | HIGH |

`tdrv_arch_ops`'s 61 slots cover device geometry (`get_num_tpb`, `get_num_seng`, `get_num_topsp`), BAR/CSR offset getters, DMA-engine descriptors, the embedded-semaphore and event-accel ops, and two const-pointer slots (`+248 instr_block_caps`, `+472 sync_events`) that point at per-arch const structs in `.rodata`/`.data.rel.ro`. `kaena_khal`'s ~245 slots are the device HAL proper — `aws_hal_rdm_*`, `aws_hal_stpb_*`, `aws_hal_notific_*`, `aws_hal_intc_*`, `aws_hal_udma_*`, `al_mla_udma_*`, `aws_hal_sp_*` (TopSP) — bound from the source arrays the register functions walk. Both are owned in full by [TDRV Arch-Ops](../runtime/tdrv-arch-ops.md); this page records the dispatch *shape*, not the per-slot geometry.

> **QUIRK — the per-arch fan-out is triplicated, not parameterized.** `cayman`/`mariana`/`sunda` each get their own `register_funcs_v{3,4,2}` and `tdrv_arch_register_*`, binding `*_cayman`/`*_mariana`/`*_sunda` function variants. A reimplementation that factors the three arches into one templated builder will not match the call graph or the three distinct register paths. The arch→register mapping is **not** monotonic in the version suffix: arch 2 (sunda) → `v2`, arch 3 (cayman) → `v3`, arch 4 (mariana) → `v4` for `kaena_khal`, but `tdrv_arch_ops_init` lists them in source order `{3, 4, 2}`, not `{2, 3, 4}`. Drive off the arch enum, not the suffix.

---

## 3. The collective-algorithm 11-entry jump table

`enc_get_algorithm_name` @`0xfef30` is the canonical *dense jump table*: an `enc_alg_type` enum in `0..10` selects one of 11 algorithm-name strings (the collective primitive: all-reduce, reduce-scatter, all-gather, …). It is the cleanest small example of the compiler's switch-to-jumptable lowering, and its spilled offset array is a real `.rodata` table.

```asm
; enc_get_algorithm_name @0xfef30  (11-case enc_alg_type -> name string)
fef30:  cmp    $0xa,%edi                  ; upper bound = 10 (11 cases, 0..10)
fef33:  ja     0xfefd0                    ; default -> "unknown" (the switch default)
fef39:  lea    0x758ac0(%rip),%rdx        ; %rdx = jump-offset array @0x857a00 (.rodata)
fef42:  movslq (%rdx,%rdi,4),%rax         ; rax = sign-extended int32[enum] (relative offset)
fef49:  jmp    *%rax                       ; computed-goto into the per-case lea block
```

The offset array at **`0x857a00`** is `int32[11]` — each entry a *relative* displacement the `movslq`/`jmp *%rax` resolves into a per-case `lea name(%rip),%rax` block. This is the GCC "jump-table-of-relative-offsets" idiom: the table holds 32-bit signed deltas, not absolute pointers, so it relocates as pure `.rodata` with no dynamic relocation. A reimplementer reproduces this as an ordinary `switch` or an array of 11 string pointers; the *only* thing to preserve is the enum→string mapping and the default that catches out-of-range values.

> **NOTE — the 11-case sibling is the policy gate, not this stringifier.** `enc_cc_algorithm_allowed` @`0x108d30` is the *decision* function (does this arch/comm-type/replica-group permit algorithm N); `enc_get_algorithm_name` @`0xfef30` is only the *name* lookup. Both are 11-keyed on `enc_alg_type`, but the reimplementation-relevant logic — which algorithm is selected for a given collective op — lives in the `allowed`/composer path ([Collectives Algorithm Taxonomy](../collectives/algorithm-taxonomy.md)), not in the name table. The name table is a diagnostic affordance.

---

## 4. ISA opcode and status jump tables

The largest switches by case count are the **four 256-case ISA-opcode dispatchers** and the **status stringifier**. These are the dense-key end of the spectrum: an 8-bit opcode or a status enum indexes directly, and most slots fall through to a shared default.

### 4.1 The 256-case opcode validators

| Function | Addr | Cases | Role | Conf |
|---|---|---:|---|---|
| `enum_variant_string_opcode` | `0x3abda0` (`.constprop.0`) | 256 | opcode byte → ISA mnemonic string | HIGH |
| `debug_invalid_neuron_isa_instruction` | `0x2ecbb0` | 256 | opcode → per-op ISA-validity check | HIGH |
| `debug_invalid_neuron_isa_instruction_0` | `0x39fec0` | 256 | arch-variant of the above | HIGH |
| `debug_invalid_neuron_engine_instruction` | `0x439ab0` | 256 | opcode → per-engine validity check | HIGH |

These are 256-slot dense arrays indexed by the raw opcode byte. The defining property is **deliberate default-fallthrough**: only valid opcodes have distinct targets; the majority of the 256 slots point at a single "invalid instruction" handler. A reimplementation must reproduce *which* opcodes are valid (the sparse set with distinct targets), not all 256 — but it must keep the dense 256-slot table shape, because the dispatch is `jmp *table(,%opcode,8)` with no sparse-key compare. The full opcode→mnemonic decode is owned by [ext-ISA Dispatch](../gpsimd/dispatch-tables.md).

### 4.2 The status / name stringifiers

`nrt_get_status_as_str` @`0xb95c0` is the error-code table, and it is **three switches in one function** — a banded dispatch over non-contiguous status-code ranges:

```asm
; nrt_get_status_as_str @0xb95c0  (102 + 6 + 4 cases over 3 bands)
b95c0:  cmp    $0x65,%edi                  ; band 1: codes 0..101 (102 cases)
b95c3:  ja     0xb95f0                     ;   -> try band 2
b95c5:  lea    0x79dbb8(%rip),%rdx         ;   jump array @0x857184 (.rodata)
b95d5:  jmp    *%rax
b95e0:  lea    0x7857d6(%rip),%rax         ; shared "unknown" string @0x83edbd
b95f0:  cmp    $0x4b6,%edi                 ; band 2: codes around 0x4af..0x4b6
b95f6:  ja     0xb95e0                     ;   out of range -> shared default
b95f8:  cmp    $0x4af,%edi                 ; band 3: codes around 0x12xx
```

The three bands (main 102-case, a 6-case `0x1201..0x1206` band, a 4-case `0x1003..0x1006` band) reflect a status enum whose numeric values are *not* contiguous — the runtime packs distinct subsystem error ranges into one enum, and the compiler emitted a separate range-checked jump table per band. The same banded shape recurs in the other enum stringifiers: `nrt_instance_type_name` @`0x5cae70` (16+9), `nrt_get_arch_name_from_family` @`0x5cae00` (16), `nrt_core_dump_format_string` @`0x924f0` (18), `translate_one_pseudo_instr_v2` @`0x273ff0` (90, signed grouped opcodes). The status enum→string mapping is owned by [Error/Status Codes](../runtime/error-codes.md).

> **GOTCHA — a "switch" can be several jump tables.** `nrt_get_status_as_str` is **one function** but **three** records in `switches.json` (102/6/4 cases), because each non-contiguous band compiles to its own `cmp upper; ja; jmp *array` table. The 460-switch total counts *tables*, not functions — the 460 records span only 323 distinct functions. A reimplementation that writes one `switch(code)` over the full enum is behaviorally correct but will not byte-match the three-table layout; that divergence is expected, not a bug.

---

## 5. protobuf TcParse tables and the const lookup tail

Two kinds round out the taxonomy: the protobuf parse machinery (mechanical, regenerate) and the small const lookup arrays (first-party data, rebuild).

### 5.1 protobuf TcParse

The `.ntff`/`neuron_trace` trace schema is parsed by protobuf's table-driven `TcParser`. There are **71** `::_table_` `TcParseTableBase` objects (`nm -C`: data symbols ending `::_table_`), of which **38 are `ntff::`** — one per trace message class. Each is a field-number→parse-thunk descriptor consumed by the generated `_InternalParse`. The shared fast-path is a 110-entry function-pointer array at `0xc04168` (`.data.rel.ro`), whose entry 0 is `TcParser::FastV8S1` (the varint-field fast path). These are `protoc` output: a reimplementer regenerates them from the recovered `.proto`, never reverses them. The field-level schema is owned by [NTFF Format](../trace/ntff-format.md).

> **CORRECTION (F-DATATABLES TcParse count) —** the seed cell counted "69 `TcParseTableBase` _table_ objects". Re-deriving on `nm -C --defined-only | rg '::_table_$'` gives **71** (`ntff::`=38 confirmed exactly; the +2 are non-`ntff` protobuf-internal/`pb::CppFeatures` tables the seed's `ntff`-weighted sweep under-counted). The `ntff`=38 sub-figure is unchanged; only the grand total moves 69→71.

### 5.2 The const lookup tail

The 37 `.rodata` tables are immutable const arrays, not function pointers. The reimplementation-relevant ones:

| Table | Addr | Shape | Indexed by | Conf |
|---|---|---|---|---|
| `data_type_sizes.0` | `0x9d6e60` | `u32[16]` `[0,8,1,1,2,2,2,2,4,4,4,4,8,1,1,1]` | NRT dtype enum → byte size | HIGH |
| `sunda_nq_ids_*` | `0x9dc610` | `u32` block | notification-queue id map | HIGH |
| `CSWTCH.16{,_0,_1}` | `0x9de3e0`/`0x9de900`/`0x9defe0` | `u32[5]` per arch | engine-type → core-type | HIGH |
| `ucode_func_symbols` | `0xbf2ea0` | 30 × {`void**`,`char*`} | dlsym order → libnrtucode symbol | HIGH |

`ucode_func_symbols` (in `.data.rel.ro`, not `.rodata`) is the **dlsym binding table**: a 30-entry array of `{slot, name}` pairs the loader walks, calling `dlsym(ucode_lib_handle, name)` and storing the result into `*slot`. The raw bytes confirm it — slot `0xc96b30` ← name `0x844350` (`"nrtucode_get_api_level"`), slot `0xc96b20` ← name `0x844367` (`"nrtucode_get_git_version"`). It binds the entire `libnrtucode_extisa.so` dispatch surface; owned by [ext-ISA Dispatch](../gpsimd/dispatch-tables.md).

> **CORRECTION (F-DATATABLES switch provenance) —** the seed cell split the 460 switches "288 NRT/HAL/encoder/ISA logic; 172 statically-linked". Re-bucketing every `switches.json` record on its `func`-name provenance prefix gives **122 first-party / 338 vendored** — the seed's "288 NRT" was inflated because it folded the **144 protobuf** TcParser/wire-format switches (and other generated/templated dispatch) into the first-party count. The vendored 338 partitions as protobuf 144, gimli 87, std 30, absl 24, serde 20, core 12, libarchive 11, addr2line 9, misc-3P 1. The **total 460 is unchanged**; only the first-party/vendored boundary is corrected — and it is decisive: only ~27% of the switch surface is reimplementation-relevant.

> **QUIRK — IDA mislabels 37 `.rodata` const tables as fn-ptr tables.** `data_tables.json` tags entries in the `.rodata` const blocks with `target_func` names (e.g. `enc_stream_map_set_stream_id` at `0xa129e0`), but the raw bytes are repeated constants — `0xa129e0` is eight qwords of `0x100000` (= 1 MiB, a default size/stream-id const), not a pointer to any function. Treat the `enc_*`/`nccl_*` `target_func` labels on `.rodata` (and the 4 `.data` ifunc/AtomicHook) tables as **unreliable**; only the 181 `.data.rel.ro` tables carry genuine RELRO function pointers. A reimplementer who follows the IDA labels will wire const data as a call target.

---

## Related Components

| Component | Relationship |
|---|---|
| `kaena_khal` / `tdrv_arch_ops` | the two per-arch C function-pointer dispatch tables this page taxonomizes; geometry owned by [TDRV Arch-Ops](../runtime/tdrv-arch-ops.md) |
| `register_funcs_v{2,3,4}` / `tdrv_arch_register_*` | the bring-up paths that fill the `.bss` vtables — **not** in `.init_array` ([Static-Init](static-init.md)) |
| `enc_get_algorithm_name` / `enc_cc_algorithm_allowed` | the 11-key collective dispatch (name vs policy); composer logic in [Collectives Algorithm Taxonomy](../collectives/algorithm-taxonomy.md) |
| `::_table_` TcParse objects | the 71 protobuf parse tables; `protoc`-generated, schema owned by [NTFF Format](../trace/ntff-format.md) |
| `_ZTV` C++ vtables | the *other* dispatch mechanism — RTTI-headed, counted separately by [RTTI Class Hierarchy](rtti-class-hierarchy.md); the C tables here are **not** in the `_ZTV` set |

## Cross-References

- [C++ Class Hierarchy and RTTI](rtti-class-hierarchy.md) — the `_ZTV` C++ vtable dispatch (vptr = `_ZTV`+0x10) that is distinct from the C function-pointer tables here; the two dispatch worlds, kept separate
- [TDRV Arch-Ops](../runtime/tdrv-arch-ops.md) — the per-slot geometry of `tdrv_arch_ops` (61 slots) and `kaena_khal` (~245 slots), the per-arch register paths, and the const `instr_block_caps`/`sync_events` they point at
- [Collectives Algorithm Taxonomy](../collectives/algorithm-taxonomy.md) — the `enc_alg_type` enum behind the 11-entry jump table and the per-arch P2P/topology op arrays (60/42/42)
- [ext-ISA Dispatch Tables](../gpsimd/dispatch-tables.md) — the 256-case ISA-opcode dispatchers and the `ucode_func_symbols` dlsym binding table that reaches `libnrtucode_extisa.so`
- [Static-Init Pipeline](static-init.md) — why the per-arch vtables are empty in the file (installed lazily at `nrt_init`, not by the 77 `.init_array` ctors)
- [Overview and Heavy-Frame Census](overview.md) — the first-party/vendored byte split that the 122/338 switch-provenance partition mirrors
