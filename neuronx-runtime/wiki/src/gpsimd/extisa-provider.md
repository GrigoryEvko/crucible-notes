# The ext-ISA Provider (`nrtucode_*` C-API)

> *All host-binary addresses on this page apply to `libnrtucode_extisa.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`, ELF64 x86-64 DYN, stripped, 9,656,488 B; `.rodata` VMA == file offset, so every `0x…` host offset is also an analysis VMA). Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — the 52 exports (`nm -D`, twice), the API-level-3 gate, the four handle structs, the device mailbox map, the claim/boot magics, and the `0x40`-byte load record are each re-derived from the objdump/xxd of the cited functions, cross-checked against the IDA Hex-Rays decompile. · Part XI — GPSIMD / Q7 Microcode & ISA · [back to index](../index.md)*

## Abstract

`libnrtucode_extisa.so` is the host-side **microcode provider** for the Neuron GPSIMD "Q7" pool engine — a self-contained library that *ships device code* and hands it to the runtime through a stable C-API. It is not a driver and never touches the device: it embeds the device programs (13 Tensilica Xtensa ELF32 microcode blobs plus one SUNDA opcode-manifest JSON — owned by [Q7 Blobs](q7-blobs.md)), an index/prelink/serialize layer over them (owned by [Dispatch Tables](dispatch-tables.md) and [Microcode Loader](microcode-loader.md)), and exposes that whole container behind **exactly 52 exported `nrtucode_*` functions** (`nm -D`, all type `T`, zero other exports; imports are libc only — *no* `dlopen`, *no* Neuron deps). Think of it the way a JIT ships a code cache as a versioned `.so`: the engine is fixed silicon, the *programs* are the cataloged pool kernels, and this library is the container + a stable ABI that selects the right blob for the running silicon and the requested opcode set.

This page owns the **API surface and the data model** that surface threads. The provider is a small object graph: a `context` (the host↔device accessor + scratch), a per-engine `core` (the bring-up handle that owns the device CSR mailbox), an `ll` low-level loader (one device push record per load/unload), and an `opset` (the compiler-side opcode-set builder). The single most important contract is the **API-level-3 handshake**: `nrtucode_get_api_level()` returns the constant `3` (`0xaec0`: `mov $0x3,%eax; ret`), and `nrtucode_context_create` rejects any caller whose declared level `≠ 3` with status `4 VERSION`. The consumer — `libnrt.so`'s `tdrv/ucode.c` ([ucode-facade](ucode-facade.md)) — `dlopen`s this library, `dlsym`s a fixed 30-entry subset of the 52, re-checks the level, then drives `init → bring-up → load → query → teardown`. The other 22 exports (the `opset_*` family, the `_private_*`/getter extras) serve the compiler/assembler/disassembler.

The page is four structural units: the **export catalog** (§1) grouped by the five families (`get`/`context`/`core`/`ll`/`opset` = 7/5/22/9/9 = 52), each pinned to symbol+addr; the **handle objects + device mailbox map** (§2) — the four struct layouts and the `core->a4` CSR region the bring-up path drives; a **worked export** (§3) — the claim/boot handshake `nrtucode_core_on_ucode_booted` as annotated pseudocode, plus the `0x40`-byte load record `nrtucode_ll_get_load_sequence` emits; and the **boundary** (§4) — what this page does *not* re-derive (the RELA relocator + Xtensa encoder are [microcode-loader](microcode-loader.md); the blob inventory is [q7-blobs](q7-blobs.md); the arch-id routing is [dispatch-tables](dispatch-tables.md)).

For reimplementation, the contract is:

- **The 52-export surface, grouped and addressed** — five families, exactly 52 `nrtucode_*` symbols and nothing else; the 30 the runtime `dlsym`s vs the 22 compiler-side extras.
- **The API-level-3 gate** — `get_api_level → 3`; `context_create` rejects `api_level ≠ 3` with `4 VERSION`; the consumer re-checks at `dlopen`.
- **The four handle objects** — `context` (`0x28`), `core` (`0x70`), `ll` (`0x48`), `opset` (`0x830`) — their sizes, key field offsets, and constructors.
- **The device CSR mailbox map** at `core->a4`, driven through a caller-supplied `rw_impl` vtable: claim/boot magic, log ring, DGE priority map, pc-bounds — and the claim handshake magics.
- **The `0x40`-byte load/unload push record** (`tag 0x1095`) emitted into a caller buffer, the one observable runtime output of the loader.

