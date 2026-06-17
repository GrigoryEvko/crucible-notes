# Collectives Firmware — Section Map

> *All addresses on this page apply to `libncfw.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (ELF64 x86-64 DYN, 615,640 B, md5 `e01ea384a76e59d511b4f005b7db98ac`, SONAME `libncfw.so.2.31.1.0.cf13a49f`, build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`; **not** stripped (symtab present, no DWARF); `.rodata` @ `0x65000`, VMA == file offset, so every `0x65xxx`/`0x66xxx`/`0x8xxxx` is both an analysis VMA and a host file offset). `NEEDED` is `libc.so.6` only. Other versions will differ.*
>
> *Evidence grade: **Confirmed (byte- and symbol-anchored)** — the 3-export provider surface, the `5/12/20/28` coretype switch, the 8-blob `.rodata` layout, and the 38×4 serializer inventory are read straight from `nm`/`objdump`/`readelf` plus the IDA `*_functions.json`/`*_callgraph.json`/`*_strings.json` sidecars (metadata `total_functions = 181`, confirmed). Per-arch leaf-struct identity and the silicon codenames are **MEDIUM** where noted. · Part X — Collectives Firmware (libncfw) · [back to index](../index.md)*

## Abstract

`libncfw.so` is the **carrier** for the on-device NCFW ("Neuron Collective FirmWare") sync-core firmware: a pure host-side x86-64 shared object that ships the Tensilica Xtensa LX images each NeuronCore's TPB sequencer runs to choreograph collective-communication DMA, and that — separately — can pretty-print the on-device collective-context struct as JSON for debug. The cleanest familiar frame is a **board-support package that embeds device firmware blobs plus a register-dump utility**: it owns no device I/O of its own (`nm -D` / `readelf -d` show zero `ioctl`/`open`/`write`/`mmap`/`dlopen` — its only imports are `memset`, `snprintf`, `strlen`, `__stack_chk_fail`), so it can neither load nor talk to silicon. It is a *provider and a serializer*, nothing more. libnrt `dlopen`s it, pulls the raw firmware bytes out of its `.rodata`, and does all the DMA and reset-release itself.

The library has exactly **two jobs**, split cleanly across its three exported C symbols. Job A is the **firmware blob provider**: `libncfw_get_image` @`0x1179` is a flat `switch` on an integer architecture ID (the `nrtucode_coretype` value `5`/`12`/`20`/`28` for the four arch generations) that hands back a four-word `{&iram, iram_size, &dram, dram_size}` descriptor pointing directly into the eight embedded blobs — one IRAM (code) + one DRAM (data/descriptor template) per generation. Job B is the **firmware-context JSON serializer**: `libncfw_ctx_log` @`0x1309` dispatches on the same `5/12/20/28` key into a 152-function recursive-descent tree that walks an in-memory collective-communication (CC) context struct — the same struct the on-device sequencer consumes from DRAM — and dumps it as JSON into a 1 MB caller buffer. The third export, `libncfw_get_version` @`0x12fa`, is a one-instruction `mov $0x2,%eax; ret` ABI gate that libnrt asserts equals `2`. This page maps both jobs and links the six Part X sub-pages that derive them; it does not re-walk the Xtensa disassembly (owned by [The NCFW Sequencer](ncfw-sequencer.md)) or the byte-level serializer tree (owned by [Serializer Families](serializer-families.md)).

The single fact a reader must internalize before reading any Part X page is the **three-Xtensa-core split** on the device. The NCFW *sequencer* core (this part) is one of three on-device Tensilica cores, and conflating it with the other two is the easiest mistake to make — see the callout below. libncfw carries only the sequencer firmware; the GPSIMD/Q7 vector microcode lives in a *different* carrier (`libnrtucode_extisa.so`, Part XI) and the Q7 management/FW-IO path is a third thing entirely.

For reimplementation, the contract for the carrier is:

