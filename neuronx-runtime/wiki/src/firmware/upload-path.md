# Firmware Upload Path (DKMS → Device DRAM)

> *All addresses on this page apply to two binaries from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`: the consumer `libnrt.so` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, **not** stripped — full C++ symbols + DWARF, `.text`/`.rodata` VMA == file offset; emitter TU `/opt/workspace/KaenaRuntime/tdrv/encd.c`) and the carrier `libncfw.so` (build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`, SONAME `libncfw.so.2.31.1.0.cf13a49f`, `.rodata` @ `0x65000` VMA == file offset). The QUIRK's byte-identity check additionally touches the sibling carrier `libnrtucode_extisa.so` (build-id `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`, stripped, `.rodata` @ `0xb000`). Other versions will differ.*
>
> *Evidence grade: **Confirmed (symbol- and byte-anchored)** — the `dlopen`/`dlsym`/version-2 handshake, the `0xc96d10`/`0xc96d08` fptr cache, the `encd_ncfw_init` `call *%rax` get-image site, and the per-arch device-load callees are read straight from `nm`/`objdump`/`readelf` on `libnrt.so`; the host-image≡v2-prologue tie is `cmp`-proven this pass across `libncfw.so` and `libnrtucode_extisa.so`. The exact DMA-descriptor algebra and the DHAL reset-release are **boundary edges** (owned elsewhere), summarized not re-derived. · Part X — Collectives Firmware (libncfw) · [back to index](../index.md)*

## Abstract

The NCFW sequencer firmware reaches silicon by a path that is entirely libnrt's, not the carrier's. `libncfw.so` is a passive provider — it `dlopen`s nothing, makes zero syscalls, and hands out raw `.rodata` pointers ([The Carrier Library](carrier-library.md)). Everything physical happens one layer up, in libnrt's device-resident collectives driver (`tdrv/encd.c`, the `encd_*` floor). The cleanest familiar frame is a **two-stage firmware loader**: a *bring-up* stage that `dlopen`s the blob store and gates its ABI version, and a *per-NeuronCore* stage that fetches the matching `{IRAM, DRAM}` image by architecture ID and DMAs it into the device. This page owns that path end to end — the boundary the carrier never crosses.

The path is two functions. **`encd_libncfw_init` @`0x251cc0`** runs once: it `dlopen`s `libncfw.so` (`RTLD_NOW`), `dlsym`s `libncfw_get_version` and asserts the result equals `2` (the carrier-rejection gate — a mismatch `dlclose`s and bails), then caches the `libncfw_get_image` and `libncfw_ctx_log` function pointers in libnrt `.bss` at `0xc96d10` and `0xc96d08`. **`encd_ncfw_init` @`0x251eb0`** runs per top-SP (the on-device sequencer-processor engine): it reads the silicon's coretype (`encd_get_coretype`), null-guards the cached `libncfw_get_image_func`, **`call *%rax`s it @`0x252335`** to fill a stack-resident `ncfw_image_t` `{&iram, iram_size, &dram, dram_size}`, then drives the device — `aws_hal_sp_topsp_init`, `encd_ncfw_configure_device_init` (the IRAM/DRAM device load), and `notification_init`. The IRAM half is DMA'd into the TPB sequencer's instruction RAM; the DRAM half is written to HBM and patched by `barrier_composer::compose_ncfw_configs_for_barrier` @`0x13d1e0`; the core is released from reset through the DHAL kernel path.

This is a *different* path from the [FW-IO MiscRAM mailbox](../kernel/fw-io.md), and conflating the two is the easiest mistake here. FW-IO is a kernel↔management-CPU request/response wire protocol over a BAR0 aperture — no firmware ever travels through it. This page is the *Xtensa-sequencer upload*: a one-way host→device DMA of executable IRAM and a DRAM data template, choreographed entirely from userspace libnrt, with the kernel touched only for the reset-release. The single most striking fact recovered this pass is that the **same SUNDA sequencer boot framework is shipped twice** — byte-identically over its boot scaffold — in two different carriers (`libncfw.so` and `libnrtucode_extisa.so`), proving the two firmware paths share a common on-device boot framework even though they carry different handler logic; the QUIRK pins the `cmp` evidence.

For reimplementation, the contract is:

- **The bring-up handshake** — `dlopen(libncfw.so, RTLD_NOW)`, `dlsym` three symbols, the `get_version == 2` gate, and the two `.bss` fptr cache slots (`0xc96d10` get-image, `0xc96d08` ctx-log). A version mismatch must `dlclose` and refuse, not proceed.
- **The per-top-SP fetch-and-load** — read coretype → null-guard the cached fptr → `call` it to fill a four-word `ncfw_image_t` → DMA the IRAM into TPB IRAM and the DRAM into HBM. The image words are pointers *into the carrier's `.rodata`*, valid only while it stays mapped.
- **The DRAM template patch** — the DRAM half is not uploaded verbatim; `compose_ncfw_configs_for_barrier` parameterizes the descriptor template (ring/mesh/barrier scheduling fields) before the HBM write. The IRAM half *is* uploaded verbatim.
- **The boundary** — the DMA-descriptor algebra (`encd_get_model_switch_desc_info`), the al_udma/vring HAL, and the DHAL reset-release are out of this path; this page pins where they are called, not how they work.

| | |
|---|---|
| **Consumer** | `libnrt.so`, build-id `8bb57aba…`; emitter TU `tdrv/encd.c` (all `encd_*` `local` `t` symbols) |
| **Carrier** | `libncfw.so`, build-id `a98f8e1c…`, SONAME `libncfw.so.2.31.1.0.cf13a49f` (the blob store) |
| **Bring-up** | `encd_libncfw_init` @`0x251cc0` — `dlopen` + 3× `dlsym` + version-2 gate |
| **Per-top-SP load** | `encd_ncfw_init` @`0x251eb0` — coretype → `get_image` fptr call @`0x252335` → device load |
| **Cached fptrs (`.bss`)** | `libncfw_get_image_func` @`0xc96d10` · `libncfw_ctx_log_func` @`0xc96d08` |
| **Version gate** | `dlsym libncfw_get_version` → `call *%rbp` → `cmp $0x2,%eax` @`0x251d11`; mismatch → `dlclose` |
| **Image descriptor** | `ncfw_image_t` (32 B) = `{ram_image iram @+0, ram_image dram @+16}`; each = `{ptr, size}` |
| **DRAM patch** | `barrier_composer::compose_ncfw_configs_for_barrier` @`0x13d1e0` (before HBM write) |
| **Device-load callees** | `aws_hal_sp_topsp_init` @`0x458090` · `encd_ncfw_configure_device_init` @`0x230c70` · `notification_init` @`0x2fed70` |
| **Reset-release** | DHAL kernel path (BOUNDARY — not this binary) |

> **NOTE — this is the Xtensa-sequencer upload, NOT the Q7 FW-IO mailbox.** Two device-firmware-adjacent paths live in this runtime and a reimplementer must keep them apart. **(1) This path** — `encd_libncfw_init` / `encd_ncfw_init` in libnrt userspace — `dlopen`s the carrier, fetches a per-arch `{IRAM, DRAM}` image, and **DMAs executable Xtensa code into the TPB sequencer's IRAM** plus a data template into HBM. It is a one-way bulk upload of firmware, choreographed from userspace, with the kernel touched only to release reset. **(2) The [FW-IO MiscRAM mailbox](../kernel/fw-io.md)** — `neuron_fw_io.c` in the DKMS kernel driver — is a CRC32C-framed *request/response wire protocol* to the device's Q7 **management** CPU over a BAR0 aperture; it carries register reads, telemetry, power, and reset *commands* — **never firmware bytes**, and never DMA. Same silicon, opposite directions: this page *pushes a program onto* the sequencer core; FW-IO *asks questions of* the management core. Nothing on this page disassembles firmware (owned by [The NCFW Sequencer](ncfw-sequencer.md)) and nothing re-derives the mailbox (owned by [FW-IO MiscRAM Mailbox](../kernel/fw-io.md)).

---

## 1. Stage 1 — Carrier Bring-Up (`encd_libncfw_init`)

### Purpose

Before any image can be fetched, libnrt must locate the carrier, prove its ABI matches, and cache the three entry points. `encd_libncfw_init` @`0x251cc0` does exactly that, once per process, and is the *only* place in the entire `encd_*` floor that crosses a shared-object boundary — every other `encd_*` callee is intra-libnrt ([encd Overview](../collectives/encd-overview.md)). The carrier cannot do this for itself: `libncfw.so` has no `dlopen` in its own import table ([The Carrier Library](carrier-library.md)), so the load is strictly the consumer's.