| | |
|---|---|
| **Provider library** | `libnrtucode_extisa.so` (build-id `7bb03bc4…`, 9,656,488 B, stripped) |
| **Exports** | exactly **52** `nrtucode_*` (`get`×7, `context`×5, `core`×22, `ll`×9, `opset`×9); zero other exports |
| **Imports** | libc only (`malloc`/`calloc`/`realloc`/`free`/`memcpy`/`memset`/`fprintf`/`getenv`/`strcmp`/`strncpy`/…) — no `dlopen`, no Neuron deps |
| **API contract version** | `3` (`get_api_level` @`0xaec0`; `context_create` gate @`0xab36`; libnrt re-checks at dlopen) |
| **Build / git** | `get_build_version` → `"1.21.1.0"` (@`0xaed0`); `get_git_version` → `"6db9edc0…e7417e5"` (@`0xaee0`) |
| **Runtime consumer** | `libnrt.so` `tdrv/ucode.c` — `dlsym`s **30 of 52** into fn-ptr globals |
| **Handle objects** | `context` `0x28` · `core` `0x70` · `ll` `0x48` · `opset` `0x830` |
| **Claim / unclaim magics** | `CLAIMED = 0x502B2DA1`; `UNCLAIMED = 0x6099CB34` (**see §3 CORRECTION**) |
| **Load record** | one `0x40`-byte "write block" record, `tag 0x1095`, emitted by `sub_9400` |

---

## 1. The Export Catalog — 52 `nrtucode_*` by Role

### Purpose

The provider's entire ABI is exactly 52 `nrtucode_*` functions (`nm -D --defined-only`, type `T`, confirmed twice) and nothing else — no C++ symbols, no internal helpers exported, no Neuron-runtime entry points. They partition cleanly into five families by role. A reimplementer reproduces this surface family-by-family; the table below is the symbol+addr anchor for each, grouped by role with the runtime/compiler split called out. The per-export *semantics* that need more than a line — the claim handshake and the load record — are §3; the loader internals each `ll_*` drives are [microcode-loader](microcode-loader.md).

### Family map

| Family | Count | Role | dlsym'd by libnrt |
|---|---|---|---|
| `get_` | 7 | Provider query + image/ext-isa fetch | 6 of 7 |
| `context_` | 5 | Context lifecycle (accessor + scratch + userdata) | 5 of 5 |
| `core_` | 22 | Per-engine bring-up, log ring, DGE, pc-bounds | 13 of 22 |
| `ll_` | 9 | Low-level loader / device push stream | 6 of 9 |
| `opset_` | 9 | Opcode-set build + query (compiler/asm only) | **0 of 9** |

### `get_` family (7) — provider query and image fetch

| Export | Addr | Role / return | Confidence |
|---|---|---|---|
| `nrtucode_get_api_level` | `0xaec0` | → `3` (`mov $0x3,%eax; ret`) — the contract constant | HIGH |
| `nrtucode_get_build_version` | `0xaed0` | → `"1.21.1.0"` | HIGH |
| `nrtucode_get_git_version` | `0xaee0` | → `"6db9edc0…e7417e5"` | HIGH |
| `nrtucode_get_ext_isa` | `0x87a0` | thin wrapper `xor %ecx,%ecx; jmp sub_8660` — fills `extkernel{body,size,json,json_size}` | HIGH |
| `nrtucode_get_num_ext_isa_libs` | `0x87b0` | `coretype-6` jump table @`0x920e78`: `6→1`, `{13,21,29}→4`, else status `1` | HIGH |
| `nrtucode_get_memory_image` | `0x8490` | fetch `{IRAM,DRAM,SRAM,EXTRAM}` image by `(coretype, image_t, flavor)`; flavor from `NEURON_UCODE_FLAVOR` env when `0` | HIGH |
| `nrtucode_get_hwdecode_table` | `0x87f0` | HW-decode payload getter (`coretype-`indexed table @`unk_934BD0`); compiler-side, not `dlsym`'d | HIGH |

### `context_` family (5) — context lifecycle

| Export | Addr | Role | Confidence |
|---|---|---|---|
| `nrtucode_context_create` | `0xab10` | gate `api_level == 3` (else `4 VERSION`); `malloc 0x28` ctx + `malloc 0x200` scratch | HIGH |
| `nrtucode_context_destroy` | `0xac50` | `free(ctx->scratch)`; `free(ctx)` | HIGH |
| `nrtucode_context_set_memhandle_impl` | `0xac10` | `ctx[+0x08] = impl` | HIGH |
| `nrtucode_context_get_userdata` | `0xacb0` | → `ctx[+0x10]` | HIGH |
| `nrtucode_context_set_userdata` | `0xacf0` | `ctx[+0x10] = ud` | HIGH |

