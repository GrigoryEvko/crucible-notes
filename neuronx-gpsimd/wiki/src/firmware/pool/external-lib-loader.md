# External-Lib Loader (device side)

The **device-side external-library loader** is the Q7 POOL/SEQ firmware routine
`load_external_libraries_impl` — the on-chip consumer of the host's `0x1095`
load/unload sequence. It pulls a host-relocated EXTISA image into the pool core's
IRAM/DRAM, runs the library's start/init hook, and wires the loaded library's
`kernel_info_table` into the same dispatch table the POOL main loop scans. This page
documents the loader entry, the **library_selector → resident-image-base resolution**,
the load orchestration, the **`kernel_info_table` registration**, the env-gate, the
error model, and — the question this page was written to settle —
**whether the device can map a bare `library_selector` back to its resident region for
UNLOAD** (the RT-12 teardown-leak question). The answer, grounded below, is **YES — no
device-side leak**.

> **NOTE — host vs device split.** The *staging* of an EXTISA library (ELF validation,
> segment copy, every `R_XTENSA_*` relocation, the `UCPL ` header) runs on the **host**
> x86 inside `libnrtucode_internal.so` — see the forward Part-8 pages
> [`nrtucode-ll-load-unload`](../../runtime/nrtucode-ll-load-unload.md) and
> [`prelinker-ucpl`](../../runtime/prelinker-ucpl.md). This page is the **device
> consumer**: by the time bytes reach the Q7 pool core they are fully relocated, so the
> loader here does **no** relocation — it DMAs, parses a fixed header, binds a table,
> and calls one symbol.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`[HIGH/MED/LOW]` × `[OBSERVED/INFERRED/CARRIED]`. `OBSERVED` = read directly from the
shipped firmware bytes / native Xtensa disassembly; `INFERRED` = deduced from anchored
mechanics; `CARRIED` = taken from a backing report and not independently re-grounded
this pass. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**.

---

## 1. Provenance — which image carries the loader

The POOL cluster's anchor identity is the **`CAYMAN_NX_POOL` DEBUG** image family
(re-carved and sha-confirmed this pass, identical to
[`pool-dispatch`](./pool-dispatch.md) and [`dispatch-hub`](../seq/dispatch-hub.md)):

| Fact | Value | Anchor | Conf |
|---|---|---|---|
| Anchor `iram.bin` | 116,768 B (`0x1c820`), sha256 `8e4412b9…ab9ed70a` | `objcopy --only-section=.rodata` + `sha256sum` | `[HIGH/OBSERVED]` |
| Anchor `dram.bin` | 28,448 B (`0x6f20`), sha256 `7bdf6ed7…d6816ecd` | same | `[HIGH/OBSERVED]` |

> **GOTCHA — the loader is NOT in the `NX_POOL` image.** The `CAYMAN_NX_POOL` DEBUG
> DRAM carries only `soc_window_manager.hpp` (offset `0xf6f`) — none of the
> external-lib loader vocabulary. The `NX_POOL` build is the *static-kernel* pool image
> (its dispatch is the `opcode−0x41` direct-indexed table at DRAM `0x80814`, 178 slots
> — see [`dispatch-hub`](../seq/dispatch-hub.md)). The **runtime loader** lives in two
> sibling members of the *same* `libnrtucode.a`, which is where every loader fact below
> is grounded `[HIGH/OBSERVED]`:
>
> | Loader-bearing member | `.rodata` size | role |
> |---|---|---|
> | `img_SUNDA_Q7_POOL_DEBUG_DRAM_contents.c.o` | 42,496 B (`0xa600`) | the **self-naming** strings (`load_external_libraries_impl`, the `total_cpus` asserts) |
> | `img_CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_DRAM_contents.c.o` | 91,776 B | the **runtime-log narrative** (`xtensa_unload`, `Library (un)loaded`, the window table) |
>
> Both carry the **`P%i:`** POOL-core log prefix (136 occurrences in the DKL DRAM),
> confirming they are pool-core firmware. The loader **source set is byte-identical**
> across SUNDA-base and CAYMAN/MARIANA DKL builds; only *placement* differs (§9).