### Entry Point

```text
nrt_init / collectives bring-up
  └─ encd_libncfw_init @0x251cc0
       dlopen("libncfw.so", RTLD_NOW)              @0x251cd9  call dlopen@plt
       dlsym(handle, "libncfw_get_version")        @0x251cf7  call dlsym@plt
         call *%rbp  (= get_version)               @0x251d0f
         cmp $0x2,%eax ; jne reject                @0x251d11
       dlsym(handle, "libncfw_get_image")          @0x251d7d
         mov %rbp, 0xc96d10  (libncfw_get_image_func)   @0x251dda
       dlsym(handle, "libncfw_ctx_log")            @0x251de4
         mov %rbp, 0xc96d08  (libncfw_ctx_log_func)     @0x251df6
     reject: dlclose(handle) ; movq $0x0,0xc96d10  @0x251d5a / 0x251d4c
```

### Algorithm

```c
// Models encd_libncfw_init @0x251cc0 (verified objdump). The RTLD_NOW flag is the
// $0x2 in %esi @0x251cc2; the SAME $0x2 reappears as the version compare @0x251d11
// (distinct uses — do not conflate the dlopen flag with the ABI version constant).
int encd_libncfw_init(void):
    void *h = dlopen("libncfw.so", RTLD_NOW);          // 0x251cd9  str "libncfw.so" @0x845425
    if (h == NULL)                                     // 0x251ce1  je -> dlerror path
        return log_error("dlopen failed to load %s: %s", dlerror());   // 0x251e08

    // --- ABI version gate ---
    get_version = dlsym(h, "libncfw_get_version");     // 0x251cf7  str @0x84544e
    if (dlerror())                          goto reject_log;            // 0x251cff/0x251d07
    if (get_version() != 2) {                          // call *%rbp 0x251d0f ; cmp $0x2 0x251d11
        log_error("Incompatible %s version. Expected: %d, Found: %d"); //   str in .rodata
    reject:
        dlclose(h);                                    // 0x251d5a  call dlclose@plt
        libncfw_get_image_func = NULL;                 // 0x251d4c  movq $0,0xc96d10
        return NRT_FAILURE;
    }

    // --- cache the two working fptrs in libnrt .bss ---
    libncfw_get_image_func = dlsym(h, "libncfw_get_image"); // 0x251d7d ; str @0x845462
    if (dlerror())                          goto reject;                // 0x251d8d
    //                                      0x251dda  mov %rbp,0xc96d10
    libncfw_ctx_log_func   = dlsym(h, "libncfw_ctx_log");   // 0x251de4 ; str @0x845474
    if (dlerror())                          goto reject;                // 0x251df4
    //                                      0x251df6  mov %rbp,0xc96d08
    return 0;                                           // 0x251d64  success
```