- **The provider ABI** — the three exports, the `5/12/20/28` coretype `switch`, the `{&iram, iram_size, &dram, dram_size}` out-descriptor shape, and the version gate (`get_version` must return `2`).
- **The embedded-blob layout** — eight nm-`'r'` symbols in `.rodata` (`v{2,3,4,4_plus}_ncfw_{iram,dram}_bin` + size words), addresses/sizes/digests, and the v4≡v4_plus DRAM identity. Detailed in [Embedded Payloads](embedded-payloads.md); summarized here.
- **The serializer tree's shape** — 38 distinct families × 4 arch clones = 152 helpers, a uniform `(out, indent, key, struct_ptr)` calling convention, two parallel views (runtime `_ctx` vs static `_configs`), and the recovered CC-context schema the JSON reflects. Detailed in [Serializer Families](serializer-families.md).
- **The boundary** — libncfw is `dlopen`'d, never statically linked; it makes zero syscalls; everything device-facing (DMA-to-IRAM, DRAM-to-HBM patch, reset-release) is libnrt's and the kernel's job, documented in [Firmware Upload Path](upload-path.md).

| | |
|---|---|
| **Binary** | `libncfw.so`, build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`, SONAME `libncfw.so.2.31.1.0.cf13a49f` |
| **Exports (3)** | `libncfw_get_image` @`0x1179` · `libncfw_get_version` @`0x12fa` (returns `2`) · `libncfw_ctx_log` @`0x1309` |
| **Device I/O** | **none** — pure host provider/serializer; imports `memset`/`snprintf`/`strlen`/`__stack_chk_fail`; `NEEDED libc.so.6` |
| **Arch key** | `nrtucode_coretype` ∈ `{5, 12, 20, 28}` → `{v2/sunda, v3/cayman, v4/mariana, v4_plus/mariana_plus}` |
| **Job A — provider** | flat `switch` → `{&iram, iram_size, &dram, dram_size}` into 8 `.rodata` blobs (no header/magic) |
| **Job B — serializer** | `5/12/20/28` dispatch → 152-fn recursive descent → JSON into a 1 MB buffer |
| **Function census** | 181 total = 3 public + 4 arch dispatchers + 152 serializer helpers (38 families × 4, incl. the 4 `ncfw_ctx_log` tops) + ~12 CRT/PLT thunks + per-arch `ncfw_log_buffer_full` `.bss` data |
| **Consumer** | `libnrt.so` `encd_libncfw_init` @`0x251cc0` (`dlopen`) / `encd_ncfw_init` @`0x251eb0` (fptr call) |

> **NOTE — three on-device Xtensa cores, do not conflate.** AWS Neuron silicon carries **three** distinct Tensilica Xtensa-family cores, and a reader meeting them for the first time will merge them. (1) The **NCFW collective sequencer** — *this part* — is an Xtensa LX in a proprietary AWS **sequencer TIE** config; its firmware is the eight blobs in `libncfw.so`; it drives ring/mesh/kangaring/barrier DMA from inside each NeuronCore's TPB sequencer block. (2) The **GPSIMD "Q7"** vector cores are Xtensa LX in the **Tensilica Vision-IVP32** config (Vision-Q7 product, e_machine `0x5e`, TIE type `_TIE_xt_ivp32_xb_vec2Nx8U`); their microcode is 13 ELF32 Xtensa objects in the *separate* carrier `libnrtucode_extisa.so` (Part XI). (3) The **Q7 management CPU / FW-IO** mailbox path is a third role — the MiscRAM mailbox protocol documented in [FW-IO MiscRAM Mailbox](../kernel/fw-io.md) — *not* an ISA, and not carried by either firmware library. Same ISA *family* (Xtensa LX), three different TIE configs and three different jobs. The sequencer's `.tie` is **not** shipped (so its custom ops are opaque); the Q7 IVP32 `.tie` *is* shipped, so that ISA decodes fully — the lineage proof lives in [Tensilica Xtensa and Vision-Q7 Identification](../gpsimd/xtensa-vision-q7.md).

---

## 1. The two jobs

libncfw realizes its purpose through exactly two code paths off the public surface; everything else in the binary is CRT glue or the serializer tree's leaves. The diagram below is the whole library at a glance.

```text
                    libncfw.so  (host x86-64; ZERO device I/O)
                    ┌──────────────────────────────────────────────────────────┐
                    │  libncfw_get_version @0x12fa   → mov $2 ; ret  (ABI gate)  │
                    └──────────────────────────────────────────────────────────┘
   arch_id (coretype 5/12/20/28)
        │
        ├──────────────────────  JOB A: FIRMWARE BLOB PROVIDER  ────────────────────┐
        │   libncfw_get_image @0x1179                                               │
        │     switch(arch_id) { 5→v2 · 12→v3 · 20→v4 · 28→v4_plus ; else EINVAL }    │
        │     out[0]=&<vN>_ncfw_iram_bin  out[1]=<vN>_ncfw_iram_bin_size             │
        │     out[2]=&<vN>_ncfw_dram_bin  out[3]=<vN>_ncfw_dram_bin_size             │
        │            │                                                              │
        │            ▼  pointers into .rodata (no copy, no header, no checksum)     │
        │   ┌──────────────────────── .rodata 0x66a60 .. 0x918e0 ───────────────┐   │
        │   │  v2_iram/dram  v3_iram/dram  v4_iram/dram  v4_plus_iram/dram (×8)  │   │
        │   │  IRAM = Xtensa LX sequencer code · DRAM = exc-table + SoC-addr tbl │   │
        │   └────────────────────────────────────────────────────────────────────┘  │
        │                                                                            │
        └──────────────────────  JOB B: CC-CONTEXT JSON SERIALIZER  ────────────────┘
            libncfw_ctx_log(ctx, out, model_name, arch_id) @0x1309
              switch(arch_id) → <arch>_ncfw_ctx_log → ncfw_ctx_log (memset 1MB; "{")
                → ncfw_log_ctx ("ncfw_ctx_top_sp")
                    → configs (static) → algo/neff/dev configs
                    → neff (runtime)   → op / basic_block / barrier_completed
                    → algo (runtime)   → ring(32×16B) / mesh / hierarchical
              returns 28 (ENOSPC) if the 1MB buffer overflowed, else 0