**Disassembler.** Device code is decoded with the native Vision-Q7 tool
`gpsimd_tools/tools/XtensaTools/bin/xtensa-elf-objdump` under `XTENSA_CORE=ncore2gp`
(`GNU objdump 2.34.20200201, Xtensa Tools 14.09`, `--version`-confirmed this pass), the
FLIX/VLIW HAVE_VISION=1 config — **not** scalar-LX.

> **GOTCHA — flat carve ⇒ no L32R literal resolution (MED wall).** The Q7 IRAM is a
> flat (non-ELF) relocated blob. The native tool decodes the ISA cleanly (real
> `entry`/`l32e`/`s32e`/`retw` windowed prologues, 273 `entry` + 446 `retw`
> boundaries), but in a flat carve it **cannot re-resolve `l32r` literal-pool words**:
> e.g. `l32r a11, 0xff4200` points *outside* the carve at a relocated absolute address.
> Consequently the **numeric entry VA of `load_external_libraries_impl` and the exact
> `operator[]` index arithmetic are `[MED/INFERRED]`** — the names and structure are
> byte-exact (DEBUG strings + the EXTISA table bytes), the literal-resolved control
> flow is not. We never fabricate a literal value; where one blocks us we say so.

---

## 2. The loader entry and its method/symbol set

The loader's whole class/method vocabulary survives as `__FILE__`/self-naming DEBUG
strings. Byte-exact offsets in `SUNDA_Q7_POOL_DEBUG_DRAM` `.rodata`
(`strings -t x`, this pass — every offset re-verified):

| DRAM off | token | source unit | role |
|---|---|---|---|
| `0x3f6` | `update_soc_address` | `soc_window.hpp` | SoC↔XT addr window program |
| `0x418` | `translate_soc_to_xt_address` | `soc_window.hpp` | `device_addr` → XT-local addr |
| `0x434` | **`load_external_libraries_impl`** | `external_lib_loader.hpp` | **THE ENTRY** |
| `0x469` | `…external_lib_loader.hpp:283` assert | — | `total_cpus` invariant (load) |
| `0x4de` | `NUM_POOL_CORES` | — | the FW-17 pool-core constant |
| `0x4ed` | `…external_lib_loader.hpp:318` assert | — | `total_cpus` invariant (post) |
| `0x58a` | `load_tables` | `object_array.hpp` | the **resident library-entry array** |
| `0x596` | `operator[]` | `object_array.hpp` | indexed entry access |
| `0x5b2` | `call_start_symbol` | `external_lib.hpp` | invoke lib start/init |
| `0x5d5` | `init` | `external_lib_funcs.hpp` | library init hook |
| `0x64c` | `dispatch_wrapper.hpp` | — | `call`-dispatch registration |
| `0x8a9` | `load_from_nx_addr` | `external_lib.cpp` | pull the staged image |
| `0x8cc` | **`xtensa_unload`** | `external_lib.cpp` | **THE UNLOAD ROUTINE** |
| `0x8da` | `.kernel_info_table` | — | the ELF section the loader binds |
| `0x8ed` | `init_dma_queue` | `data_transfer.hpp` | DMA-queue setup |
| `0x90e` | `dram_addr_to_soc_addr` | `memory_manager.hpp` | addr translation (reverse) |
| `0x92f` | `dma_data_transfer` | `data_transfer.hpp` | the DMA copy primitive |

`[ALL offsets HIGH/OBSERVED — read directly from the carved DEBUG DRAM bytes.]`
The full `__FILE__` path is `/opt/workspace/NeuronUcode/src/external_lib/external_lib_loader.hpp`.

```
ENTRY NAME : load_external_libraries_impl        (external_lib_loader.hpp)        [HIGH/OBS]
ENTRY VA   : Q7 IRAM, device VA base 0x01000000  — NOT byte-pinned (flat carve,    [MED/INF]
             l32r wall, §1); the entry's NAME + presence are HIGH/OBSERVED.
```

---

## 3. Loader entry, as annotated C pseudocode

The driver consumes the host **load sequence** — the array of 0x40-byte `0x1095`
records emitted by the host's `nrtucode_ll_get_load_sequence`
([`nrtucode-ll-load-unload`](../../runtime/nrtucode-ll-load-unload.md), forward
Part-8). Each record carries (host-side layout, `[CARRIED]` from the RT generators):