Two facts a reimplementer must reproduce. **The version gate is `dlclose`-and-refuse, not warn-and-continue:** a carrier whose `get_version` is not `2` is unmapped (`0x251d5a`) and the get-image fptr is zeroed (`0x251d4c`), so a later `encd_ncfw_init` hits the null-guard rather than calling a stale pointer. **The handle itself is discarded after the three `dlsym`s** — libnrt keeps the *function pointers*, not the `dlopen` handle, so the carrier stays resident for the life of the process and the `.rodata` pointers `get_image` returns remain valid (the carrier is never `dlclose`d on the success path).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_libncfw_init` | `0x251cc0` | the `dlopen`/`dlsym`/version-2 handshake; sole `.so` boundary in the `encd_*` floor | CERTAIN |
| `libncfw_get_image_func` (`.bss`) | `0xc96d10` | cached `get_image` fptr; null-checked by `encd_ncfw_init` | CERTAIN |
| `libncfw_ctx_log_func` (`.bss`) | `0xc96d08` | cached `ctx_log` fptr; used only by the crash-dump path | CERTAIN |
| `libncfw_get_version` (in carrier) | `libncfw 0x12fa` | `mov $0x2; ret` — the value the gate asserts | CERTAIN |

### Considerations

The handshake `dlsym`s three symbols but the page's two stages use only two of them. `libncfw_ctx_log_func` @`0xc96d08` is *not* on the upload path at all — it is the JSON crash-dump serializer, called from `encd_ncfw_log_topsp_context` @`0x2527b0` when a top-SP faults ([The Carrier Library §4](carrier-library.md)). It is cached here because the handshake is the one place the carrier is resolved; a reimplementer should resolve all three at bring-up but understand that only `get_image` feeds the upload.

---

## 2. Stage 2 — Per-Top-SP Fetch and Device Load (`encd_ncfw_init`)

### Purpose

`encd_ncfw_init` @`0x251eb0` is the per-engine bring-up that actually puts firmware on silicon. For each top-SP (the on-device sequencer-processor engine that runs the NCFW image), it computes that engine's BAR0 register windows, fetches the architecture-matched `{IRAM, DRAM}` image through the cached provider fptr, hands the image to the HAL device-init, and wires the engine's notification queue. The IRAM bytes become the program the sequencer fetches the instant reset deasserts; the DRAM bytes become the exception-dispatch/SoC-address table the sequencer reads from HBM ([Embedded Payloads §3](embedded-payloads.md)).

### Entry Point

```text
tdrv_init / collectives device bring-up
  └─ encd_ncfw_init @0x251eb0   (per top-SP)
       encd_get_coretype                              @0x2522ee  -> arch id (5/12/20/28)
       mov 0xc96d10,%rax  (libncfw_get_image_func)    @0x2522fb
       test %rax,%rax ; je fail                        @0x252316  null-guard
       lea 0xa0(%rsp),%r12  (ncfw_image_t out)         @0x252327
       call *%rax   (= libncfw_get_image)              @0x252335  *** THE FETCH ***
       test %eax,%eax ; jne fail                        @0x252337  -> "Failed to set ... ucode"
       aws_hal_sp_topsp_init(&iram, &dram, …)          @0x25239b
       encd_ncfw_configure_device_init                 @0x2523c8  (IRAM->TPB-IRAM, DRAM->HBM DMA)
       notification_init("notification-mla%d-topsp")   @0x252406
```

### Algorithm

```c
// Models encd_ncfw_init @0x251eb0 (verified objdump). One top-SP per call; the
// register-window math and the per-arch BAR0 offsets are elided (boundary, §3).
// ncfw_image_t is the 32-byte {iram{ptr,size}, dram{ptr,size}} struct (encd-18 §3).
int encd_ncfw_init(encd_context *ctx, int topsp_idx):
    db_physical_core_get_mla_and_tpb(ctx, topsp_idx, &mla, &tpb);   // device handles
    // … compute instr-fetch-queue head/tail/tail-inc BAR0 windows (per-arch) …

    int coretype = encd_get_coretype(ctx);             // 0x2522ee  -> 5 | 12 | 20 | 28
    if (coretype < 0) goto fail;                        // 0x2522f5

    // --- the fetch: call the cached provider fptr ---
    get_image = libncfw_get_image_func;                // 0x2522fb  mov 0xc96d10,%rax
    assert(get_image != NULL);                          // 0x252316  je 0x2526bf
                                                        //   ("libncfw_get_image_func != NULL" @0x809a38)
    ncfw_image_t img;                                   // stack @0xa0(%rsp), %r12
    int rc = get_image(coretype, &img);                // 0x252335  call *%rax
    if (rc != 0)                                        // 0x252337
        return log_error("Failed to set NeuronCollectivesFirmware ucode"
                         " / Failed to get NCFW image. errno=%d coretype=%d", rc, coretype); // 0x2525d0

    // img = { iram = {ptr -> libncfw .rodata, size},     // IRAM code  (verbatim upload)
    //         dram = {ptr -> libncfw .rodata, size} }    // DRAM table (patched before upload)

    // --- device load (HAL): IRAM -> TPB IRAM, DRAM -> HBM ---
    rc = aws_hal_sp_topsp_init(&img.iram, &img.dram, …);    // 0x25239b
    if (rc) goto fail;                                  // 0x2523a2
    rc = encd_ncfw_configure_device_init(ctx, topsp_idx);  // 0x2523c8  the actual DMA program
    if (rc) goto fail;                                  // 0x2523d2

    // --- per-top-SP notification queue ---
    snprintf(name, "notification-mla%d-topsp", mla);    // 0x2523b3  fmt @0x8454a7
    return notification_init(name, …);                  // 0x252406
fail:
    return NRT_FAILURE;