```

**Job A — firmware blob provider.** `libncfw_get_image` @`0x1179` takes `(arch_id, void* out[4])` and is a flat `cmpl`-chain `switch` (verified `objdump`: `cmpl $0x1c`/`$0x14`/`$0x5`/`$0xc` → per-arch `lea` of the four `*_bin`/`*_bin_size` symbols, with a `cmpq $0x0` null-out guard that returns early). It performs **no copy**: the out-words are addresses-into-`.rodata` and the raw `*_bin_size` u64 values. Return codes are `0` (ok), `22`/EINVAL (null out-pointer), `2`/ENOENT (unknown arch). The blobs themselves are raw bytes — **no ELF header, no magic, no checksum** — so the IRAM image's byte 0 is already the on-device packed XEA2 vector table. This is the path libnrt's `encd_ncfw_init` walks to obtain the image it DMAs to the device.

**Job B — firmware-context JSON serializer.** `libncfw_ctx_log` @`0x1309` is the only non-trivial logic in the library. Given a parsed in-memory CC-context struct (built by libnrt's collectives layer, *not* by libncfw), it pretty-prints the entire structure as JSON into a 1 MB caller buffer. Because the serializer is the structural reflection of the on-device sequencer's context, recovering every `snprintf` key and struct dereference reconstructs the on-device CC-context schema — rings, meshes, hierarchical algos, kangaring peer rings, APB-broadcast DMA, NEFF host/device barriers, tsync, basic-block tables, TPB completion — **without the device**. It is a debug/telemetry dumper, never on the hot path; libnrt caches its function pointer (`libncfw_ctx_log_func` @`libnrt 0xc96d08`) for diagnostic dumps. Full tree in [Serializer Families](serializer-families.md).

The two jobs share only the **`5/12/20/28` arch key**: `get_image` and `ctx_log` use the identical four-way `switch`, which is why a single coretype enum (`nrtucode_coretype_t`, owned by libnrt) drives both the firmware selection and the context-dump selection.

---

## 2. The arch key — coretype 5/12/20/28

Both jobs are parameterized by one integer: the architecture generation, supplied by libnrt as the `nrtucode_coretype` value. The mapping is read directly out of the two switches and the serializer symbol names (`sunda_ncfw_ctx_log` @`0x1a12b`, `cayman_ncfw_ctx_log` @`0x32ed2`, `mariana_ncfw_ctx_log` @`0x4bc79`, `mariana_plus_ncfw_ctx_log` @`0x64a20`).

| `arch_id` (coretype) | Generation | Codename | Blob prefix | `ctx_log` dispatcher | Confidence |
|---|---|---|---|---|---|
| `5` | NeuronCore-v2 (trn1 / inf2) | sunda | `v2_ncfw_*` | `sunda_ncfw_ctx_log` @`0x1a12b` | HIGH (switch); MED (silicon name) |
| `12` | NeuronCore-v3 (trn2) | cayman | `v3_ncfw_*` | `cayman_ncfw_ctx_log` @`0x32ed2` | HIGH; MED |
| `20` | NeuronCore-v4 | mariana | `v4_ncfw_*` | `mariana_ncfw_ctx_log` @`0x4bc79` | HIGH; MED |
| `28` | NeuronCore-v4+ | mariana_plus | `v4_plus_ncfw_*` | `mariana_plus_ncfw_ctx_log` @`0x64a20` | HIGH; MED |
| else | — | — | — | return `22` (EINVAL) / `2` (ENOENT) | HIGH |

> **NOTE —** the codenames `sunda`/`cayman`/`mariana`/`mariana_plus` are recovered from the `ctx_log` symbol strings inside this binary and map by switch position to `v2/v3/v4/v4_plus`; the *silicon* product names (the `trn1`/`inf2`/`trn2` correspondence) live in libnrt's coretype table, not here, hence **MEDIUM** on the silicon column. The canonical reconciliation is [Coretype Numbering Reconciliation](../arch/coretype-numbering.md).

> **GOTCHA — the four serializer copies are clones, not arch-specialized logic.** Each of the 38 serializer families is duplicated byte-for-byte into four arch copies (IDA disambiguates them with `_0`/`_1`/`_2` suffixes; the sunda copy is the base). The clones dereference *identical* struct offsets; only the top-level context sizing differs — sunda places `neff`/`algo` at `ctx+0x3060`/`+0x30C0`, the other three at `ctx+0x4280`/`+0x42E0`, so v3/v4/v4+ contexts are ≥17 KB vs sunda's ~12.4 KB. A reimplementer who assumes the four arches have *different* leaf schemas will over-engineer: write one serializer, parameterize the two top-level offsets. (Leaf-offset identity across v3/v4/v4+ is **MEDIUM** — the clone bodies look identical but were not byte-diffed individually.)

---

## 3. Job A in one screen — the embedded payloads

The eight blobs are nm-`'r'` symbols in `.rodata`; each is sized by the adjacent `*_bin_size` u64 word. The provider hands out a pair per arch. This is a summary; the byte-level carve, entropy, and on-device image layout (the packed XEA2 vector table at IRAM byte 0, the DRAM exception/SoC-address tables) are owned by [Embedded Payloads](embedded-payloads.md) and disassembled in [The NCFW Sequencer](ncfw-sequencer.md).

| Blob (nm `'r'` sym) | Host off / VMA | Size (B) | `sha256[0:16]` | Role |
|---|---|---|---|---|
| `v2_ncfw_dram_bin` | `0x66a60` | 14,016 | `ca01951124e505b6` | DRAM data (v2/sunda) |
| `v2_ncfw_iram_bin` | `0x6a140` | 43,232 | `e379980b7ec3f2fe` | IRAM code (v2) — largest |
| `v3_ncfw_dram_bin` | `0x74a40` | 19,968 | `2418ab0f6350ce93` | DRAM data (v3/cayman) |
| `v3_ncfw_iram_bin` | `0x79860` | 19,392 | `d7bc8b814b03c1f0` | IRAM code (v3) |
| `v4_ncfw_dram_bin` | `0x7e440` | 19,968 | `1c3ac5f445865844` | DRAM data (v4/mariana) |
| `v4_ncfw_iram_bin` | `0x83260` | 19,488 | `ed8eed3429da3834` | IRAM code (v4) |
| `v4_plus_ncfw_dram_bin` | `0x87ea0` | 19,968 | `1c3ac5f445865844` | DRAM data — **== v4** (byte-identical) |
| `v4_plus_ncfw_iram_bin` | `0x8ccc0` | 19,488 | `abc4d4521dd857ab` | IRAM code (v4_plus) |

Two carve facts that bite a reimplementer: the **v4 and v4_plus DRAM blobs are byte-identical** (same `sha256` `1c3ac5f4…`) while their IRAM blobs differ — v4_plus is a CC-op handler-logic revision on the same boot framework (the v4↔v4_plus IRAM diff is confined to the `0x1190..0x4ab0` handler region). And the `v4_plus_ncfw_iram_bin_size` u64 word's *high* bytes overlap the SONAME tail that follows it in `.rodata`; the true length is the low dword `0x4c20` = 19,488 — read the size as a u32, not a u64, for that one symbol.

---

## 4. Job B in one screen — the serializer tree

The serializer is a fixed recursive descent of 38 distinct families, each cloned four times (152 helpers), under four arch dispatchers and the three public symbols — 181 functions total, confirmed against the metadata. Every node shares one signature, so the tree reads uniformly once the convention is known. Byte-level field offsets and the full call chain are in [Serializer Families](serializer-families.md).

### The uniform calling convention

```c
// Every one of the 38 leaf/node families (sunda base + 3 clones each):
int ncfw_log_<family>(const char *out_buf, int indent, const char *key, void *struct_ptr);
//   emits  <indent spaces> + (key ? "\"key\": {" : "{")   for an object   (or "[" for an array)
//   walks children / emits scalar fields
//   closes with "}," / "}"            indent grows +2 per nesting level
//   every write is bounds-checked vs  (0x100000 - strlen(out_buf))
//
// ncfw_log_addr @0x41c3 takes an EXTRA leading char arg = "emit trailing comma?":
int ncfw_log_addr(char trail_comma, const char *out, int indent, const char *key, uint64_t *p);
//   prints  <key>: "0x%016lX"      actual constant @0x65127 is `%s: "0x%016lX"\n` (key passed as %s, e.g. "soc_addr")
```

Truncation is handled by a per-arch global flag `ncfw_log_buffer_full` (four copies in `.bss` at `0x95029`/`2a`/`2b`/`2c`), set whenever an `snprintf` would overflow the 1 MB buffer; `ncfw_ctx_log` returns `28` (ENOSPC) if any copy is set, else `0`.

### The two parallel views

The single most important structural fact about the JSON is that the algorithm subtree appears **twice**, because the sequencer keeps a live runtime state and a static descriptor separately:

| View | Root family | Children | What it reflects | Row size |
|---|---|---|---|---|
| **runtime state** | `ncfw_log_algo_ctx` @`0x18cd2` | `ring_ctx` / `mesh_ctx` / `hierarchical_ctx` | live per-channel counters (recv_cnt, send_credit, run_state) | 16 B / channel |
| **static config** | `ncfw_log_algo_configs` @`0x1961c` | `ring_configs` / `mesh_configs` / `hierarchical_configs` | full descriptor (neighbors, 4 semas, kangaring, APB-bcast) | 148 B / channel |

So the same ring algorithm emits both a `"ring": {channel_list}` runtime row and a full 148-byte static channel descriptor — the latter is the primary reimplementation artifact, owned field-for-field by [The 148-Byte Ring Channel Descriptor](../collectives/channel-descriptor.md). The per-op scheduler descriptor (`cc_op_entry`, the packed bit-field + ring/mesh union that `ncfw_log_spad_ctrl_cc_op_entry` @`0x1840` dumps) is owned by [The cc_op_entry On-Device Collective ISA](../collectives/cc-op-isa.md).

> **NOTE —** the serializer is the only place the CC-context schema is observable on the host without the device. It does **not** define the on-device *meaning* of the scheduler (that runs in the Xtensa IRAM image and is partially opaque behind the sequencer TIE — see [The NCFW Sequencer](ncfw-sequencer.md)). libncfw merely *reflects* the struct; libnrt's collectives layer *builds* it ([Collective-Compute Architecture](../collectives/overview.md)).

### The 38 families by group

Rather than list all 152 cloned helpers, the table below gives the *shape* of the serializer space: the seven structural groups, how many distinct families each holds, and the representative sunda-copy entry point. Each family has exactly four arch copies (sunda base + cayman/mariana/mariana_plus clones), so the column total `38 × 4 = 152`. The per-family addresses, the recursive call chain, and the byte-level struct offsets are in [Serializer Families](serializer-families.md).

| Group | Families | Representative (sunda) | Reflects |
|---|---|---|---|
| Top / ctx | 3 | `ncfw_log_ctx` @`0x19c0e` | root `ncfw_ctx_top_sp` → configs / neff / algo |
| Algo — config view | 7 | `ncfw_log_algo_ring_channels_configs` @`0x6020` | static 148 B ring rows, neighbors, kring, mesh events; `hierarchical_configs` is a `__stub` |
| Algo — runtime view | 4 | `ncfw_log_algo_ring_ctx` @`0x16a00` | live 16 B ring rows, mesh `event_index`, hierarchical `run_state` |
| Spad-ctrl / cc-op | 3 | `ncfw_log_spad_ctrl_cc_op_entry` @`0x1840` | the packed per-op scheduler descriptor (ring/mesh union) |
| Addr helpers | 3 | `ncfw_log_addr` @`0x41c3` | `soc_addr` u64 formatting (incl. trail-comma variant) |
| NEFF barrier subtree | 9 | `ncfw_log_neff_device_barrier_config` @`0xfef5` | host/device barriers, step `m2s/s2m`, `tdrbp`, dma-sync-sema |
| Config / dev / basic-block / tsync | 9 | `ncfw_log_dev_configs` @`0xc371` | `tpb_id`/`seng_id`/`dev_id`, tsync, basic-block 4 B table |

> **QUIRK —** two families are present in all four arch copies but have **no caller** reachable from `ncfw_ctx_log` in the sunda trace: `ncfw_log_soc_addr` @`0x8a1b` and `ncfw_log_dma_reprogram_info` @`0x4571`. They are either dead/legacy or reached only through a non-sunda context layout (**LOW** — xref unresolved). The latter also carries a recovered firmware-side gap: the `.rodata` key run is `m2s_low` @`0x65137` / `m2s_high` @`0x6513f` / `s2m_low` @`0x65148`, with **no `s2m_high` key string anywhere in the image** (`strings libncfw.so | grep -c s2m_high` → `0`) — so the serializer emits an `m2s_low`/`m2s_high`/`s2m_low` triple where the four-field layout would expect a closing `s2m_high` (**HIGH** that the `s2m_high` key is absent; that this drops a real field is inferred).

---

## 5. The consumer boundary

libncfw is inert until libnrt loads it. The boundary is a `dlopen`/`dlsym` handshake with a version gate, fully owned by [Firmware Upload Path](upload-path.md); the essentials:

```text
libnrt encd_libncfw_init @0x251cc0
   dlopen("libncfw.so", RTLD_NOW)
   dlsym libncfw_get_version  →  assert == 2  (else "Incompatible libncfw.so version")
   dlsym libncfw_get_image    →  cache fptr @ libnrt bss 0xc96d10
   dlsym libncfw_ctx_log      →  cache fptr @ libnrt bss 0xc96d08