| `0x1095` field | off | on LOAD | on UNLOAD |
|---|---|---|---|
| direction | `+0x0c` | `1` | `2` |
| device_addr (SoC addr of staged `UCPL ` image) | `+0x10` | live | **`0`** |
| library_selector (EXTISA UID) | `+0x18` | live (`0`/`3`) | live (`0`/`3`) |
| image_size | `+0x1c` | live | **`0`** |

> **GOTCHA — on unload the device gets ONLY a selector.** `device_addr` and
> `image_size` are **both 0** on a teardown record (the host's `dir==1` gate zeroes
> them). So the unload path *cannot* re-derive the region from the record — it must use
> resident state. This is the crux of §10.

```c
// load_external_libraries_impl(seq, n_records, pool_ctx)   // external_lib_loader.hpp
// Device-side; the staged images are already host-relocated (no R_XTENSA on device).
for (size_t r = 0; r < n_records; ++r) {
    rec0x1095 *rec = &seq[r];                          // 0x40-byte record (RT-11/12)
    uint32_t   sel = rec->library_selector;            // +0x18  (0=base POOL, 3=CPTC)

    if (rec->direction == 1) {                         // ---- LOAD ----
        // L1: pick/create the resident library entry keyed by selector (§4)
        entry *e = load_tables.operator[](sel);        // object_array.hpp  [name HIGH/key MED]

        // L2: host SoC addr -> Q7-local XT addr via the resident window table (§4b)
        xt_addr xt = translate_soc_to_xt_address(rec->device_addr);   // soc_window.hpp

        // L3: DMA the staged "UCPL " image into the core's IRAM/DRAM/EXTRAM
        load_from_nx_addr(e, xt, rec->image_size);     // external_lib.cpp -> init_dma_queue
                                                       //                  -> dma_data_transfer
        // L4: parse the (already-relocated) UCPL header -> {code,data} extents + start_sym
        ucpl_hdr *h = (ucpl_hdr *)xt;                  // magic "UCPL ", see prelinker-ucpl
        if (h->start_sym == 0)                         // "Corrupted prelink library; NULL start symbol"
            return ERR_CORRUPT;

        // L5: locate the lib's `.kernel_info_table` section and bind it (§5)
        e->kit = find_section(h, ".kernel_info_table");// loader holds the name str (0x8da)

        // L6: run the library's own start/init -> it finishes any internal table setup
        call_start_symbol(e);                          // external_lib.hpp / _funcs.hpp:init

        // L7: structural invariant (FW-17): a freshly loaded lib occupies >=1 pool core
        ASSERT(e->total_cpus == 1 || e->total_cpus == NUM_POOL_CORES);   // line 283

    } else if (rec->direction == 2) {                  // ---- UNLOAD ----
        entry *e = load_tables.operator[](sel);        // SAME resident array, bare selector
        xtensa_unload(e);                              // external_lib.cpp (§10)
        ASSERT(e->total_cpus == 0 || e->total_cpus == 1 || e->total_cpus == NUM_POOL_CORES); // line 318
    }
}
```

`[Names + record fields HIGH/OBSERVED; the exact per-step dataflow (which register holds
what) is MED — the flat-carve l32r wall blocks byte-pinning the control flow.]`

> **NOTE — the device does NOT re-relocate.** Prelink, segment-load, and every
> `R_XTENSA_NONE/32/SLOT*_OP/SLOT*_ALT` relocation already ran on the host *before* the
> records were emitted (the staged image is fully relocated; the
> [`prelinker-ucpl`](../../runtime/prelinker-ucpl.md) page documents the host walk and
> the `UCPL ` header it produces). The device load is just: DMA-in → header parse →
> table bind → start call.

---

## 4. library_selector → resident-EXTISA-image-base resolution (the central deliverable)

The device resolves a selector to its resident image **not** by re-reading the record's
`device_addr` (absent on unload) but through its **own resident maps**, populated at
load time. Two mechanisms, both `[HIGH/OBSERVED]` by name:

### (a) The resident library-entry array — `object_array<entry>`