```

### The image descriptor crosses the boundary by value

The `get_image` call fills a 32-byte `ncfw_image_t` on `encd_ncfw_init`'s stack (`lea 0xa0(%rsp),%r12` @`0x252327`). Its shape is two `ram_image_t` halves (`encd-18 §3`):

```c
// ncfw_image_t (32 B) — filled by libncfw_get_image, consumed by aws_hal_sp_topsp_init
struct ram_image_t  { void *ram_image; uint64_t ram_image_size; };  // 16 B
struct ncfw_image_t { ram_image_t iram;     // +0   IRAM code  (libncfw .rodata ptr + size)
                      ram_image_t dram; };   // +16  DRAM table (libncfw .rodata ptr + size)
```

> **GOTCHA — the image words are pointers into the carrier's `.rodata`, not copies.** `libncfw_get_image` returns *addresses into `libncfw.so`'s `.rodata`* with no allocation and no copy ([The Carrier Library §2](carrier-library.md)). `img.iram.ram_image` therefore dereferences valid memory only while `libncfw.so` stays mapped — which it does, because Stage 1 never `dlclose`s on success. A reimplementer who `dlclose`s the carrier after caching the fptr, or who frees the descriptor expecting owned memory, dereferences unmapped `.rodata`. The DMA/`memcpy` in `aws_hal_sp_topsp_init` / `encd_ncfw_configure_device_init` is the point at which the bytes are actually copied out of the carrier and into the device.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_ncfw_init` | `0x251eb0` | per-top-SP fetch-and-load driver | HIGH |
| `encd_get_coretype` | `0x230be0` | arch id `5/12/20/28` for the provider switch | HIGH |
| `aws_hal_sp_topsp_init` | `0x458090` | HAL: hands the `{iram,dram}` image to the device | HIGH |
| `encd_ncfw_configure_device_init` | `0x230c70` | the device-load: drives the IRAM→TPB-IRAM / DRAM→HBM DMA + init signal | HIGH |
| `notification_init` | `0x2fed70` | per-top-SP notification queue (`"notification-mla%d-topsp"`) | HIGH |
| `encd_get_model_switch_desc_info` | `0x253c90` | builds the IRAM/DRAM→device DMA descriptors (config-chunk→`ncfw_ctx_addr`, spad→sram/iram) | MEDIUM |

---

## 3. The DRAM Template Patch and Reset Release

### Purpose

The IRAM half is uploaded byte-for-byte, but the DRAM half is not: it is a *template* with the exception-dispatch table and a zero-filled BSS region ([Embedded Payloads §3](embedded-payloads.md)) that must be parameterized with this collective's scheduling fields before it is written to HBM. That patch, and the final reset-release that lets the sequencer start fetching the freshly-loaded IRAM, are the two steps that turn a copied image into a running program.

### The patch

```text
img.dram (from get_image)  ─── exception table + SoC-addr grid + zero BSS template
        │
        ▼  barrier_composer::compose_ncfw_configs_for_barrier @0x13d1e0
   patched DRAM  ─── ring/mesh/barrier scheduling fields written into the template
        │           (the same fields libncfw_ctx_log later reflects as JSON)
        ▼  DMA -> HBM  (via encd_ncfw_configure_device_init / model-switch desc path)
   device DRAM (HBM)
```

`compose_ncfw_configs_for_barrier` @`0x13d1e0` is the producer of the very fields the carrier's `ctx_log` serializer dumps for debug ([Serializer Families](serializer-families.md)) — the runtime CC-context is built here, patched into the DRAM template, and DMA'd to HBM. A reimplementer reproduces this as: fetch the DRAM template → overlay the per-collective ring/mesh/barrier descriptor → DMA the result, *not* the raw template, to HBM. The IRAM path has no analogous patch — it is uploaded verbatim.

### Reset release (boundary)

After both halves are resident — IRAM in TPB instruction RAM, patched DRAM in HBM — the sequencer core is released from reset through the DHAL kernel path (`neuron_reset.c` / `neuron_dhal_v{2,3,4}.c`), at which point the core begins fetching at IRAM byte 0 (the packed XEA2 vector table, [The NCFW Sequencer §2](ncfw-sequencer.md)). The reset-release is **not** in `libnrt`'s `encd_*` floor and is **not** the FW-IO reset command (which resets the *device/TPB*, a different mechanism); it is the DHAL core-release path and is owned by the Part III kernel pages. This page pins only that it is the terminal step.