### `core_` family (22) — bring-up, logs, DGE, pc-bounds

| Export | Addr | Role | Confidence |
|---|---|---|---|
| `nrtucode_core_create` | `0x99e0` | `malloc 0x70`; init `{context, kind, a3, mailbox_base, a5, friendly_name}` | HIGH |
| `nrtucode_core_destroy` | `0x9b20` | disable logs; if booted, write `UNCLAIMED` to mailbox; `free(core)` | HIGH |
| `nrtucode_core_on_ucode_booted` | `0x9e50` | **claim handshake** (§3): read mailbox+0, expect `UNCLAIMED`, write `CLAIMED`, `boot_state=1` | HIGH |
| `nrtucode_core_get_context` | `0x9cf0` | → `core[+0x00]` | HIGH |
| `nrtucode_core_get_userdata` | `0x9d30` | → `core[+0x08]` | HIGH |
| `nrtucode_core_set_userdata` | `0x9d70` | `core[+0x08] = ud` | HIGH |
| `nrtucode_core_get_coretype` | `0x9db0` | → `*(u32*)(core+0x10)` (scheme B kind) | HIGH |
| `nrtucode_core_set_friendly_name` | `0x9df0` | `strncpy(core+0x48, name, 0x20)` | HIGH |
| `nrtucode_core_get_friendly_name` | `0x9e40` | → `core+0x48` | HIGH |
| `nrtucode_core_enable_logs` | `0x9f60` | `enable_logs_with_size_hint(core, 0x20000)` | HIGH |
| `nrtucode_core_enable_logs_with_size_hint` | `0x9f70` | rw-alloc device log ring; write `{bufsize, ptr, head}`; probe levels 4..0 | HIGH |
| `nrtucode_core_disable_logs` | `0x9bd0` | requires `boot_state==1` | HIGH |
| `nrtucode_core_set_max_loglevel` | `0xa110` | write `lvl` → mailbox+20 | HIGH |
| `nrtucode_core_print_logs` | `0xa1a0` | read `[tail,head)` from device log ring; emit per-record `(level, NUL-string)` | HIGH |
| `nrtucode_core_dge_set_priority_class_map` | `0xa3a0` | kind∈{2,9,17,25} & booted; `n≤4`; rw-write `classes` → mailbox+24 | HIGH |
| `nrtucode_core_dge_get_priority_class_map` | `0xa490` | same gate; rw-read mailbox+24 | HIGH |
| `nrtucode_core_get_dge_mailbox_addr` | `0xa580` | same gate; `idx≤4`; scrub `mailbox+40+4*j` with `0xFFFFFF00`; → `mailbox+40` | HIGH |
| `nrtucode_core_private_set_dge_mailbox` | `0xa720` | kind gate; `idx≤3`; rw-write `val` → `mailbox+40+4*idx` | HIGH |
| `nrtucode_core_private_get_dge_mailbox_values` | `0xa7a0` | kind gate; `idx≤3`; rw-read `mailbox+40+4*idx` | HIGH |
| `nrtucode_core_enable_pc_bounds_check` | `0xa860` | require `lo<hi`; write `lo`→mailbox+56, `hi`→mailbox+64 | HIGH |
| `nrtucode_core_disable_pc_bounds_check` | `0xa960` | write `0`→mailbox+56, `-1`→mailbox+64 | HIGH |
| `nrtucode_core_private_get_pc_bounds` | `0xaa10` | read mailbox+56→`*lo`, mailbox+64→`*hi` | HIGH |

### `ll_` family (9) — low-level loader

