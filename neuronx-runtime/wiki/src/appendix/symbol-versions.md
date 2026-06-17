# Symbol-Version Manifest

> *Binary: `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; git `0b044f4ce917b633a70eb3d0bc460f34ac3da620`). ELF64 LSB DYN x86-64, NOT stripped, DWARF v4. Every export address is an analysis VMA (`.text` VMA == file offset for this image — see [ELF Anatomy](../forensics/elf-anatomy.md)). Other versions will differ.*
>
> *Part XVI — Appendices / REFERENCE catalogue · **Evidence grade:** the `libnrt` version-node graph, the per-node export roster, and the `nec_*`/`nrt_*` reverse seam are byte-anchored to the binary's `.gnu.version_d`/`.gnu.version` (`readelf -V`) and `.dynsym` (`nm -D --with-symbol-versions`). The binaries are **not** present in this extract tree; the counts and addresses below are reconciled from the cross-cell symbol-version evidence and the in-tree boundary pages ([nccl-boundary](../collectives/nccl-boundary.md), [nccom/abi](../nccom/abi.md), [elf-anatomy](../forensics/elf-anatomy.md), [api-async-collectives](../runtime/api-async-collectives.md)) that were each derived from `readelf`/`nm` on the binary — every count is either cross-page-consistent or confidence-tagged. · [back to index](../index.md)*

## Abstract

`libnrt.so` carries a three-node symbol-version graph in its `.gnu.version_d` table (VMA `0xa238`, `VERDEFNUM 3`): the BASE filename node `libnrt.so.1` (the SONAME, binding no symbol), `NRT_2.0.0` (the bulk public C ABI), and `NRT_3.0.0` (the explicit-async `nrta_*` family), the last declared with `Parent 1: NRT_2.0.0` so it **inherits** the v2 node. There is **no** `NRT_1.0.0` — the legacy ABI is collapsed into the BASE/`NRT_2.0.0` pair, and the only ABI evolution this build records is the additive `NRT_3.0.0` child. A reimplementation's linker version script must place each export in the same node it lands in here, or symbol resolution against an existing client breaks; the parent edge is what lets a v2-only consumer link a v3-capable runtime transparently.

This manifest pins two distinct ABI surfaces that share the same version nodes. The **forward export ABI** is the public façade consumers link against: 137 `GLOBAL` entry points under `NRT_2.0.0` (121 `nrt_*` plus 16 `nec_*` device-callback exports) and 8 `nrta_*` async producers under `NRT_3.0.0` — 145 `GLOBAL` exports total (zero `WEAK`). The **reverse seam** is the load-time versioned import surface the sibling collectives library binds: `libnccom.so` carries `DT_NEEDED libnrt.so.1` and a VERNEED on `NRT_2.0.0`, then imports exactly **16 `nec_*` + 4 `nrt_*` = 20** callbacks back into `libnrt`, every one `@NRT_2.0.0`. Those `nec_*` exports are simultaneously part of `libnrt`'s forward roster (they are `@@NRT_2.0.0` definitions) and the entire reverse-callback contract — the one place a `libnrt` export exists *because* a peer library needs to call back in. The page closes the catalogue: the version-node table, the forward roster grouped by family, the 20-symbol reverse seam, and the cross-binary export surface for the three sibling `.so`s libnrt loads or is loaded against.

| | |
|---|---|
| **Version-node table** | `.gnu.version_d` @`0xa238`, `VERDEFNUM 3` — `libnrt.so.1` (BASE) · `NRT_2.0.0` · `NRT_3.0.0` (parent `NRT_2.0.0`) |
| **SONAME** | `libnrt.so.1` (BASE node, index 1) — no `NRT_1.0.0` node exists |
| **GLOBAL export surface** | **145** = 137 `NRT_2.0.0` GLOBAL + 8 `NRT_3.0.0` GLOBAL (zero WEAK) |
| **Raw `@@NRT_*` all-binding** | **149** = 141 `@@NRT_2.0.0` (137 GLOBAL + 4 LOCAL `std::string` insts) + 8 `@@NRT_3.0.0` |
| **`NRT_2.0.0` roster** | 121 `nrt_*` + 16 `nec_*` GLOBAL + 4 LOCAL `std::string` template bodies |
| **`NRT_3.0.0` roster** | 8 `nrta_*` async-execution producers (`0x7bb00`–`0x7e440`) |
| **Reverse seam** | 16 `nec_*` + 4 `nrt_*` = **20** `@NRT_2.0.0` callbacks `libnccom` imports (see [nccl-boundary](../collectives/nccl-boundary.md)) |
| **Unversioned defined exports** | **0** — every public symbol binds to a version node |
| **VERNEED** (`.gnu.version_r` @`0xa298`) | 7 toolchain files only (`libc`/`libstdc++`/`libgcc_s`/`libm`/`libpthread`/`libdl`/`ld-linux`) — no NRT-internal cross-lib need |

> **NOTE — two export counts, both correct, neither wrong.** The GLOBAL/WEAK export surface is **145** (137 + 8); a naive `readelf --dyn-syms | grep '@@NRT_'` returns **149**, inflated by 4 `LOCAL` `std::string` template bodies the version script's global pattern swept into `NRT_2.0.0` (`_M_dispose` @`0xc6a60`, `_M_replace` @`0xc6dc0`, `_M_assign` @`0xc6a90`, `_M_mutate` @`0xc6bd0`). They are version-tagged yet `LOCAL`, so not externally bindable and not part of the intended API. Ground any export-count claim on `GLOBAL|WEAK` binding, never on a raw `@@NRT_*` grep. The 149 figure is the all-binding total this manifest also reports for completeness; [ELF Anatomy §2](../forensics/elf-anatomy.md) carries the same correction.

---

## 1. Version Nodes

The export ABI is governed by the 3-entry `.gnu.version_d` graph (`readelf -V`, `VERDEF` @`0xa238`, `VERDEFNUM 3`). Index 1 is the BASE filename node — it *is* the SONAME, names the library, and binds no symbol. Indexes 2 and 3 are the two real ABI levels. `NRT_3.0.0` declares `Cnt: 2` with `Parent 1: NRT_2.0.0`, so it is a strict superset: a v3 client resolves every v2 symbol transparently through the parent edge. Per-symbol indexing is done by `.gnu.version` (VERSYM @`0x9c38`, 2 bytes per dynsym entry).

| Node | Index | Parent | SONAME | GLOBAL exports | Role | Confidence |
|---|---|---|---|---|---|---|
| `libnrt.so.1` | 1 | BASE | `libnrt.so.1` | 0 | the SONAME filename node — names the library, defines no symbol | CERTAIN |
| `NRT_2.0.0` | 2 | none | — | **137** (+4 LOCAL) | the bulk public C ABI: 121 `nrt_*` + 16 `nec_*` GLOBAL; the 4 LOCAL `std::string` instantiations swept in by the version script | CERTAIN |
| `NRT_3.0.0` | 3 | `NRT_2.0.0` | — | **8** | the explicit-async `nrta_*` schedule/transfer surface only | CERTAIN |

Verbatim `.gnu.version_d` (the dynstr literals are exactly `"libnrt.so.1"`, `"NRT_2.0.0"`, `"NRT_3.0.0"`):

```text
000000: Rev:1 Flags:BASE Index:1 Cnt:1 Name: libnrt.so.1
0x001c: Rev:1 Flags:none Index:2 Cnt:1 Name: NRT_2.0.0
0x0038: Rev:1 Flags:none Index:3 Cnt:2 Name: NRT_3.0.0
0x0054:   Parent 1: NRT_2.0.0
```

> **NOTE — there is no `NRT_1.0.0`.** The graph records exactly one ABI break point: the additive `NRT_3.0.0` child. Whatever a `1.x` runtime exported was folded into `NRT_2.0.0` (and the unversioned BASE), so a reimplementer who expects a v1 node to satisfy old clients is wrong — the v2 node is the floor, and `libnccom`'s VERNEED needs `NRT_2.0.0`, not `NRT_1.0.0` ([nccl-boundary §4](../collectives/nccl-boundary.md)). The child-versioning is *additive only*: adding `nrta_*` under a `NRT_3.0.0` parented on `NRT_2.0.0` lets the runtime grow the async surface without breaking a binary linked only against v2.

> **GOTCHA — the BASE node is not an empty slot to drop.** Index 1 (`libnrt.so.1`) is the SONAME node; it carries the `BASE` flag and binds zero symbols, but a version script that omits it produces a `.so` whose `NRT_2.0.0`/`NRT_3.0.0` nodes have no filename anchor and whose VERSYM indices shift. The BASE node is mandatory scaffolding even though it exports nothing — `VERDEFNUM` is 3, not 2, precisely because the filename node counts.

---

## 2. Forward Export Roster

The 145 GLOBAL exports group into four families by name prefix and version node. This roster gives the family, its node, its count, and a representative slice of symbols — not the full 145-name dump, which a reimplementer reconstructs from the family rule plus the per-page catalogues. Every `nrt_*` and `nrta_*` argument signature is owned by the public-API pages ([api-async-collectives](../runtime/api-async-collectives.md) and siblings); every `nec_*` signature and thunk target by the [nccom ABI catalogue](../nccom/abi.md).

| Family | Version node | Count | Representative symbols | Confidence |
|---|---|---|---|---|
| `nrt_*` public C API | `@@NRT_2.0.0` | 121 | `nrt_init`, `nrt_close`, `nrt_load`, `nrt_execute`, `nrt_tensor_allocate`, `nrt_barrier`, `nrt_all_gather`, `nrt_build_global_comm`, `nrt_get_libnccl_net`, `nrt_async_sendrecv_init`, `nrt_profile_session_start`, `nrt_inspect_begin`, `nrt_sys_trace_start` | HIGH |
| `nec_*` device/topology callbacks | `@@NRT_2.0.0` | 16 | `nec_get_device_count` (`0x1bfd50`), `nec_get_virtual_core_size` (`0x1bfdd0`), `nec_inc_semaphore` (`0x1bfd40`), `nec_get_dynamic_send_size_bytes` (`0x1bfef0`) — the reverse seam, §3 | HIGH |
| `std::string` template bodies (LOCAL) | `@@NRT_2.0.0` | 4 | `_M_dispose` (`0xc6a60`), `_M_replace` (`0xc6dc0`), `_M_assign` (`0xc6a90`), `_M_mutate` (`0xc6bd0`) — version-tagged but `LOCAL`, not API | HIGH |
| `nrta_*` async-execution producers | `@@NRT_3.0.0` | 8 | `nrta_cc_prepare` (`0x7c980`), `nrta_cc_schedule` (`0x7bd80`), `nrta_execute_schedule` (`0x7bb00`), `nrta_tensor_copy` (`0x7d8c0`), `nrta_is_completed` (`0x7c720`) | HIGH |

The 8 `NRT_3.0.0` exports are exactly the async schedule/transfer family ([api-async-collectives §1](../runtime/api-async-collectives.md)), the complete node — `readelf -W --dyn-syms | grep '@@NRT_3.0.0'`:

```text
nrta_cc_prepare        @0x7c980     nrta_cc_schedule       @0x7bd80
nrta_execute_schedule  @0x7bb00     nrta_get_sequence      @0x7c450
nrta_is_completed      @0x7c720     nrta_tensor_copy       @0x7d8c0
nrta_tensor_read       @0x7e440     nrta_tensor_write      @0x7df10
```

The 121 `nrt_*` exports under `NRT_2.0.0` split by subsystem: init/lifecycle, model load/execute, the tensor + tensor-set surface, device/topology queries, the host-collective ops (`nrt_all_gather`/`nrt_barrier`/`nrt_build_global_comm`/`nrt_get_libnccl_net`), the 11 `nrt_async_sendrecv_*` point-to-point thunks plus 2 async-exec helpers, and the profiling/inspect/sys_trace/debug families. The shape and signatures of each subsystem are owned by the respective public-API pages; this roster pins only *which node* each family lands in.

> **QUIRK — `nec_*` exports are forward exports and reverse callbacks at once.** The 16 `nec_*` symbols are `@@NRT_2.0.0` GLOBAL definitions in `libnrt`'s dynamic table (so they count in the 137 GLOBAL `NRT_2.0.0` total and the 145 surface), yet no other part of `libnrt` calls them through the dynamic table — they exist solely so `libnccom` can resolve them at load (§3). They are the only `libnrt` exports whose *consumer* is a sibling library rather than a framework integrator. A reimplementation that drops them from the version script because "nothing internal needs them" breaks `libnccom`'s load. They are counted once, here in the forward roster; §3 catalogues them as the reverse seam.

> **NOTE — `nrtucode_*` and `enc_*` are NOT in `libnrt`'s dynsym.** The microcode loader (`tdrv/ucode.c`) and the embedded NCCL-fork algorithm engine (`enc_*`/`alg_ring`/`kangaring`/`mesh`) are internal: zero `nrtucode_*` and zero `enc_*` symbols are exported from `libnrt.so` ([ucode-facade](../gpsimd/ucode-facade.md), [nccl-boundary](../collectives/nccl-boundary.md)). The `nrtucode_*` ABI is a *consumed* surface — its 52 exports live in the sibling `libnrtucode_extisa.so` (§4), `dlsym`'d in, never re-exported. Do not look for them in this manifest.

---

## 3. The `nec_*` / `nrt_*` Reverse Seam

The reverse seam is the load-time versioned import surface the sibling collectives library binds back into `libnrt`. `libnccom.so` carries `DT_NEEDED libnrt.so.1` and a `.gnu.version_r` entry requiring `(libnrt.so.1, NRT_2.0.0)` — and *only* `NRT_2.0.0`, never `NRT_3.0.0`. It imports **20** symbols, all `@NRT_2.0.0`, every one resolving name-for-name at *load* time against a `libnrt` `@@NRT_2.0.0` definition: **16 `nec_*`** device/topology/proxy callbacks (`libnrt` band `0x1bfd40..0x1bff20`) and **4 `nrt_*`** runtime queries (`0x83e10`/`0x84210`/`0x940b0`/`0x94400`). This is the entire `libnrt` ← `libnccom` ABI; the conceptual seam and the `nec_*` thunk semantics are owned by [nccl-boundary](../collectives/nccl-boundary.md), the symbol-by-symbol table by [nccom/abi](../nccom/abi.md).

| Group | Symbols | `libnrt` band | Version | Confidence |
|---|---|---|---|---|
| `nec_*` device/topology/proxy callbacks | `nec_inc_semaphore` `0x1bfd40`, `nec_get_device_count` `0x1bfd50`, `nec_get_device_pci_bdf` `0x1bfd60`, `nec_get_virtual_core_size` `0x1bfdd0`, `nec_build_port_and_rid_map` `0x1bfdf0`, `nec_is_mla_available` `0x1bfe00`, `nec_mla_idx_to_rid` `0x1bfe10`, `nec_rid_to_mla_idx` `0x1bfe20`, `nec_get_peer_mla_idx` `0x1bfe30`, `nec_get_p2p_pod_peer_node` `0x1bfeb0`, `nec_pod_node_can_access_peer_node` `0x1bfed0`, `nec_ndl_printk` `0x1bfee0`, `nec_get_dynamic_send_size_bytes` `0x1bfef0`, `nec_get_dynamic_send_offset_bytes` `0x1bff00`, `nec_get_dynamic_recv_offset_bytes` `0x1bff10`, `nec_set_recv_size_bytes` `0x1bff20` | `0x1bfd40..0x1bff20` | `@@NRT_2.0.0` | HIGH |
| `nrt_*` runtime queries | `nrt_get_total_vnc_count` `0x83e10`, `nrt_get_instance_info` `0x84210`, `nrt_get_version` `0x940b0`, `nrt_get_libnccl_net` `0x94400` | scattered | `@@NRT_2.0.0` | HIGH |
| **Reverse seam total** | **16 `nec_*` + 4 `nrt_*` = 20**, all `@NRT_2.0.0`; **zero** binding `NRT_3.0.0` | — | — | HIGH |

> **CORRECTION (ENC-08) — the reverse `nec_*` count is 16, not 12.** An early single-cell pass over the `enc/nec.cc` band reported **12** `nec_*` callbacks, and the original raw symbol-version note carried that figure into a draft. The audited dynsym count is **16**. The "12" was a band-of-analysis artifact: that pass swept only `0x1bfdf0..0x1bff20` (the twelve topology/dynamic-size shims that live in one cell) and missed the four lower-addressed callbacks `nec_inc_semaphore` (`0x1bfd40`), `nec_get_device_count` (`0x1bfd50`), `nec_get_device_pci_bdf` (`0x1bfd60`), and `nec_get_virtual_core_size` (`0x1bfdd0`). The both-binaries closure — `nm -D --undefined-only` on `libnccom`'s import side cross-checked against `libnrt`'s `@@NRT_2.0.0` export side — settles it at **16 `nec_*` + 4 `nrt_*` = 20**, the figure [nccl-boundary](../collectives/nccl-boundary.md) and [nccom/abi](../nccom/abi.md) both carry and the count a reimplementation must export `@@NRT_2.0.0`.

> **NOTE — the forward leg crosses no version node.** The opposite direction (`libnrt` → `libnccom`, 37 `neuron*` entry points) is a runtime `dlopen`+`dlsym` keyed on bare name with **no** ELF version tag and no `DT_NEEDED` — `libnrt` holds zero `neuron*` symbols in its dynsym, and `libnccom` exports the 37 as plain `T` globals with no `.gnu.version_d` node of its own ([nccl-boundary §1](../collectives/nccl-boundary.md), [nccom/abi](../nccom/abi.md)). Only the reverse seam touches the symbol-version manifest. The forward compatibility token is the *numeric* id `89` returned by `neuronGetVersionInfo`, an entirely separate mechanism from the `NRT_2.0.0` ELF version — the two skew axes never meet.

> **NOTE — local `nec_*` are correctly absent.** The `libnrt`-local `nec_get_version_info` (`0x1bfde0`, a `jmp ncclGetVersionInfo`) and the eleven `nec_vil_*` helpers (`0x260240+`) are **not** in the dynamic symbol table and are correctly absent from `libnccom`'s import list — they are intra-`libnrt` calls, not boundary symbols. Counting them inflates the reverse set; the seam is the dynsym surface only, which is why the count is exactly 16, not 17 or 27.

---

## 4. Cross-Binary Export Surface

`libnrt` is one of four shared objects in `opt/aws/neuron/lib/`. The other three differ structurally: none carries an `NRT_*` version-def graph — they are unversioned export surfaces, reached by `libnrt` through `dlopen`/`dlsym` (the firmware and microcode images) or binding *into* `libnrt` through the versioned reverse seam (the collectives library). This table fixes their SONAMEs, build-ids, export counts, and whether they version their exports, so a reimplementer knows which surfaces are versioned ABIs and which are bare `dlsym` target lists.

| Binary | SONAME | Build-id | Export count | Versioned? | Confidence |
|---|---|---|---|---|---|
| `libnrt.so.2.31.24.0` | `libnrt.so.1` | `8bb57aba…c102e` | **145** GLOBAL (137 `NRT_2.0.0` + 8 `NRT_3.0.0`) | **yes** — 3-node `.gnu.version_d` (§1) | CERTAIN |
| `libncfw.so` | `libncfw.so.2.31.1.0.cf13a49f` | `a98f8e1c…00db5` | **3** (`libncfw_get_image`/`_get_version`/`_ctx_log`) | no — empty `.gnu.version_d`; version + hash *in the SONAME* | HIGH |
| `libnrtucode_extisa.so` | (stripped) | `7bb03bc4…ae5e44` | **52** `nrtucode_*` (all `T`) | no — bare `T` exports, stripped image | HIGH |
| `libnccom.so` | `libnccom.so.2` | `9c00176c…463f0` | **37** `neuron*` (all bare `T`) | no — unversioned; VERNEED on `libnrt`'s `NRT_2.0.0` for the 20 reverse imports | HIGH (boundary-derived) |

The export counts are cross-checked against the in-tree pages: `libncfw`'s 3 entry points and SONAME-embedded version are from [ELF Anatomy](../forensics/elf-anatomy.md) and [coretype-numbering](../arch/coretype-numbering.md); `libnrtucode_extisa`'s 52 `nrtucode_*` exports from [extisa-provider](../gpsimd/extisa-provider.md) (`get`×7, `context`×5, `core`×22, `ll`×9, `opset`×9); `libnccom`'s 37 `neuron*` forward exports and `NRT_2.0.0` VERNEED from [nccom/abi](../nccom/abi.md). `libnccom`'s SONAME `libnccom.so.2` and build-id are catalogued at the boundary; the library itself is in a separate `aws-neuronx-collectives` package, so its internal `.gnu.version_d` (it defines none — it only *consumes* `libnrt`'s `NRT_2.0.0` node) is boundary-derived.

> **QUIRK — three sibling `.so`s, three different versioning conventions.** `libnrt` versions its exports in `.gnu.version_d` (the `NRT_*` graph); `libncfw` versions itself *in the SONAME string* (`libncfw.so.2.31.1.0.cf13a49f` — version `2.31.1.0` + git short-hash `cf13a49f`) with an empty version-def table; `libnrtucode_extisa` is stripped and versions through an *API-level constant* (`nrtucode_get_api_level()` returns `3`, checked by the consumer, [extisa-provider](../gpsimd/extisa-provider.md)); `libnccom` does not version its 37 forward exports at all (bare `T` for `dlsym`) and instead *imports* a version need on `libnrt`'s `NRT_2.0.0`. A reimplementer must not assume "collectives/firmware ABI ⇒ ELF symbol versions": only `libnrt` uses that mechanism, and only on its own exports and the one reverse need.

> **NOTE — `libnccom-net.so` is a fourth, edge-only image.** The OFI/libfabric net plugin (`libnccom-net.so`, build-id `3415f096…a654a`, exporting the `ncclNetPlugin_v4/v5/v6` structs) is `dlopen`'d softly by `libnrt`'s `ncclInit` and reaches `libnrt` only through the single `nrt_get_libnccl_net@NRT_2.0.0` handoff ([nccom/abi NET edge](../nccom/abi.md)). It is recorded here for completeness; its 3 `ncclNetPlugin_vN` exports are data objects following the upstream `ncclNet_vN` ABI, not the `nec_`/`neuron` symbol-version surface this manifest pins.

---

## Cross-References

- [The libnrt ↔ libnccom Boundary (nec_ / nccl)](../collectives/nccl-boundary.md) — the conceptual two-link seam (forward `dlsym` vs reverse versioned bind), the ENC-08 `nec_*` count correction (16, not 12), and the `nec_*` thunk semantics this manifest pins to version nodes
- [The libnrt ↔ libnccom ABI](../nccom/abi.md) — the symbol-by-symbol catalogue: every `neuron*` forward definer, every `nec_*`/`nrt_*` reverse definer, and the re-derived 37 / 16+4 / 3 counts the reverse-seam and cross-binary tables here summarize
- [ELF Anatomy](../forensics/elf-anatomy.md) — the `.gnu.version_d`/`.gnu.version_r` section geometry, the 3-node version graph, the 145-vs-149 export-count correction, and the `libncfw` sibling contrast this manifest's cross-binary table draws on
- [Public C API: Async, SendRecv and Collectives Thunks](../runtime/api-async-collectives.md) — the 8 `nrta_*` `@@NRT_3.0.0` async family and the 11 `nrt_async_sendrecv_*` `@@NRT_2.0.0` thunks the forward roster groups, with their reconstructed signatures
- [The ext-ISA Provider (`nrtucode_*` API)](../gpsimd/extisa-provider.md) — the 52 `nrtucode_*` exports of `libnrtucode_extisa.so` (cross-binary table) and the API-level-3 versioning convention
- [Vendored-Library SBOM](../forensics/vendored-sbom.md) — the `GLIBCXX_3.4.22` / `CXXABI_1.3.11` / `GLIBC_2.28` ABI-floor *needs* (`.gnu.version_r`) that pin the dynamic `libstdc++`/`libc` requirements this manifest's VERNEED line summarizes