### Considerations

The split of responsibility is absolute and mirrors the carrier's passivity: **libncfw produces bytes; libnrt fetches, patches, and DMAs; the kernel releases reset.** The carrier provably cannot do any of the latter three — it has no `dlopen`, no syscall wrapper, no DMA engine ([The Carrier Library QUIRK](carrier-library.md)). The DMA-descriptor algebra (how many 16 KiB-granular chunks the IRAM and ctrl-SRAM need, the per-qbundle descriptor counts) is `encd_get_num_descs_model_switch` @`0x253ac0` / `encd_get_model_switch_desc_info` @`0x253c90`, owned by [encd Overview](../collectives/encd-overview.md); this page does not re-derive it.

---

## 4. The Host-Image ≡ v2 Prologue Tie

> **QUIRK — the same SUNDA sequencer boot framework is shipped twice, byte-identically over its boot scaffold, in two different carriers.** A host image at `libnrtucode_extisa.so` `.rodata` offset `0x917010` is **byte-identical to the `libncfw.so` v2 (SUNDA) IRAM image over the entire reset→vector→boot scaffold**, and this is the concrete fact that links the two firmware-upload paths: both carriers embed the *same* Xtensa-LX sequencer boot framework for the v2 generation, then diverge only in the CC-op handler bodies. The `cmp` evidence, re-run this pass:
>
> ```text
> # both carve from .rodata with VMA == file offset
> dd if=libncfw.so            skip=0x6a140  count=0xa8e0  > v2_iram_libncfw.bin   # v2_ncfw_iram_bin, 43232 B
> dd if=libnrtucode_extisa.so skip=0x917010 count=0xa8e0  > v2_iram_extisa.bin    # same length window
>
> cmp v2_iram_libncfw.bin v2_iram_extisa.bin
>   -> differ: byte 164, line 1        # first divergence at 0-based offset 0xa3
>
> # identical runs >= 16 B (the whole boot scaffold matches):
> #   0x0000 .. 0x00a3   reset vector + packed XEA2 window/exception table  (163 B, IDENTICAL)
> #   0x00a8 .. 0x01ac   vector tail + exception prologues                  (260 B, IDENTICAL)
> #   0x01ad .. 0x01ed   boot-stage entry region                           ( 64 B, IDENTICAL)
> #   0x01f8 .. 0x1007   reset body + cache loops + MMU + boot-stage-2 head (3599 B, IDENTICAL)
> #   0x1007 .. 0xa8e0   CC-op handler bodies                              (DIFFER — ~89% of bytes)
> ```
>
> The shared prefix is exactly the per-arch boot scaffold the sequencer page documents: reset slot A `06 76 00` → `j 0x1dc` (the **v2/sunda** reset, *not* the v4 `06 7d 00`), the window-overflow/underflow vectors, the user-exception prologue at `+0x6c`, and the boot path up to the `call0 0x1018` stage-2 entry — all identical. The two diverge at `0xa3` (a cache-region patch) and wholesale past `0x1007` (the handler logic). Overall only **10.9 % (4703/43232 B) is identical**, and the two images have *different* `sha256` (`e379980b…` libncfw vs `037ec003…` extisa) — so this is a **shared boot framework with distinct handler bodies**, not one duplicated image.

> **CORRECTION (UPLOAD-01) —** an early seed framing described the host image @`0x917010` as "byte-identical to the libncfw v2 IRAM prologue" and invited stating it as a *full-image* identity. The `cmp` above shows the identity is **scaffold-only**: bytes `0x0..0xa3` (reset/vectors) and the boot band `0x1f8..0x1007` match byte-for-byte, but the images differ from `0xa3` onward in the handler region and carry different `sha256`. The accurate claim is "identical reset/vector/boot scaffold (the SUNDA sequencer boot framework), divergent handler bodies" — the prologue tie is real and proves the shared framework; a whole-image identity claim would be wrong. CONFIDENCE: **HIGH** (`cmp`/`sha256` re-run this pass on both carriers).

### Why the tie matters for reimplementation