| Export | Addr | Role | Confidence |
|---|---|---|---|
| `nrtucode_ll_get_libraries_from_opcodes` | `0x8ce0` | opcode→lib map: `6→{0}`; `{13,21,29}→{3 if CPTC env else 0}`; `num_libs=1` | HIGH |
| `nrtucode_ll_create` | `0x8ef0` | `malloc 0x48`; fetch ext-isa (`sub_8660`); SUNDA minimal vs Cayman prelink (`<0x10000` cap → `7 ENOSPC`) | HIGH |
| `nrtucode_ll_destroy` | `0x91e0` | if `ll[+0x18]` rw-free `ll[+0x08]`; `free(ll)` | HIGH |
| `nrtucode_ll_get_load_sequence_num_instrs` | `0x9270` | assert `ll->ctx==core->ctx`; → `1` | HIGH |
| `nrtucode_ll_get_unload_sequence_num_instrs` | `0x9330` | same → `1` | HIGH |
| `nrtucode_ll_get_load_sequence` | `0x93f0` | `mov $1,%edx; jmp sub_9400` — emit ONE `0x40`-byte record (§3) | HIGH |
| `nrtucode_ll_get_unload_sequence` | `0x9880` | `mov $2,%edx; jmp sub_9400` — unload variant | HIGH |
| `nrtucode_ll_set_friendly_name` | `0x9890` | `strncpy(ll+0x20, name, 0x20)` | HIGH |
| `nrtucode_ll_get_library_size` | `0x98e0` | → `*(u64*)(ll+0x18)` | HIGH |

### `opset_` family (9) — opcode-set build + query (compiler/assembler)

| Export | Addr | Role | Confidence |
|---|---|---|---|
| `nrtucode_opset_create` | `0x8840` | `malloc 0x830`; 256 opcode slots zeroed | HIGH |
| `nrtucode_opset_destroy` | `0x8940` | free each non-null slot; `free(opset)` | HIGH |
| `nrtucode_opset_add_instruction` | `0x89e0` | `opcode=instr[0]`; opcode `0xF0` → specialization `instr[12]` into 256-byte bitmap | HIGH |
| `nrtucode_opset_private_get_num_opcodes` | `0x8b10` | SSE popcount of 256 non-null slot ptrs | HIGH |
| `nrtucode_opset_private_get_num_specializations` | `0x8bd0` | sum of one opcode's 256-byte bitmap | HIGH |
| `nrtucode_opset_private_has_opcode` | `0x8c50` | → `slot[opcode] != 0` | HIGH |
| `nrtucode_opset_private_has_specialization` | `0x8c70` | → `bitmap[spec]` | HIGH |
| `nrtucode_opset_set_friendly_name` | `0x8c90` | `strncpy(opset+0x808, name, 0x20)` | HIGH |
| `nrtucode_opset_get_library_index` | `0x8db0` | if any opcode present OR coretype∈{6,13,21,29} → `*lib_index=0`; else `1` | HIGH |

> **NOTE —** the `opset_*` family is the *producer-side* API: the compiler/assembler builds an opcode set (`opset_create → opset_add_instruction` per kernel instruction) and maps it to a microcode library (`opset_get_library_index`). The runtime never calls it — which is why the consumer's 30-entry `dlsym` table is a strict subset of the 52 exports. The 22 unused-by-runtime extras are the 9 `opset_*`, plus `get_hwdecode_table`, the `core_{get,set}_userdata`/`get_coretype`/`set_max_loglevel`/`enable_logs_with_size_hint`/`dge_get_priority_class_map`/`private_*` getters, and the `ll_get_library_size`/`get_{load,unload}_sequence_num_instrs` getters. CONFIDENCE: **HIGH** (each enumerated in the decompile). The runtime↔provider `dlsym` binding (which 30, in what order) is the [ucode.c facade](ucode-facade.md).

### Status and result codes

Every `nrtucode_*` that returns a result yields `nrtucode_result_t`:

| Code | Name | Reason |
|---|---|---|
| `0` | `SUCCESS` | — |
| `1` | `UNKNOWN_CORE` | coretype not in the recognised set |
| `2` | `UNKNOWN_IMAGE` | no flavor record matched in `get_memory_image` |
| `3` | `MISSING_IMAGE` | matched record but null getter |
| `4` | `VERSION` | `context_create` api-level `≠ 3` (the gate at `0xab36`) |
| `5` | `ALLOCATION` | `malloc`/rw-alloc failure |
| `6` | `IO` | rw read/write failure |
| `7` | `ENOSPC` | Cayman prelink library `> 0x10000` device cap |
| `8` | `INVALID` | null/misaligned/range guard; removed `NRTUCODE_MPLUS_ON_MARIANA` flag |
| `9` | `RELOCATION` | RELA relocation error ([microcode-loader](microcode-loader.md)) |

---

## 2. Handle Objects and the Device Mailbox

### Purpose