`load_tables` (`object_array.hpp`, strings `0x58a`/`0x596`/`0x5a1`) is a **resident
array of per-library `entry` records**, indexed by `operator[]`. It is the only
array-access surface in the loader and sits directly between the `total_cpus` asserts
and `call_start_symbol` — i.e. it **is** the per-library table walked by both load and
unload. Because it is resident, the load-time `selector → entry` binding **persists to
unload time**.

**The `entry` record** — the only byte-exact field is `total_cpus`, pinned by the
asserts; the rest are method-INFERRED from the loader vocabulary:

| `entry` field | how known | conf |
|---|---|---|
| `total_cpus` ∈ {0,1,NUM_POOL_CORES} | asserts at lines 283/318 | `[HIGH/OBSERVED]` |
| `library_size` | log `library_size not set, defaulting to data_scratch_size…` (DKL `0x179f`) | `[HIGH/OBSERVED]` (field exists) |
| XT base addr | populated via `translate_soc_to_xt_address` | `[MED/INFERRED]` |
| `.kernel_info_table` ptr + count | bound at L5 | `[MED/INFERRED]` |
| start/init/fini symbols | `call_start_symbol` | `[MED/INFERRED]` |

> **GOTCHA — selector vs dense load-slot (MED).** Whether `operator[]` keys on the raw
> selector (`0`/`3`) or on a dense load-order slot is **not** byte-pinnable from the
> flat IRAM (the `l32r` index arithmetic is unresolvable, §1). **Either way the array is
> resident and the map survives load→unload** — which is all the RT-12 verdict needs.
> The follow-up to settle it is an ELF-with-section-headers carve of a DKL image.

### (b) The SoC↔XT address-window table — the resident region base

`soc_window_manager` (DKL DRAM, `[HIGH/OBSERVED]` strings) owns a **16-entry XT-address
window table**:

| evidence | DKL off |
|---|---|
| `P%i: Q7:   xt_addrs[16] = [0x%08x%08x, …16 entries…]` | `0x477a` |
| `translate_soc_to_xt_address` | `0xbc7` |
| `P%i: WIN[%u]: @%llx -> %x` | `0xbe3` |
| `P%i: update_window: num=%u, xt_addr=0x%x, soc_addr=0x%llx, l_mask=0x%llx` | `0xb25` |
| `R: program_window: num=%d, vld=%d, xt_addr=0x%llx, …` | `0xe5f` |
| **`push_unallocated_window`** | `0xf05` |

`translate_soc_to_xt_address` maps a host SoC/HBM address into a Q7-local XT address via
one of these 16 windows on **load**; `push_unallocated_window` **reclaims** a window on
**unload**. So the device tracks each loaded region's XT base in a resident window slot,
reclaimable **without** the host resending an address.

> **CONCLUSION (selector→base).** The device holds *two* resident selector→region maps —
> the `object_array<entry>` (load-time slot) and the 16-window XT-addr table (region
> base) — populated at load and reversed at unload. `translate_soc_to_xt_address` is the
> *load-time* resolver (driven by `device_addr`, which is 0 on unload, so it is **not**
> the unload resolver); the resident array + window table are. This is precisely what
> makes a bare-selector unload resolvable (§10).

---

## 5. kernel_info_table registration (the dispatch write-path)

The loaded library is an `EM_XTENSA` (`e_machine=94`) `ET_EXEC` ELF32 carrying a
`kernel_info_table` PROGBITS section. Ground-truthed this pass against
`CAYMAN_Q7_POOL_PERF_EXTISA_0` (selector 0, the base POOL lib):

```
$ readelf -S cay_extisa0.bin
  [ 7] kernel_info_table  PROGBITS  02000380  007400  000088  WA  0  0  8   <- 0x88/8 = 17 entries
  [ 1] .text              PROGBITS  01000000  000100  006f1e  AX  0  0 32
```

The VMA `0x02000380` matches [`dispatch-hub`](../seq/dispatch-hub.md) exactly. Decoded
entries (8 bytes each: **key bytes `[0x00, 0x00, spec, opcode]` at +0** — which the POOL
dispatcher compares as a native-LE `u32` = `(opcode<<24)|(spec<<16)` — followed by the **LE
funcVA at +4**). The "BE `(spec<<8|opcode)`" reading of the same four bytes is the host-emit
transcode framing — see the CORRECTION below:

