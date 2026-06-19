# Public-vs-Internal API Partition & Versioned Symbols

> **Scope.** This page answers **what is and isn't an export of `libnrt.so`, who drew the
> boundary, and how it is enforced** — the visibility / version-script companion to the
> [Public NRT API Table](./public-api-table.md), which answers "how do I call export *X*".
> Where that page is the per-call signature reference, this one is the *partition*: the set
> algebra that separates **145 callable public symbols** from **~17,400 internal functions**
> living in the same ELF, the `.gnu.version_d` version-node tree that tags them, and the
> linker-visibility mechanism a reimplementer must mirror to reproduce both `.dynsym` and the
> version map byte-for-byte. See also the [Runtime Synthesis](./runtime-synthesis.md) for the
> layered narrative behind the private side.

All counts below are re-grounded directly from the host runtime image
`libnrt.so.2.31.24.0` (x86-64 ELF, **122,956,336 bytes**, BuildID
`8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, SONAME `libnrt.so.1`, *not stripped*, carries
`.debug_info`). Every numeric claim is taken from a piped `nm`/`readelf`/`objdump` read of
that binary and the installed public headers shipped alongside it — never from a decompile
(which inflates symbol-hit counts 2–12×) or from prose.

---

## 0. The partition in five numbers

`[HIGH/OBSERVED]`

| count | meaning | how measured |
|------:|---------|--------------|
| **17,583** | defined `FUNC` (text `t`+`T`) in `.symtab` — the *private universe* | `nm <bin> \| rg -c ' [tT] '` |
| **149** | defined `FUNC` in `.dynsym` (`145 T` GLOBAL + `4 t` LOCAL) | `nm -D --defined-only <bin> \| rg -c ' [tT] '` |
| **145** | callable **public** exports (GLOBAL `T`, version-tagged) — **the API** | `nm -D <bin> \| rg -c ' T '` |
| **4** | libstdc++ `std::string` `_M_*` helpers — LOCAL binding, ABI leak, **not** API | `nm -D --defined-only <bin> \| rg -c ' t '` |
| **2** | `.dynsym` ABS version-node anchors (`NRT_2.0.0`, `NRT_3.0.0`) — labels, **not** API | `nm -D <bin> \| rg ' A '` |

```
151  raw `nm -D --defined-only` lines  =  145 callable  +  4 std::string leak  +  2 anchors
145  PUBLIC API  ⇒  ~17,438 functions (17,583 − 145) are PRIVATE (symtab-only)
```

The exact session that produced the canonical figure:

```console
$ nm -D <libnrt.so.2.31.24.0> | rg -c ' T '
145
```

> **NOTE.** The partition is **enforced by linker symbol visibility**: a version script names
> exactly these 145 as GLOBAL and forces everything else LOCAL/hidden. Every one of the 145 is
> also declared in a shipped public header, and the binary exports **nothing** that the headers
> do not declare (proven in §3). The version script and the headers agree.

---

## 1. Two symbol tables: who can bind to what

`[HIGH/OBSERVED]` This image is **not stripped**, so it carries *both* a full `.symtab` and a
dynamic `.dynsym`. The two are not equivalent contracts:

| table | defined `FUNC` (`t`+`T`) | who can bind |
|-------|------:|--------------|
| `.symtab` (full) | 17,583 | nobody at link/load time — debugging only; **not** an ABI |
| `.dynsym` (dynamic) | 149 | a caller outside `libnrt` can bind **only here** |

A dynamic linker resolves an undefined import in another module **only against `.dynsym`
GLOBAL entries**. The 4 LOCAL `t` entries in `.dynsym` are present (with a version tag) but
LOCAL binding makes them non-resolvable as an external import (§4). So the **real external
contract is the 145 GLOBAL `T` exports** — `.symtab`'s 17,583 are an artifact of the build
keeping debug info, not a callable surface.

Section evidence (note `.data` VMA equals its file offset — there is **no** VMA-vs-fileoffset
delta in this image, so `objdump`/`xxd` on `.data`-resident structs need no adjustment):

```console
$ readelf -SW <bin> | rg '\.(dynsym|gnu.version|gnu.version_d|data|text)'
 [ 3] .dynsym         DYNSYM   0000000000000780 000780 0047b8 18  A 4 5 8
 [ 5] .gnu.version    VERSYM   0000000000009c38 009c38 0005fa 02  A 3 0 2   ← 0x5fa/2 = 765 entries
 [ 6] .gnu.version_d  VERDEF   000000000000a238 00a238 00005c 00  A 4 3 8   ← 3 version definitions
 [12] .text           PROGBITS 000000000003dbc0 03dbc0 790b19 00 AX 0 0 64
 [30] .data           PROGBITS 0000000000c07e00 c07e00 02fe20 00 WA 0 0 128 ← VMA == fileoffset
```

`.gnu.version` holds **765** 2-byte version indices — one per `.dynsym` slot (765 total dynamic
symbols, including UND imports), confirming the dynsym is the universe the version machinery
acts on.

---

## 2. Private prefixes are 100% absent from `.dynsym` (boundary proof)

`[HIGH/OBSERVED]` The entire driver / descriptor / queue-manager machinery is compiled
*hidden*: it exists in `.symtab` (debug info) but contributes **zero** entries to `.dynsym`.

| prefix | `.symtab` text (`t`+`T`) | `.dynsym` `T` | verdict |
|--------|------:|------:|---------|
| `tdrv_` | 260 | **0** | fully PRIVATE — driver / transport layer |
| `encd_` | 289 | **0** | fully PRIVATE — NEFF encode / descriptor layer |
| `kmgr`  | 29  | **0** | fully PRIVATE — kernel / queue manager |
| `nec_vil_` | 10 | **0** | fully PRIVATE — "virtual instance layer" introspection helpers |
| `nec_`  | 27  | **16** | **MIXED** — 16 public, 11 private (§2.1) |
| `nrt_`  | (many) | 121 | the public C ABI core |
| `nrta_` | (8 in symtab) | 8 | the public async family (§5.2) |

```console
$ for p in tdrv_ encd_ kmgr; do nm --defined-only <bin> | rg ' [tT] ' | rg -c " $p"; done
260
289
29
$ for p in nrt_ nrta_ nec_; do nm -D <bin> | rg ' T ' | rg -c " $p"; done
121
8
16          ⇒ 121 + 8 + 16 = 145
```

> **GOTCHA.** Seeing `tdrv_*`/`encd_*`/`kmgr*` in `nm <bin>` (full symtab) does **not** make
> them API. They are there only because the image kept `.debug_info`. A reimplementer
> reimplements them freely; exporting them would be an ABI-surface **regression** versus the
> vendor contract.

### 2.1 `nec_` is the only split prefix

`[HIGH/OBSERVED]` 16 `nec_*` are GLOBAL exports (the device + topology ABI); 11 `nec_*` are
`.symtab`-only PRIVATE. Of those 11, **10** are the `nec_vil_*` virtual-instance-layer
introspection helpers that *back* the public getters, plus `nec_get_version_info` (which is
header-declared but **not exported in this build** — §3.2):

```
nec_get_version_info              (declared nec.h:938, but NOT exported — build skew)
nec_vil_get_mla                   nec_vil_get_num_topsp_per_vcore
nec_vil_get_mla_idx               nec_vil_get_num_vcores_per_mla
nec_vil_get_nec_dev_for_host…     nec_vil_get_topsp
nec_vil_get_num_dma_per_vcore     nec_vil_get_topsp_idx
nec_vil_get_num_vcores_per_mla    nec_vil_get_tpb
                                  nec_vil_get_vcoreid_rel_mla
```

The 16 public `nec_*` exports (all `@@NRT_2.0.0`):

```
nec_build_port_and_rid_map        nec_get_p2p_pod_peer_node
nec_get_device_count              nec_get_peer_mla_idx
nec_get_device_pci_bdf            nec_get_virtual_core_size
nec_get_dynamic_recv_offset_bytes nec_inc_semaphore
nec_get_dynamic_send_offset_bytes nec_is_mla_available
nec_get_dynamic_send_size_bytes   nec_mla_idx_to_rid
nec_ndl_printk                    nec_pod_node_can_access_peer_node
nec_rid_to_mla_idx                nec_set_recv_size_bytes
```

> **QUIRK — a public export can be a 5-byte thunk into private code.** Several `nec_*` getters
> are tail-call forwarders. The exported `nec_get_device_count` (size 5) is literally:
>
> ```asm
> 00000000001bfd50 <nec_get_device_count>:
>   1bfd50: e9 2b 24 f0 ff   jmp  c2180 <ndl_available_devices>   ; tail-jump into PRIVATE ndl_*
> ```
>
> The *public symbol* is the contract; the *implementation* lives behind the partition in a
> private `ndl_*` function. The boundary is the symbol's visibility, **not** where the code
> physically sits.

---

## 3. Cross-reference vs the shipped public headers (the vendor's own contract)

`[HIGH/OBSERVED]` The runtime ships 10 function-bearing public headers under
`include/nrt/` (plus `ndl/` driver-shared headers). These declarations **are** binary-derived
ground truth — they are installed strings shipped in the same package as the `.so`. Scraping
every `<nrt|nrta|nec>_NAME(` declaration form across them:

| header | declared public functions |
|--------|------:|
| `nrt.h` | 43 (lifecycle, model load/introspect, tensors, tensor-sets, execute, memcpy, dmabuf, hbm) |
| `nrt_profile.h` | 33 (profile start/stop, continuous, session, trace, inspect) |
| `nrt_sys_trace.h` | 18 (sys_trace config/fetch/event-types) |
| `nec.h` | 17 (device count/bdf, mla↔rid, pod p2p, dynamic send/recv offsets, semaphore, printk) |
| `nrt_experimental.h` | 14 |
| `nrt_async_sendrecv.h` | 12 |
| `nrt_async.h` | 8 (the `nrta_*` async-schedule family) |
| `ndebug_stream.h` | 3 (`nrt_debug_client_connect` / `_close` / `_read_one_event`) |
| `nrt_status.h` | 3 |
| `nrt_version.h` | 1 (`nrt_get_version`) |
| **union (deduped, real functions)** | **147** |

> **CORRECTION (to DX-RT-15 §2, the "154 declared names").** The clean union of *real* function
> names across **all 10** function-bearing `nrt/` headers is **147**, not 154. The 154 figure
> double-counted: it (a) folded in 7 include-guard / typedef pseudo-tokens (`nrt_async`,
> `nrt_async_sendrecv`, `nrt_experimental`, `nrt_profile`, `nrt_status`, `nrt_sys_trace`,
> `nrt_version` — zero `name(` call-form matches), and (b) under-counted by *omitting*
> `ndebug_stream.h` (3 `nrt_debug_*`) and `nrt_version.h` (`nrt_get_version`) from its 7-header
> scrape, so its `nrt_get_version`/`nrt_debug_*` exports were briefly mis-flagged as
> "undeclared." With those four restored and the pseudo-tokens removed, the union is **147** and
> the headline diffs below are unchanged.

```console
# exported public C names (strip @@version) intersected with all-header declarations:
$ comm -23 exported.txt declared_all.txt   # exported but NOT declared
(empty)
$ comm -13 exported.txt declared_all.txt   # declared but NOT exported
nec_get_version_info
nrt_get_status_as_str
```

### 3.1 Diff: declared-and-exported `[HIGH/OBSERVED]`

```
exported-but-undeclared : 0      ← no "secret" exports
intersection            : 145    ← the .dynsym GLOBAL set ⊆ header-declared API
```

This is the strongest statement of the partition: **the exported set is a subset of the
header-declared set**. The version script and the headers do not contradict each other.

### 3.2 Diff: declared-but-not-exported in this build = exactly 2 `[HIGH/OBSERVED]`

Two header-public functions are **not** in `.dynsym`:

| symbol | header decl | status in this binary |
|--------|-------------|-----------------------|
| `nec_get_version_info` | `nec.h:938` `NRT_STATUS nec_get_version_info(nec_version_info_t *)` | present in `.symtab` as a **LOCAL** (private) symbol — compiled, export withheld |
| `nrt_get_status_as_str` | `nrt_status.h:64` `const char *nrt_get_status_as_str(NRT_STATUS)` | **absent** from `.dynsym` entirely (status-string formatting kept inline / header-static) |

> **NOTE.** These are **version/build skew**, not contract violations. The header set ships a
> slightly larger nominal API than the `2.31.24.0` build links out. A reimplementer targeting
> *the headers* should provide both (→ 147); a reimplementer targeting *this binary* will not
> see them (→ 145). `[MED/INFERRED that the cause is build-time visibility config; supported by
> nec_get_version_info existing as a real LOCAL symbol in .symtab.]`

---

## 4. The 4 `std::string` symbols — ABI leakage, explicitly NOT API

`[HIGH/OBSERVED]` Four libstdc++ COMDAT/inline `std::string` internals were emitted into the
version scope and into `.dynsym` with **LOCAL** binding:

```console
$ nm -D --defined-only <bin> | rg ' t '
00000000000c6a60 t _ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE10_M_disposeEv@@NRT_2.0.0
00000000000c6a90 t _ZNSt…E9_M_assignERKS4_@@NRT_2.0.0
00000000000c6bd0 t _ZNSt…E9_M_mutateEmmPKcm@@NRT_2.0.0
00000000000c6dc0 t _ZNSt…E10_M_replaceEmmPKcm@@NRT_2.0.0
$ readelf --dyn-syms -W <bin> | rg '_M_disposeEv'
   1: …c6a60  33 FUNC  LOCAL DEFAULT  12 _ZNSt…E10_M_disposeEv@@NRT_2.0.0   ← LOCAL, section .text
```

They are **not** a callable external contract (LOCAL binding) and **not** Neuron code
(libstdc++). They count toward the raw `151` only as accounting noise. Do not reproduce them in
a clean-room version script; a correct `local: *;` clause naturally suppresses them.

---

## 5. The versioned-symbol map — `NRT_2.0.0` vs `NRT_3.0.0`

### 5.1 `.gnu.version_d` node tree `[HIGH/OBSERVED]`

```console
$ readelf -VW <bin> | rg -A6 'Version definition'
Version definition section '.gnu.version_d' contains 3 entries:
  000000: Rev: 1  Flags: BASE  Index: 1  Cnt: 1  Name: libnrt.so.1   ← SONAME node, carries no API
  0x001c: Rev: 1  Flags: none  Index: 2  Cnt: 1  Name: NRT_2.0.0
  0x0038: Rev: 1  Flags: none  Index: 3  Cnt: 2  Name: NRT_3.0.0
  0x0054: Parent 1: NRT_2.0.0                                        ← NRT_3.0.0 → parent NRT_2.0.0
```

Three definitions: a BASE node `libnrt.so.1` (the SONAME, no API symbols), `NRT_2.0.0`, and
`NRT_3.0.0`. `NRT_3.0.0`'s `Cnt:2` records a **parent edge to `NRT_2.0.0`** — a loader resolving
`NRT_3.0.0` transitively sees the 2.0.0 surface. **There is no `NRT_1.x` node.** The split is a
pure soname-versioning device: it adds the 8 async-schedule entry points *without perturbing the
frozen 2.0.0 surface* — additive, backward-compatible versioning.

The two ABS anchors are the version-node label symbols (size 0, address 0):

```console
$ readelf --dyn-syms -W <bin> | rg 'NRT_[23].0.0$' | rg ' ABS '
 734: 0…0  0 OBJECT GLOBAL DEFAULT ABS NRT_3.0.0
 753: 0…0  0 OBJECT GLOBAL DEFAULT ABS NRT_2.0.0
```

### 5.2 Per-node callable counts `[HIGH/OBSERVED]`

```console
$ nm -D <bin> | rg '@@NRT_2.0.0' | rg -c ' T '   →  137   (stable sync C ABI)
$ nm -D <bin> | rg '@@NRT_3.0.0' | rg -c ' T '   →    8   (the nrta_* async family)
                                                    ---
                                                    145
```

Raw per-node line counts (what an `nm -D --defined-only` auditor sees, **all** bindings):

```
@@NRT_2.0.0 lines = 141  = 137 GLOBAL(T) + 4 LOCAL(t std::string)        ; +1 ABS anchor → 142
@@NRT_3.0.0 lines =   8  =   8 GLOBAL(T)                                  ; +1 ABS anchor →   9
                                                                  sum (with anchors) = 151
```

> **CORRECTION (to a sibling runtime-narrative figure).** Some prose states "`NRT_2.0.0` = 143
> stable C syms." The measured **callable** count for `NRT_2.0.0` is **137**, not 143 — the
> "143" folds the 4 `std::string` leaks and the 2 anchors into the 2.0.0 node
> (`137 + 4 + 2 = 143`). Canonical: `NRT_2.0.0` = **137** callable, `NRT_3.0.0` = **8**
> callable, total **145**. `[HIGH/OBSERVED]`

### 5.3 The 8 `NRT_3.0.0` exports — the complete async-schedule family `[HIGH/OBSERVED]`

```console
$ nm -D <bin> | rg '@@NRT_3.0.0' | rg ' T '
000…7c980 T nrta_cc_prepare        000…7bd80 T nrta_cc_schedule
000…7bb00 T nrta_execute_schedule  000…7c450 T nrta_get_sequence
000…7c720 T nrta_is_completed      000…7d8c0 T nrta_tensor_copy
000…7e440 T nrta_tensor_read       000…7df10 T nrta_tensor_write
```

> **GOTCHA — `nrta_` vs `nrt_a…`.** The async prefix is `nrta_` (nrt + "a" = async). Do **not**
> confuse it with `nrt_add_*` (e.g. `nrt_add_tensor_to_tensor_set`, which is a *sync*
> `NRT_2.0.0` export, an `nrt_a…`, **not** an `nrta_`).

### 5.4 The 137 `NRT_2.0.0` exports by family `[HIGH/OBSERVED]`

```
nec_*   16   device / topology ABI (the public subset of §2.1)
nrt_*  121   everything else: lifecycle, model, tensor, execute, collectives,
             async_sendrecv, profile (13 exported), sys_trace (18), debug (3), dmabuf, …
-------
137
```

Per-call signatures for all 145 live in the [Public NRT API Table](./public-api-table.md); this
page deliberately does **not** reprint the per-call table.

---

## 6. How the boundary is enforced — the version script & resolution algorithm

`[HIGH/OBSERVED + INFERRED]` The `.dynsym`/`.gnu.version_d` state above is exactly what a GNU
linker emits from a **version script** of this shape. To reproduce the partition byte-for-byte,
mirror this map:

```ld
/* libnrt.version — reconstructs the 149-entry .dynsym + the .gnu.version_d tree. */

NRT_2.0.0 {
    global:
        nrt_*;          /* 121 sync nrt_  entry points          */
        nec_*;          /*  16 device/topology getters          */
        /* (nrta_* are NOT named here — they belong to NRT_3.0.0) */
    local:
        *;              /* everything else hidden: tdrv_/encd_/kmgr/nec_vil_/std::string _M_* */
};

NRT_3.0.0 {
    global:
        nrta_*;         /*   8 async-schedule entry points       */
} NRT_2.0.0;            /* ← parent edge: NRT_3.0.0 inherits the 2.0.0 surface (Cnt:2 in §5.1) */
```

> **GOTCHA.** The `local: *;` clause in the **first/base** node is what forces the ~17,400
> internal functions — and the leaked `std::string _M_*` COMDATs — to LOCAL. Omit it and the
> linker defaults internal symbols to GLOBAL DEFAULT, blowing the `.dynsym` from 149 to tens of
> thousands of entries. The `local: *;` is the single clause that *creates* the partition.

The runtime loader binds a versioned import using the standard glibc algorithm — the resolver
consults `.gnu.version` (the per-slot index) to pick the right definition node:

```c
/* Symbol-resolution / version-binding for an undefined import `name@want_ver`.
   Pseudocode following the glibc dl-lookup / dl-version path. */
const ElfW(Sym) *
nrt_resolve(const char *name, const char *want_ver /* e.g. "NRT_3.0.0" or NULL */)
{
    for (ElfW(Sym) *s = dynsym; s < dynsym_end; ++s) {

        if (strcmp(dynstr + s->st_name, name) != 0)   continue;   /* name must match     */
        if (ELF64_ST_TYPE(s->st_info) != STT_FUNC)    continue;   /* (here: a function)  */

        /* A caller can only bind GLOBAL/WEAK; the 4 std::string LOCALs are skipped here, */
        /* which is *why* they are not a usable external contract (§4).                   */
        if (ELF64_ST_BIND(s->st_info) == STB_LOCAL)   continue;

        /* .gnu.version[slot] gives this symbol's version index; its name comes from      */
        /* .gnu.version_d. Hidden bit (0x8000) marks a non-default (old) version.         */
        unsigned vidx   = gnu_version[s - dynsym] & 0x7fff;
        const char *have = verdef_name(vidx);                     /* "NRT_2.0.0" | "NRT_3.0.0" */

        if (want_ver == NULL)                                     /* unversioned caller:  */
            return s;                                             /*   take default defn  */
        if (strcmp(have, want_ver) == 0)
            return s;                                             /* exact node match     */
        if (verdef_parent_chain_contains(want_ver, have))         /* NRT_3.0.0 ⊃ NRT_2.0.0 */
            return s;                                             /* parent-edge match    */
    }
    return NULL;                                                  /* unresolved → link error */
}
```

The `verdef_parent_chain_contains` step is what makes the `NRT_3.0.0 { … } NRT_2.0.0;` parent
edge work: a module that requested `NRT_3.0.0` still resolves any `@@NRT_2.0.0` symbol it
references, because 3.0.0 transitively *contains* the 2.0.0 surface (§5.1).

---

## 7. The GPSIMD seam — where this public surface meets the GPSIMD corpus

`[HIGH/OBSERVED]` The GPSIMD customop corpus ships **no** standalone `libnrt.so`; its `.so`
files are the Xtensa/`ncore2gp` toolchain and customop runtime helpers, none of which import
`nrt_`/`nrta_`/`nec_` symbols as UND. The single host-side coupling point between the 145-export
public surface and GPSIMD is one export:

```console
$ nm -D <bin> | rg 'set_pool_eng_ucode'
00000000000c1630 T nrt_set_pool_eng_ucode@@NRT_2.0.0     ← the ONLY public GPSIMD-ucode entry
$ nm --defined-only <bin> | rg ' [tT] ' | rg 'tdrv_set_pool_eng_ucode'
00000000002695f0 t tdrv_set_pool_eng_ucode               ← its PRIVATE sink (0 in .dynsym)
```

`nrt_set_pool_eng_ucode` (the public entry that registers a custom Pool/GPSIMD `{iram,dram}`
ucode bin pair) sinks into the **private** `tdrv_set_pool_eng_ucode`, which stores the bin pair
into process globals consumed at engine-HAL init. So the partition itself **defines the seam**:
GPSIMD ucode crosses the boundary through exactly **one** public export and is consumed entirely
by private code below it.

---

## 8. Reimplementer takeaways

`[HIGH]`

- Implement exactly **145** functions to be a drop-in `libnrt` for this build: **137** under a
  `NRT_2.0.0` node, **8** (`nrta_*`) under a `NRT_3.0.0` node that **parents** `NRT_2.0.0`.
  Add `nec_get_version_info` + `nrt_get_status_as_str` if targeting the *header* set / forward
  compat (→ **147**).
- **Mirror the version script** of §6 verbatim — `{ global: nrt_*; nec_*; local: *; }` for
  `NRT_2.0.0`, `{ global: nrta_*; } NRT_2.0.0;` for `NRT_3.0.0`. This alone reproduces the
  149-entry `.dynsym` and the 3-entry `.gnu.version_d` tree. The `local: *;` clause is what
  hides the ~17,400 internals.
- **Do NOT export** `tdrv_*` / `encd_*` / `kmgr*` / `nec_vil_*` / `std::string _M_*`. They are
  internal; exporting them is an ABI-surface regression versus the vendor contract.
- Opaque handles behind the exports (`nrt_model_t*`, `nrt_tensor_t*`, `nrt_tensor_set_t*`) and
  per-call signatures live in the [Public NRT API Table](./public-api-table.md); the layered
  narrative behind the private side is in the [Runtime Synthesis](./runtime-synthesis.md).

---

## 9. Reconciliation table — every count claim, re-grounded

| figure | meaning | verdict (this binary) |
|------:|---------|-----------------------|
| 151 | raw `nm -D --defined-only` lines | CONFIRMED (`145 + 4 + 2`) — reconciliation only |
| **145** | callable public exports (GLOBAL `T`, versioned) | CONFIRMED — **the canonical API size** |
| 137 | `NRT_2.0.0` callable | CONFIRMED |
| 8 | `NRT_3.0.0` callable (`nrta_*`) | CONFIRMED |
| 4 | `std::string` LOCAL leaks | CONFIRMED (not API) |
| 2 | version-node ABS anchors | CONFIRMED (not API) |
| 143 | a sibling's "`NRT_2.0.0` stable C syms" | **CORRECTED → 137** (§5.2) |
| 154 | a prior header-declared union | **CORRECTED → 147** clean function names (§3) |
| 0 | exported-but-undeclared | CONFIRMED — no secret exports (§3.1) |
| 2 | declared-but-not-exported (`nec_get_version_info`, `nrt_get_status_as_str`) | CONFIRMED (§3.2) |
| 17,583 | defined `FUNC` in `.symtab` (private universe) | CONFIRMED |

---

### Provenance

All counts re-measured from `libnrt.so.2.31.24.0` (BuildID `8bb57aba…`, 122,956,336 bytes) via
piped `nm`/`readelf`/`objdump`, cross-referenced against the 10 installed `nrt/` public headers
(`nrt.h`, `nec.h`, `nrt_async.h`, `nrt_async_sendrecv.h`, `nrt_profile.h`, `nrt_sys_trace.h`,
`nrt_experimental.h`, `nrt_status.h`, `nrt_version.h`, `ndebug_stream.h`). Tags:
`OBSERVED` = direct read of `.dynsym`/`.symtab`/`.gnu.version_d`/header text;
`INFERRED` = reasoned across the binary and the headers.