The 52 exports thread four opaque handles. A reimplementer must reproduce their sizes (the `malloc` constants are exact), the field offsets the API reads/writes, and — the part that actually reaches silicon — the device CSR mailbox the `core_*` bring-up path drives through a caller-supplied accessor. The provider itself performs *no* device I/O; every host↔device transaction goes through the `rw_impl` vtable installed at `context_create`.

### The four structs

```c
struct nrtucode_context_t {           // malloc 0x28 @ context_create 0xab10
    void*  rw_impl;          // +0x00  host↔device accessor vtable (see below)
    void*  memhandle_impl;   // +0x08  set via context_set_memhandle_impl
    void*  userdata;         // +0x10  get/set_userdata
    u64    scratch_capacity; // +0x18  = 0x200 (movq $0x200,0x18(%r15) @0xab81)
    void*  scratch;          // +0x20  = malloc(0x200)
};

struct nrtucode_core_t {              // malloc 0x70 @ core_create 0x99e0
    nrtucode_context_t* context;  // +0x00
    void*  userdata;              // +0x08
    u32    kind;                  // +0x10  NRTUCODE_CORE_*_NX_POOL, scheme B {2,9,17,25}
    u64    a3;                    // +0x18  pass-through (LOW: likely TPB-idx)
    u64    a4;                    // +0x20  device mailbox/CSR base ("mailbox" below)
    u64    a5;                    // +0x28  pass-through device handle (LOW)
    u32    boot_state;           // +0x30  0=unbooted, 1=BOOTED_LEGACY (after on_ucode_booted)
    u64    logbuf_dev_ptr;       // +0x38  0 until enable_logs
    u32    logbuf_size;          // +0x40
    u32    log_tail_read_idx;    // +0x44
    char   friendly_name[0x21];  // +0x48  "nrtucode_core_t@%p"
};

struct nrtucode_ll_t {                // malloc 0x48 @ ll_create 0x8ef0
    nrtucode_context_t* context;  // +0x00
    u64    dram_alloc;           // +0x08  device buffer (rw-freed at destroy if +0x18!=0)
    u32    iram_target;          // +0x10  device/IRAM target addr → record +0x18
    u64    library_size;         // +0x18  get_library_size → record +0x1c (low dword)
    char   friendly_name[0x21];  // +0x20  "nrtucode_ll_t@%p"
};

struct nrtucode_opset_t {             // malloc 0x830 @ opset_create 0x8840
    nrtucode_context_t* context;  // +0x000
    void*  opcode_slots[256];    // +0x008  slot[op]=ptr to 256-byte specialization bitmap | NULL
    char   friendly_name[0x21];  // +0x808  "nrtucode_opset_t@%p"
};
```

> **QUIRK —** the engine is named by **two distinct coretype numbering schemes** that index different machinery: scheme A "ext-isa coretype" `{6,13,21,29}` (bitmask `0x20202040`, used by `get_ext_isa`/`get_num_ext_isa_libs`/`ll_create`/`opset_get_library_index`) is a *function argument*; scheme B "`core->kind`" `NRTUCODE_CORE_*_NX_POOL = {2,9,17,25}` (bitmask `0x02020204`, validated by the DGE/pc-bounds gates) is stored at `core+0x10`. They are *not* the same number for the same silicon — `get_num_ext_isa_libs` does `add $0xfffffffa` (i.e. `coretype-6`) into a jump table, while the DGE gate masks `{2,9,17,25}`. Confusing them is the single easiest mistake here. The full `arch_id → coretype → lib-count` routing is [dispatch-tables](dispatch-tables.md). CONFIDENCE: **HIGH** (both bitmasks read from disasm).

### The `rw_impl` accessor vtable

`context_create`'s first non-null argument is the host↔device memory accessor. The provider invokes it by call-offset; the slot semantics are inferred from the call sites (the concrete implementation lives in `libnrt`'s tdrv, not here):

| Slot | Call site signature | Inferred role | Confidence |
|---|---|---|---|
| `[+0x00]` | `call *(%rax)` with `(handle, len, dst_ptr, dev_addr)` | device→host **read** | MED |
| `[+0x08]` | `call *0x8(%rax)` with `(handle, len, src_ptr, dev_addr)` | host→device **write** | MED |
| `[+0x10]` | `call *0x10(%rax)` | memmove/copy | MED |
| `[+0x18]` | `call *0x18(%rax)` | device **alloc**(size, flags) | MED |
| `[+0x20]` | `call *0x20(%rax)` | **map**/translate (`ll->dram_alloc → device ptr`) | MED |