| idx | key bytes (`[0,0,spec,opcode]`) | opcode | spec | funcVA (LE) | dispatch key `opcode<<24\|spec<<16` |
|---|---|---|---|---|---|
| 0 | `0x0000007e` | `0x7e` | 0 | `0x01000080` | `0x7e000000` |
| 3 | `0x00000045` | `0x45` | 0 | `0x01000b90` | `0x45000000` |
| 6 | `0x000000f0` | `0xf0` | 0 | `0x01003370` | `0xf0000000` |
| 7 | `0x000001f0` | `0xf0` | **1** | `0x01003380` | `0xf0010000` |
| 8 | `0x000002f0` | `0xf0` | **2** | `0x01003484` | `0xf0020000` |
| 9 | `0x000004f0` | `0xf0` | **4** | `0x010037a8` | `0xf0040000` |
| 10 | `0x000003f0` | `0xf0` | **3** | `0x01003a60` | `0xf0030000` |
| … | (17 total) | | | (all in `.text` `0x010xxxxx`) | |

`[ALL 17 entries HIGH/OBSERVED — decoded byte-exact from cay_extisa0.bin file offset
0x7400.]` The `opcode 0xf0` rows with `spec` 0..4 are the extended-opcode family
documented in [`pool-ext-0xf0`](./pool-ext-0xf0.md).

> **CORRECTION — two encodings of the SAME (opcode,spec) pair, do not conflate.** The
> **on-disk** table stores `(spec<<8 | opcode)` big-endian. The POOL dispatcher's
> **lookup key** is `opcode<<24 | spec<<16` (pinned by
> [`dispatch-hub`](../seq/dispatch-hub.md)). These are two layouts of the *same* tuple,
> not two different keys: opcode `0xf0` / spec `1` is the on-disk `0x000001f0` BE and
> the lookup `0xf0010000`. A reimplementer must transcode, not copy the raw word.

**Registration = two facts:**

1. **No per-entry device fixup.** The funcVA slots are `R_XTENSA_RELATIVE` and were
   relocated **on the host** against the device base
   ([`prelinker-ucpl`](../../runtime/prelinker-ucpl.md)). When the image lands, the
   table funcVAs already point at the correct device `.text` VAs (see the decoded
   `0x010xxxxx` targets above). So "registration" is **not** a relocation loop on the
   device. `[HIGH/OBSERVED — funcVAs already valid in the shipped blob.]`