libnrt encd_ncfw_init @0x251eb0  →  encd_set_ncfw_ucode_bins
   fptr(coretype, ncfw_image_t*)         // = libncfw_get_image
     guard: assert libncfw_get_image_func != NULL
     err:   "Failed to get NCFW image. errno=%d coretype=%d"
   → IRAM bytes DMA'd to TPB IRAM ; DRAM bytes written to HBM, then patched by
     compose_ncfw_configs_for_barrier ; core released from reset via the DHAL kernel path
```

The split of responsibility is absolute: **libncfw produces bytes and JSON; libnrt and the kernel do everything physical.** The DMA-to-IRAM, the DRAM-to-HBM template patch (`compose_ncfw_configs_for_barrier`, which parameterizes the DRAM descriptor template with ring/mesh/barrier scheduling fields), and the reset-release through DHAL are all out of this part — they live in [Firmware Upload Path](upload-path.md) and the DHAL kernel pages.

---

## 6. Verification notes

> The two-job split, the arch switch, the blob layout, and the serializer census were all cross-checked against `nm`/`objdump`/`readelf` on the binary plus the IDA sidecars for `libncfw.so`:
> - **3 exports** confirmed `nm -D`: `libncfw_get_image` @`0x1179`, `libncfw_get_version` @`0x12fa` (`mov $0x2,%eax; ret`, verified `objdump`), `libncfw_ctx_log` @`0x1309`. Imports = `memset`/`snprintf`/`strlen`/`__stack_chk_fail`; `NEEDED libc.so.6` only; **no** `ioctl`/`open`/`write`/`mmap`/`dlopen` (`nm -D` + `readelf -d`) — the zero-device-I/O claim is byte-exact.
> - **Arch switch** `5/12/20/28`: the *same* `cmpl`-chain drives both `get_image` (`cmpl $0x1c`/`$0x14`/`$0x5`/`$0xc` → per-arch `lea`) and `ctx_log` (tail-call to `{sunda,cayman,mariana,mariana_plus}_ncfw_ctx_log` @`0x1a12b`/`0x32ed2`/`0x4bc79`/`0x64a20`); unknown arch returns `22`/`2`.
> - **8 blobs** carved from `.rodata` (`@0x65000`, VMA == file offset per `readelf -S`); sizes from the `*_bin_size` u64 words; `sha256` re-computed (v4 DRAM `1c3ac5f4…` == v4_plus DRAM; v4 IRAM `ed8eed34…` != v4_plus IRAM `abc4d452…`).
> - **181-function census** matches the sidecar metadata `total_functions = 181`; each of the 38 family names appears exactly four times (`nm`-verified).
>
> **[MED]** Silicon codenames (`sunda/cayman/mariana/mariana_plus` ↔ `v2/v3/v4/v4_plus` ↔ `trn1/inf2/trn2/…`) are inferred by switch position and corroborated by `get_image`; the authoritative table lives in libnrt, not this binary. Leaf-descriptor offset identity across v3/v4/v4+ is asserted from clone-body inspection, not an exhaustive byte-diff.

---

## Part X sub-pages

| Page | Owns | Anchor |
|---|---|---|
| [The Carrier Library (libncfw.so)](carrier-library.md) | the host x86-64 object: 3 exports, `get_image` switch, version gate, ELF/SONAME/build-id, zero-device-I/O proof | `0x1179`/`0x12fa`/`0x1309` |
| [Embedded Payloads (8 Xtensa Blobs)](embedded-payloads.md) | the 8 `.rodata` blobs: carve offsets, sizes, digests, entropy, v4≡v4_plus DRAM, on-device IRAM/DRAM layout | `0x66a60`..`0x918e0` |
| [Serializer Families (per-arch CC-context dumpers)](serializer-families.md) | the 38×4 JSON tree, `(out,indent,key,ptr)` convention, recovered CC-context schema, the two parallel views | `0x1309` → 152 helpers |
| [The NCFW Sequencer (Xtensa LX Disassembly)](ncfw-sequencer.md) | the on-device firmware: reset vector, boot, idle dispatcher, exception table, per-arch deltas, opaque TIE | IRAM `+0x0` `j 0x1f8` … |
| [Firmware Upload Path (DKMS → Device DRAM)](upload-path.md) | the libnrt↔libncfw boundary: `dlopen`/`dlsym`, version-2 gate, DMA-to-IRAM, DRAM-to-HBM, reset-release | `encd_ncfw_init` @`0x251eb0` |
| *(this page)* | the map: two jobs, the `5/12/20/28` key, the three-Xtensa-core distinction | — |

---

## Related Components

| Name | Relationship |
|---|---|
| `libnrt.so` (collectives) | the consumer: `dlopen`s libncfw, calls `get_image`, builds the CC-context that `ctx_log` reflects |
| `libnrtucode_extisa.so` (Part XI) | the *sibling* firmware carrier — GPSIMD/Q7 Vision-Q7 microcode, a different Xtensa TIE config |
| `compose_ncfw_configs_for_barrier` (libnrt) | patches the DRAM descriptor template before HBM upload — the producer of the fields `ctx_log` dumps |
| DHAL kernel path (Part III) | performs the DMA-to-IRAM and reset-release that libncfw cannot do itself |

## Cross-References

**Part X siblings (this subsystem)**
- [The Carrier Library (libncfw.so)](carrier-library.md) — the 3-export host object and `get_image` switch
- [Embedded Payloads (8 Xtensa Blobs)](embedded-payloads.md) — the 8 `.rodata` firmware blobs, byte-level
- [Serializer Families (per-arch CC-context dumpers)](serializer-families.md) — the 38×4 JSON serializer tree
- [The NCFW Sequencer (Xtensa LX Disassembly)](ncfw-sequencer.md) — the on-device sequencer firmware disassembly
- [Firmware Upload Path (DKMS → Device DRAM)](upload-path.md) — the `dlopen` boundary and DMA/reset path

**Consumers and the CC-context schema (Part IX)**
- [Overview: the Collective-Compute Architecture](../collectives/overview.md) — the consumer that builds the CC-context libncfw reflects
- [The cc_op_entry On-Device Collective ISA](../collectives/cc-op-isa.md) — the per-op scheduler descriptor `ncfw_log_spad_ctrl_cc_op_entry` dumps
- [The 148-Byte Ring Channel Descriptor](../collectives/channel-descriptor.md) — the static ring-channel config row, field-for-field

**Sibling on-device engine and apparatus**
- [Overview: the Q7 GPSIMD Engine](../gpsimd/overview.md) — the *other* on-device Xtensa engine (Vision-Q7), a different TIE config
- [Tensilica Xtensa and Vision-Q7 Identification](../gpsimd/xtensa-vision-q7.md) — the Xtensa-LX lineage proof for both cores
- [FW-IO MiscRAM Mailbox](../kernel/fw-io.md) — the third Xtensa-family role: the Q7 management / mailbox path
- [Coretype Numbering Reconciliation](../arch/coretype-numbering.md) — the canonical `5/12/20/28` ↔ silicon mapping
- [back to index](../index.md)