The names are not in this binary; they are reconstructed from the read (`on_ucode_booted` @`0x9e7d`), write (`on_ucode_booted` @`0x9eeb`, `core_destroy` @`0x9b67`), alloc (log-ring), and map (load record) call patterns. CONFIDENCE: **MED** (call-offset pattern; concrete impl in `libnrt` — see [ucode-facade](ucode-facade.md)).

### The device CSR mailbox map

All offsets are from `core->a4 = *(u64*)(core+0x20)`, accessed only through `rw_impl`:

| Offset | Width | Field | Written/read by |
|---|---|---|---|
| `+0` | u32 | claim/boot magic (`UNCLAIMED=0x6099CB34` ↔ `CLAIMED=0x502B2DA1`) | `on_ucode_booted` (rmw), `core_destroy` (unclaim) |
| `+4` | u32 | log ring bufsize | `enable_logs_with_size_hint` |
| `+8` | u64 | log buffer device ptr | `enable_logs_with_size_hint` |
| `+16` | u32 | log head index | `enable_logs_with_size_hint`, `print_logs` (read) |
| `+20` | u32 | max loglevel | `set_max_loglevel`, `enable_logs` (probe) |
| `+24` | u32[5] | DGE priority-class map (`n≤4`) | `dge_{set,get}_priority_class_map` |
| `+40` | u32[5] | DGE mailbox slots (`idx≤4`) | `get_dge_mailbox_addr`, `private_{set,get}_dge_mailbox` |
| `+56` | u64 | pc-bounds SOC addr lo | `enable_pc_bounds_check`, `private_get_pc_bounds` |
| `+64` | u64 | pc-bounds SOC addr hi | `enable_pc_bounds_check`, `disable_pc_bounds_check` |

CONFIDENCE: **HIGH** — every offset cross-checked across the log/DGE/pc-bounds functions; the claim magic and its `+0` location are §3.

---

## 3. Worked Export — the Claim Handshake and the Load Record

### Purpose

The two exports a reimplementer most needs to get byte-exact are `nrtucode_core_on_ucode_booted` (the device-ownership handshake that gates every later `core_*` call) and `nrtucode_ll_get_load_sequence` (the one observable runtime output of the whole stack — a single device push record). Both are short, both are fully decoded, and both carry a correction against the earlier scaffold.

### `nrtucode_core_on_ucode_booted` — the claim handshake

### Entry Point

```text
nrtucode_core_on_ucode_booted (0x9e50)
  ├─ rw_impl->read (call *(%rax))        @0x9e7d   ── read claim magic from mailbox+0
  ├─ cmp $0x6099cb34   (UNCLAIMED)       @0x9e8e   ── fresh boot → proceed
  ├─ cmp $0x502b2da1   (CLAIMED)         @0x9e97   ── already claimed → log + status 8
  └─ rw_impl->write (call *0x8(%rax))    @0x9eeb   ── write CLAIMED; set boot_state=1
```

### Algorithm

```c
// nrtucode_core_on_ucode_booted(core)            // sub @0x9e50
function on_ucode_booted(core):
    if core == NULL:                               // @0x9e55
        fprintf(stderr, "nrtucode: invalid API usage in `%s`: `%s` is null"); abort()

    magic = 0
    rw = core->context->rw_impl                    // (*core)
    rw->read(rw, /*len=*/4, &magic, core->a4)      // call *(%rax)  read mailbox+0  @0x9e7d
    if rc != 0: return rc                           // IO

    if magic == 0x6099CB34:                         // UNCLAIMED  (cmp @0x9e8e)
        magic = 0x502B2DA1                          // CLAIM      (movl @0x9ecf)
        rw->write(rw, 4, &magic, core->a4)          // call *0x8(%rax)  write mailbox+0  @0x9eeb
        if rc != 0: return rc                       // IO
        core->boot_state = 1                        // BOOTED_LEGACY (movl $1,0x30 @0x9ef2)
        return 0                                     // SUCCESS

    if magic == 0x502B2DA1:                         // already CLAIMED by another core (@0x9e97)
        log("Core is claimed by another nrtucode_core_t instance")
        return 8                                     // INVALID
    // any other value = a foreign / incompatible booted image
    log("Magic value `%x` mismatch (booted image is incompatible)")   // @0x9f01
    return 8                                         // INVALID
```