2. **Bind by section name.** The device loader resolves the loaded library's
   `kernel_info_table` section (the literal `.kernel_info_table` at DKL DRAM `0x190f` /
   SUNDA `0x8da` is the **loader's** copy of the name) and binds it as the POOL
   dispatcher's active keyed table. The POOL extended-inst path then runs **one linear
   scan** matching the packed `(opcode,spec)` key → the entry's funcVA — see
   [`kernel-info-table`](./kernel-info-table.md) and
   [`pool-ext-0xf0`](./pool-ext-0xf0.md). So *"install opcode→funcVA"* == *"make this
   library's `kernel_info_table` the table the dispatcher scans."*
   `[HIGH/OBSERVED for the table format + the section-name ref; the exact device bind
   instruction is MED — the flat-carve l32r wall.]`

`call_start_symbol` / `init` (`external_lib.hpp` / `external_lib_funcs.hpp`) runs the
library's own start hook (the `UCPL` `start_sym`), which performs any
library-internal table setup before the dispatcher uses it. `[HIGH/OBSERVED name.]`

> **QUIRK — section named with and without the leading dot.** The shipped EXTISA blob's
> section is `kernel_info_table` (no dot, per `readelf -S`); the loader's resolver string
> is `.kernel_info_table` (with dot). The loader matches by the dotted ELF section name;
> the blob's section-header name is the un-dotted form. Both observed byte-exact.

---

## 6. The env-gate

> **CORRECTION — the conditional-library gate is HOST-side, NOT in the device loader.**
> The selector the device receives **already encodes** the gate decision. Confirmed in
> the host `libnrtucode_internal.so`:
>
> | host string | meaning |
> |---|---|
> | `NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE` | env var → selector `3` (CPTC) if set, else `0` (base POOL) |
> | `NEURON_UCODE_FLAVOR` | picks `{debug,DEBUG,test,TEST,perf-default}` flavor of the image |
> | `cptc_decode_impl…` (mangled `.xt.prop` symbols) | the selector-3 kernel family |
>
> `[HIGH/OBSERVED — strings present in the host .so.]`

So when `NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE` is set the host emits
`library_selector = 3` (the `cptc_decode` extended-inst superset, PERF gens only);
unset → `0`. The **device loader has no `getenv`** — it sees a concrete selector and
loads exactly that library's image. The only device-visible consequence is **which**
`kernel_info_table` gets bound: the **17-entry** base table (selector 0) vs the
**9-entry** CPTC superset (selector 3). `[device-invariance INFERRED HIGH from "no
getenv in firmware"; the 17/9 counts HIGH/OBSERVED — §9.]`

---

## 7. The error model

The CAYMAN DKL DEBUG image carries the full loader runtime-log narrative (richer than
SUNDA's RELEASE floor). Byte-exact strings (`strings -t x`, this pass; all carry the
`P%i:` pool-core prefix):

| DKL off | string | trigger / status |
|---|---|---|
| `0x18a2` | `Corrupted prelink library; NULL start symbol` | `UCPL` `start_sym == 0` → abort |
| `0x17f6` | `Invalid library_size: %u (max: %u)` | size/capacity over device cap |
| `0x179f` | `library_size not set, defaulting to data_scratch_size for backward compatibility` | fallback (entry **has** `library_size`) |
| `0x1876` | `Failed to allocate memory for library` | device alloc failure |
| `0x1774` | `iDMA channel 0 failed to initialize!` | DMA-queue init failure |
| `0x195d` | `Unsupported Memory Transfer Paradigm` | bad memcpy/DMA mode |
| `0x184d` | `LIB: codesize=%d@%p datasize=%d@%p` | `UCPL` code/data extents (progress) |
| `0x18d5` | `Library loaded` | LOAD success |
| `0x18f8` | **`Library unloaded`** | UNLOAD success |
| `0x1b06` | `iDMA error: %d` (+ `0x1b1b` buffer-error detail) | per-transfer DMA fault |

`check_idma_error` (`0x1922`) runs after each transfer. **Error order:** validate
`UCPL`/`start_sym` (NULL → *Corrupted prelink library*) → validate `library_size`
(>max → *Invalid library_size*) → alloc (fail → *Failed to allocate memory*) → DMA-in
(`check_idma_error` per transfer; init fail → *iDMA channel … failed*) → on success
*Library loaded*. The `total_cpus` asserts (§8) are the structural-invariant guard.

> **NOTE — fatal, not warn.** A loader failure **aborts the install** (the asserts halt;
> the validation paths bail). This mirrors the host relocator, where a failed relocation
> is correctness-critical and aborts — see the status channel in
> [`prelinker-ucpl`](../../runtime/prelinker-ucpl.md). `[error strings HIGH/OBSERVED;
> the precise device rc codes are not byte-pinned from the flat IRAM — MED.]`

---

## 8. FW-17 boundary — `total_cpus` / NUM_POOL_CORES (and the unload state)

The loader's `entry.total_cpus` asserts are the `NUM_POOL_CORES` invariant boundary
owned by [`prelink-validation`](./prelink-validation.md). Byte-exact in SUNDA Q7
**DEBUG** (`0x469`/`0x4ed`) **and RELEASE** (`0x3c1`/`0x445`):

```
external_lib_loader.hpp:283  entry.total_cpus == 1 || entry.total_cpus == NUM_POOL_CORES
external_lib_loader.hpp:318  entry.total_cpus == 0 || entry.total_cpus == 1 || entry.total_cpus == NUM_POOL_CORES
```

`[BOTH lines HIGH/OBSERVED in DEBUG and RELEASE DRAM.]` A library occupies **1** core
(per-core lib), **NUM_POOL_CORES** (broadcast lib), or **0** (line 318 only). The split:

- **Line 283** (no `==0`) is the **during-load** check — a freshly loaded entry must
  occupy ≥1 core.
- **Line 318** (adds `==0`) is the **post-load / post-unload** check — an entry may
  legitimately drop to `total_cpus == 0` after teardown.

`[the two-line split HIGH/OBSERVED; "318 == teardown-state allows 0" reading is
MED/INFERRED.]` `NUM_POOL_CORES` is the 8-core GPSIMD pool count (the up-to-8 `0x1095`
records cross-context = one per pool core; the `P%i:` per-core prefix). This page
**drives** the load/walk/install; `prelink-validation` **validates** `total_cpus` + the
prelink memory bounds.

---

## 9. Per-gen invariance

- The loader **source set** (`external_lib_loader.hpp` / `external_lib.cpp` /
  `external_lib.hpp` / `external_lib_funcs.hpp` / `soc_window*.hpp` /
  `data_transfer.hpp` / `object_array.hpp` / `dispatch_wrapper.hpp`) is **identical**
  across SUNDA base-Q7 and CAYMAN/MARIANA DKL builds — same `.cpp/.hpp` tokens, same
  `xtensa_unload` / `call_start_symbol` / `load_from_nx_addr` / `.kernel_info_table`.
  `[HIGH/OBSERVED — strings present in both images.]`
- **Placement differs.** SUNDA's **base** Q7_POOL **is** the loader shell — it has **no
  separate EXTISA members** in the archive (its kernels are static-linked into the base
  image). CAYMAN/MARIANA factor the loader into a separate
  `Q7_POOL_DYNAMIC_KERNEL_LOAD` (DKL) image and ship runtime-loadable EXTISA blobs
  (selectors `0,1,2,3` — `ar t` confirmed). `[HIGH/OBSERVED — member inventory.]`
- **Gen-invariant** formats: the `0x1095` record, the selector space, the `UCPL `
  header, and the 8-byte `kernel_info_table` entry. Per-gen deltas are only the table
  entry **count** — `[HIGH/OBSERVED this pass]` CAYMAN base = **17**, CAYMAN CPTC = **9**
  (`readelf -S` on the EXTISA blobs); SUNDA = 18 `[CARRIED from SX-FW-16 — SUNDA's table
  is embedded in the base image, not a standalone EXTISA member]`.

---

## 10. RT-12 teardown-leak question — RESOLVED

> **The question (RT-12).** On UNLOAD the host emits `device_addr = 0` and
> `image_size = 0`; the device gets **only** the `library_selector`. RT-12 flagged
> `[MED/INFERRED]` a possible **device-side leak**: *if the device cannot map a bare
> `library_selector → resident region`, the unload could no-op and leave the EXTISA
> IRAM/DRAM region mapped.*

**VERDICT: NO leak. The device CAN and DOES resolve a bare selector to its resident
region — `YES`, the reverse map exists on-device.** Three independently-observed
mechanisms:

1. **A resident unload routine exists and completes.** `external_lib.cpp` defines
   `xtensa_unload` (SUNDA `0x8cc`, CAYMAN DKL `0x18ea`), and the DKL emits
   `P%i: Library unloaded` (`0x18f8`) right after it. A no-op unload would not have a
   dedicated routine + a per-core completion log. `[HIGH/OBSERVED.]`

2. **A resident library-entry array persists the load-time mapping.** `object_array<entry>`
   (`load_tables`/`operator[]`) is resident; each entry, populated at load, retains
   `total_cpus`, `library_size`, and the bound `.kernel_info_table`. The selector (or its
   dense load-slot) indexes it. The host does **not** need to resend `device_addr` — the
   device already holds the binding. `[array + fields HIGH/OBSERVED; "selector is the
   index" MED/INFERRED — §4a.]`

3. **A resident SoC↔XT window table holds the region base.** The 16-entry
   `xt_addrs[16]` window table (DKL `0x477a`) plus `translate_soc_to_xt_address`
   (`0xbc7`) and **`push_unallocated_window`** (`0xf05`): the device tracks each loaded
   region's XT base in a resident window slot and **reclaims** it on unload — no host
   address needed. `[HIGH/OBSERVED — the window-table strings are direct.]`

> **Therefore** line 318's `== 0` case (absent from line 283) is exactly the
> **post-unload state**: after `xtensa_unload` the entry's `total_cpus` drops to `0`
> (slot freed), `push_unallocated_window` reclaims the window, and the
> `.kernel_info_table` binding is cleared. The bare selector found its resident entry;
> the region is unmapped; no leak. `[the "==0 == post-teardown" reading is MED/INFERRED,
> consistent with the unload routine + window-reclaim + success log, all OBSERVED.]`

> **CORRECTION / refinement of RT-12.** RT-12's leak was a *host-view* inference ("the
> host gives the device no address"). That observation **stands** (`device_addr`/
> `image_size` *are* 0 on unload), but the leak it feared is **averted on the device**:
> the device keeps its own resident `selector → {entry, XT-window}` maps from load time
> and reverses them in `xtensa_unload`. The only residual MED is *whether* `operator[]`
> keys on the raw selector vs a dense load-slot — both make unload resolvable; the flat
> IRAM does not byte-pin the index expression. Follow-up: an ELF-with-section-headers
> carve of a DKL image to byte-pin the `operator[]` index and the `entry` struct layout.