The two carriers serve different on-device cores — `libncfw.so` the NCFW collective sequencer (this upload path), `libnrtucode_extisa.so` the GPSIMD/Q7 vector pool ([Part XI](../gpsimd/overview.md)) — yet they share the v2 sequencer boot scaffold verbatim. The practical consequence is that a reimplementer building either upload path can lift one boot framework (reset vector, window/exception vectors, cache loops, MMU program, stage-2 entry) and parameterize only the handler region per role and per arch. It also explains why the entropy/fingerprint checks ([Embedded Payloads](embedded-payloads.md)) key on the *reset prologue*: that prologue is the stable, cross-carrier identity, while the handler bytes are the per-build revision surface.

---

## 5. The Two Paths, Side by Side

The single most important orientation on this page is the contrast with FW-IO. Both touch device firmware-adjacent state, but they are opposite mechanisms in opposite directions.

| Dimension | NCFW upload (this page) | FW-IO mailbox ([kernel/fw-io](../kernel/fw-io.md)) |
|---|---|---|
| Where it runs | libnrt **userspace** (`tdrv/encd.c`) | DKMS **kernel** driver (`neuron_fw_io.c`) |
| Target core | NCFW collective **sequencer** (Xtensa LX) | Q7 **management** CPU |
| Direction | host → device, one-way bulk | request ↔ response, bidirectional |
| Carries | executable IRAM code + DRAM data template | register reads, telemetry, power, reset commands — **no firmware** |
| Transport | H2T DMA into TPB IRAM / HBM | MMIO into a BAR0 MiscRAM aperture + doorbell |
| Framing | none — raw `{ptr, len}` images | CRC32C-framed `{seq, cmd, size}` messages |
| Trigger | `encd_ncfw_init` per top-SP | doorbell `TRIGGER 0x800` + sequence-number poll |
| Reset role | *consumes* a reset-release at the end (DHAL) | *issues* a device/TPB reset command (`RESET 0x1ec`) |

> **GOTCHA — "reset" appears in both paths and means different things.** This upload path *ends* with a sequencer-core reset-**release** (DHAL core-release, so the loaded IRAM starts executing). FW-IO *issues* a device/TPB reset **command** through its `RESET` MiscRAM register (`fw_io_initiate_reset`, [kernel/fw-io §2](../kernel/fw-io.md)). A reimplementer wiring "reset" must not cross them: the upload needs a per-core release after DMA; FW-IO's reset is a whole-device/TPB command to the management CPU. They are unrelated registers and unrelated cores.

---

## Related Components

| Name | Relationship |
|---|---|
| `libncfw.so` (`libncfw_get_image`) | the carrier this path `dlopen`s and fetches images from; pure passive provider |
| `libnrtucode_extisa.so` | the *sibling* carrier that ships the byte-identical SUNDA boot scaffold (§4 QUIRK) |
| `barrier_composer::compose_ncfw_configs_for_barrier` @`0x13d1e0` | patches the DRAM template before HBM upload — producer of the fields `ctx_log` dumps |
| `aws_hal_sp_topsp_init` / `encd_ncfw_configure_device_init` | the HAL/device-init that performs the actual IRAM→TPB-IRAM / DRAM→HBM DMA |
| DHAL kernel reset path (Part III) | releases the sequencer core from reset after the image is resident |
| FW-IO mailbox (`neuron_fw_io.c`) | the *contrasting* path — kernel↔management-CPU wire protocol, no firmware transfer |

## Cross-References

**Part X siblings (this subsystem)**
- [Collectives Firmware — Section Map](overview.md) — the two-job carrier map and the three-Xtensa-core distinction
- [The Carrier Library (libncfw.so)](carrier-library.md) — the provider ABI this path calls: `get_image` switch, version-2 gate, zero-device-I/O proof
- [Embedded Payloads (8 Xtensa Blobs)](embedded-payloads.md) — the IRAM/DRAM blobs this path uploads; carve, sizes, the RAW format invariant
- [The NCFW Sequencer (Xtensa LX Disassembly)](ncfw-sequencer.md) — what the uploaded IRAM bytes do once the core is released; the reset vector at byte 0

**The contrasting path and the composer floor**
- [FW-IO MiscRAM Mailbox](../kernel/fw-io.md) — the SEPARATE Q7 management-CPU mailbox; this page contrasts it, does not re-derive it
- [encd: the Device-Resident Descriptor Emitter](../collectives/encd-overview.md) — the `encd_*` floor that owns the DMA-descriptor algebra summarized here
- [Coretype Numbering Reconciliation](../arch/coretype-numbering.md) — the `5/12/20/28` arch key the fetch switches on
- [back to index](../index.md)