`core_destroy` (`0x9b20`) is the symmetric release: if `boot_state != 0` it writes the `UNCLAIMED` magic back to the same mailbox slot (`movl $0x6099cb34,0xc(%rsp)` @`0x9b4b`, then `rw->write(…, core->a4)` @`0x9b67`) before `free(core)`.

> **CORRECTION (UC-API) —** the earlier scaffold (`P1-UC-API §2/§3`, echoed by [overview](overview.md) §2) stated the **UNCLAIMED/unclaim magic is `0x60969274`**. The binary truth is **`0x6099CB34`**: `cmp $0x6099cb34,%r9d` @`0x9e8e` in `on_ucode_booted` and `movl $0x6099cb34,0xc(%rsp)` @`0x9b4b` in `core_destroy`, both byte-exact. `0x502B2DA1` (CLAIMED) is correct as documented. A reimplementer that boots the firmware with `0x60969274` as the "unclaimed" sentinel will fail the `cmp` at `0x9e8e` and every claim will fall through to the "booted image is incompatible" path. CONFIDENCE: **HIGH** (immediate read directly from disasm, two independent call sites).

> **CORRECTION (UC-API) —** the same overview §2 GOTCHA claims `core_destroy` writes the unclaim magic to **`mailbox+4`** (the log-bufsize slot), not `mailbox+0`. The disasm shows the rw-write address argument is `mov 0x20(%rbx),%rsi` = `core->a4` (mailbox+0) with **no `+4` displacement** (`0x9b56`); the claim *read* in `on_ucode_booted` uses the identical `mov 0x20(%rbx),%rsi` (`0x9e6c`). Both claim and unclaim target **mailbox+0**. CONFIDENCE: **HIGH** (address operand has no displacement in either call site).

### `nrtucode_ll_get_load_sequence` — the `0x40`-byte push record

### Entry Point

```text
nrtucode_ll_get_load_sequence  (0x93f0)   ── mov $1,%edx (seq_type=load) ; jmp 0x9400
nrtucode_ll_get_unload_sequence(0x9880)   ── mov $2,%edx (seq_type=unload); jmp 0x9400
  └─ sub_9400                              ── shared emitter; ONE 0x40-byte record
```

### Algorithm

Both sequence getters tail-jump into the shared emitter `sub_9400` with `seq_type` pre-loaded (`mov $0x1,%edx` @`0x93f6` for load, `mov $0x2,%edx` @`0x9886` for unload). The buffer must be 8-byte aligned (else `8 INVALID`). Exactly one record is emitted; the 8-slot unrolled body further into `sub_9400` is **dead code**, reached only on the `ll->context != core->context` error path.

```c
// sub_9400(ll, core, seq_type, instr_buf, a3, &num_pushed)
struct ucode_push_record {            // 0x40 bytes — byte-exact from sub_9400 disasm
    u16 tag;          // +0x00  = 0x1095   "write block" command (movw $0x1095 @0x9537)
    u8  pad[10];      // +0x02  = 0
    u8  seq_type;     // +0x0c  = 1 (load) | 2 (unload)
    u8  flag;         // +0x0d  = 0xFF     (movb $0xff,0xd(%rsi) @0x9566)
    u16 rsvd;         // +0x0e  = 0
    u64 payload_ptr;  // +0x10  = rw_impl->map(ctx, ll->dram_alloc) for load; 0 for unload
                      //           (mov %rax,0x10(%rsi) @0x9554)
    u32 iram_target;  // +0x18  = ll->iram_target  (mov 0x10(%r15),%edi → 0x18(%rsi) @0x9533/0x956a)
    u32 payload_len;  // +0x1c  = (u32)ll->library_size (mov 0x18(%r15),%ecx → 0x1c(%rsi) @0x9516/0x9558)
    u8  zero[32];     // +0x20  = 0
};
// *num_pushed = 1;  return 0;
```

This record, written through `rw_impl->write`, is what installs the relocated microcode body into device IRAM. The relocation that produces the body the record points at — and the `"UCPL "` device header that frames it — are [microcode-loader](microcode-loader.md). CONFIDENCE: **HIGH** (every field offset and source register read from the `sub_9400` disasm).

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `nrtucode_core_on_ucode_booted` | `0x9e50` | claim handshake (read/cmp/write/boot_state) | HIGH |
| `nrtucode_core_destroy` | `0x9b20` | symmetric unclaim (write `0x6099CB34` → mailbox+0) | HIGH |
| `nrtucode_ll_get_load_sequence` | `0x93f0` | `seq_type=1` shim → `sub_9400` | HIGH |
| `nrtucode_ll_get_unload_sequence` | `0x9880` | `seq_type=2` shim → `sub_9400` | HIGH |
| `sub_9400` | `0x9400` | shared `0x40`-byte record emitter | HIGH |