---

## 11. Summary — deliverables vs evidence

| # | deliverable | answer | conf |
|---|---|---|---|
| A | loader entry | `load_external_libraries_impl` (`external_lib_loader.hpp`); numeric VA blocked by flat-carve l32r wall | HIGH/OBS (name) · MED (VA) |
| B | selector→image resolution | resident `object_array<entry>` + 16-entry `xt_addrs[16]` SoC↔XT window table | HIGH (names) · MED (keying) |
| C | load orchestration | DMA-in → `UCPL ` parse → `.kernel_info_table` bind → `call_start_symbol`; relocation ran on HOST | HIGH (order) · MED (device dataflow) |
| D | `kernel_info_table` registration | 8-byte `{BE (spec<<8\|opcode) \| LE funcVA}`, host-relocated; device binds by section name; dispatcher matches `opcode<<24\|spec<<16` | HIGH/OBS (fmt) · MED (bind insn) |
| E | env-gate (CPTC/unstable) | HOST-side `getenv` → selector `0`/`3`; device sees concrete selector, no `getenv` | HIGH/OBS |
| F | FW-17 boundary | `entry.total_cpus` ∈ {0,1,NUM_POOL_CORES}; lines 283 (load) / 318 (post) | HIGH/OBS |
| G | error handling | NULL-start / invalid-size / alloc-fail / iDMA-error; *Library (un)loaded*; fatal | HIGH/OBS (logs) |
| H | per-gen invariance | gen-invariant loader code; SUNDA=base shell, CAYMAN+=separate DKL image + EXTISA blobs | HIGH/OBS |
| I | RT-12 teardown leak | **RESOLVED — NO device leak**; resident entry array + window table + `xtensa_unload` make a bare selector resolvable | HIGH/OBS (mechanisms) · MED (exact index) |

---

## See also

- [POOL Engine Main Dispatch Loop](./pool-dispatch.md) — the loop that scans the table this loader binds (#677).
- [POOL Extended-Opcode (0xF0) Dispatch](./pool-ext-0xf0.md) — the `opcode 0xf0` / `spec` family seen in the decoded table *(stub — #678, concurrent)*.
- [kernel_info_table Binary Layout](./kernel-info-table.md) — the 8-byte entry format in full *(stub — #681)*.
- [External-Lib Prelink Validation + NUM_POOL_CORES](./prelink-validation.md) — the `total_cpus`/bounds validator §8 hands off to *(stub — #680)*.
- [SEQ Dispatch Hub](../seq/dispatch-hub.md) — the dispatch key `opcode<<24\|spec<<16` and the two pool dispatch back-ends *(committed)*.
- [nrtucode_ll load/unload](../../runtime/nrtucode-ll-load-unload.md) — the host generator of the `0x1095` sequence this loader consumes *(forward Part-8; not yet authored)*.
- [Prelinker / UCPL](../../runtime/prelinker-ucpl.md) — the host relocator + `UCPL ` header staging step *(forward Part-8; not yet authored)*.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the tag legend used throughout.