### Considerations

The claim handshake is read-modify-write, not a blind store: a second `core` on the same engine reads `CLAIMED` and bails with status `8`, and a foreign booted image (any magic that is neither sentinel) is rejected rather than silently overwritten. A reimplementer must keep the three-way branch — collapsing it to "write `CLAIMED` unconditionally" defeats the multi-instance and image-compatibility checks the firmware relies on. The load record's `payload_len` is read as a **32-bit** field from `ll->library_size` (`mov 0x18(%r15),%ecx`, a dword move), even though `library_size` is stored as a `u64`; a library over `0x10000` is already rejected at `ll_create` ([microcode-loader](microcode-loader.md)), so the high dword is always zero on the emit path.

---

## 4. Boundary — What This Page Does Not Own

### Purpose

The provider is a container with an index, a loader, and a blob store; this page owns only the **API surface and the data model**. Three adjacent concerns are owned by sibling pages, and a reimplementer should read this page for the *what* and those for the *how*.

### The boundary

> **NOTE —** the provider's three internal subsystems are each a sibling page; this page links them and does not re-derive their byte-level internals:
>
> - **The RELA relocation + Xtensa immediate encoding + `"UCPL "` device header** — everything `ll_create` invokes between blob-select and the staged image (`sub_8380`/`sub_74E0`/`sub_79E0`, the reloc-type taxonomy, the eight FLIX slot signatures, the geometry descriptor) — is owned by [The Microcode Loader](microcode-loader.md). This page stops at the `ll` handle's *exported* surface and the `0x40`-byte record it emits.
> - **The blob inventory** — the 13 embedded Xtensa ELF32 images (9 distinct), the SUNDA opcode manifest, and the on-device `kernel_info_table` opcode→entry format the firmware uses after the record installs the body — is owned by [The 13 Q7 Blobs](q7-blobs.md).
> - **The `arch_id → coretype → lib-count` routing** behind `get_ext_isa`/`get_num_ext_isa_libs`/`ll_get_libraries_from_opcodes` (the `arch_id-6` jump tables, the per-arch handler tables `T0..T3`, both coretype schemes in full) is owned by [Dispatch Tables](dispatch-tables.md).
>
> The runtime consumer that `dlopen`s this library and binds the 30-of-52 subset is [The `ucode.c` Facade](ucode-facade.md). CONFIDENCE: **CERTAIN** (each subsystem's owning cell is named in the Part-XI map).

---

## Related Components

| Name | Relationship |
|---|---|
| The Microcode Loader ([microcode-loader](microcode-loader.md)) | The RELA relocator + Xtensa encoder + UCPL header `ll_create` drives; owns the prelink internals |
| The 13 Q7 Blobs ([q7-blobs](q7-blobs.md)) | The embedded ELF32 images + SUNDA manifest this provider serves; the `kernel_info_table` format |
| Dispatch Tables ([dispatch-tables](dispatch-tables.md)) | The `arch_id → coretype → lib-count` routing and both coretype numbering schemes |
| The `ucode.c` Facade ([ucode-facade](ucode-facade.md)) | `libnrt`'s consumer: the 30-entry `dlsym` binding, api-level check, init/teardown |
| FW-IO MiscRAM Mailbox ([kernel](../kernel/fw-io.md)) | The device-side CSR region the `rw_impl` writes land in — the Q7 management path |

## Cross-References

- [Overview: the Q7 GPSIMD Engine](overview.md) — the provider→loader→device map and the 52-export role groups
- [The Microcode Loader (RELA + FLIX, UCPL)](microcode-loader.md) — the relocation/encoding internals behind `ll_create`, not re-derived here
- [ext-ISA Dispatch Tables and arch-id Scheme](dispatch-tables.md) — `arch_id → coretype → lib-count`; the two coretype schemes in full
- [The `ucode.c` dlopen Facade](ucode-facade.md) — the runtime's 30-of-52 `dlsym` binding and the api-level handshake
- [The 13 Q7 Microcode Blobs](q7-blobs.md) — the embedded images this provider's `get_*`/`ll_*` exports serve
- [back to index](../index.md)
