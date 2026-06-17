# The Q7 GPSIMD Engine — Part-XI Map

> **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrtucode_extisa.so` — host ELF64 x86-64 DYN, stripped, 9,656,488 B, build-id `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`. `.rodata` VMA == file offset, so every `0x…` offset on this page is a host file offset unless noted. The on-device ISA config is the Cadence/Tensilica `Xm_ncore2gp` "Cairo" build in `aws-neuronx-gpsimd-tools 0.21.0.0-bc9b5fad5`.
> *Evidence grade: **Confirmed (byte-anchored)** — the 52 exports (`nm -D`, twice), the 13 embedded Xtensa ELF32 blobs (14 `\x7fELF` magics), the SUNDA opcode manifest, and the provider→loader→device flow are all cross-checked against the IDA Hex-Rays decompile of `libnrtucode_extisa.so`. · Part XI — GPSIMD / Q7 Microcode & ISA · [back to index](../index.md)*

## Abstract

This page is the **map of the GPSIMD / Q7 microcode stack** as reconstructed from `libnrtucode_extisa.so` and the on-disk `ncore2gp` config. The GPSIMD "pool" engine is a **vector co-processor that is programmed per-custom-op by microcode**: the host never executes a pool kernel directly. Instead, a host-side **provider library** (`libnrtucode_extisa.so`) ships the device code — 13 Tensilica Xtensa ELF32 microcode blobs plus a SUNDA opcode-manifest JSON — together with a 52-function `nrtucode_*` C-API that indexes, prelinks, and serializes that code into a device push stream. Think of it the way a JIT ships a code cache: the engine is fixed silicon, the *programs* are the cataloged pool kernels, and the provider is the container + loader that selects the right blob for the running silicon and the requested opcode.

The engine itself is a **Cadence Tensilica Vision-Q7** — an Xtensa LX scalar core with the IVP (Instruction Vision Processing) 512-bit vector extension, in the AWS `Xm_ncore2gp` "Cairo" config. The firmware self-labels it `"Q7"` in its own diagnostics (`"P%i: Q7: rdma_desc_gen [%s] Start"` @ host off `0x58f71`). Two consumers drive the provider: **`libnrt.so`'s `tdrv/ucode.c`** is the runtime consumer — it `dlopen`s the provider, `dlsym`s a fixed 30-entry subset of the 52 exports, asserts the provider API level `== 3`, and drives init → load → query; the **compiler/assembler/disassembler** is the second consumer, using the remaining 22 exports (the `opset_*` family, the `_private_*` getters) to build and validate opcode sets at custom-op registration time.

This page documents (1) the **engine identity** and where the Vision-Q7 proof lives, (2) the **provider → loader → device flow** and the three handle objects it threads, (3) the **52 exports grouped by role**, and (4) the **boundary** between the Q7 vector cores, the NCFW sequencer Xtensa (Part X), and the Q7 *management* CPU / FW-IO path. Each subsystem — the ISA identification and catalog, the toolchain, the provider API, the dispatch tables, the loader, the blob container, and the `ucode.c` facade — is a **sibling page**; this page links them and does not duplicate their byte-level derivations.

> **CORRECTION (scaffold) —** an earlier scaffold of this wiki described the GPSIMD/Q7 cores as "ARM-derived" with a TIE config that "could not be decoded." **Both are wrong, and superseded in place.** The cores are **Cadence Vision-Q7** (Xtensa LX + IVP), proven three independent ways (the in-blob mangled type `_TIE_xt_ivp32_xb_vec2Nx8U`, the host ELF32 machine fields `e_machine=0x5e`/`e_flags=0x300`, and the on-disk `Xm_ncore2gp` "Cairo" `core.xparm` with `vq7_isa="1"`). The `.tie` config **is shipped** in `aws-neuronx-gpsimd-tools`, so the 1065-op IVP ISA decodes exactly — see [Vision-Q7 Identification](xtensa-vision-q7.md) and the [IVP ISA Catalog](ivp-isa-catalog.md). This matches the landing-page correction in [index.md](../index.md). CONFIDENCE: **CERTAIN**.

For reimplementation, the contract a Part-XI reader must reproduce is:

- **The provider container model** — a self-contained host library with exactly 52 `nrtucode_*` exports (and zero other exports, only libc imports — *no* `dlopen`, *no* Neuron deps) that embeds 13 Xtensa ELF32 blobs + one real JSON manifest and serves them by `arch_id` and opcode.
- **The API-level-3 contract** — `nrtucode_get_api_level()` returns `3` (`0xaec0`: `mov $0x3,%eax; ret`); `nrtucode_context_create` and the consumer's `dlopen`-time check both reject any provider whose level `≠ 3` (status `4 = VERSION`).
- **The two coexisting coretype numbering schemes** — scheme A "ext-isa coretype/arch param" `{6,13,21,29}` (bitmask `0x20202040`) used by the ext-isa/ll/opset paths, and scheme B "core->kind" `NRTUCODE_CORE_*_NX_POOL = {2,9,17,25}` (bitmask `0x02020204`) stored at `core+0x10` and validated by the DGE/pc-bounds paths. Confusing them is the single easiest mistake here.
- **The three handle objects + device mailbox map** — `context` (`0x28`), `core` (`0x70`), `ll` (`0x48`) [+ `opset` `0x830`], and the device CSR layout at `core->a4` (claim magic, log ring, DGE priority map, pc-bounds).
- **The provider/firmware boundary** — the provider never touches the device; all host↔device I/O goes through a caller-supplied `rw_impl` vtable installed at `context_create`.

| | |
|---|---|
| **Provider library** | `libnrtucode_extisa.so` (build-id `7bb03bc4…`, 9,656,488 B) |
| **Engine** | GPSIMD "pool" vector co-processor — firmware self-label **"Q7"** = Cadence **Vision-Q7** (Xtensa LX + IVP) |
| **Exports** | exactly **52** `nrtucode_*` (`get`×7, `context`×5, `core`×22, `ll`×9, `opset`×9); zero other exports |
| **Imports** | libc only (`malloc`/`calloc`/`realloc`/`free`/`memcpy`/`memset`/`fprintf`/`getenv`/`strcmp`/…) — no `dlopen`, no Neuron deps |
| **API contract version** | `3` (`get_api_level` @`0xaec0`; `context_create` gate; libnrt re-checks at dlopen) |
| **Embedded device code** | 13 Xtensa ELF32 blobs (`e_machine=0x5e`, `e_flags=0x300`) = **9 distinct** images (SUNDA + v3×4 + v4×4; v4_plus re-points at v4) |
| **Opcode manifest** | SUNDA real JSON @`0x920fa0` (1728 B, 17 ops); 12 Cayman headers = `{"dummy_message":"hello world"}` stubs |
| **Runtime consumer** | `libnrt.so` `tdrv/ucode.c` — `dlsym`s 30 of 52 into fn-ptr globals (`ucode_func_symbols`@`0xbf2ea0` in libnrt) |
| **Build / git** | `get_build_version` → `"1.21.1.0"` (@`0xaed0`); `get_git_version` → `"6db9edc0…e7417e5"` (@`0xaee0`) |

---

## 1. Engine Identity

### Purpose

Before any of the API or loader machinery makes sense, fix what the engine *is*: the destination of every byte the provider serializes is a Tensilica Vision-Q7 pool core, not an AWS-bespoke ISA and not an ARM core. The full three-way proof lives on the [identification page](xtensa-vision-q7.md); this section states the conclusion and the single anchor a reader needs to trust the rest of Part XI.

### The Vision-Q7 conclusion

The 13 embedded device images are standard Xtensa ELF32 (`readelf -h`: `ELFCLASS32`, little-endian, `EXEC`, machine *"Tensilica Xtensa Processor"* = `e_machine 0x5e`, `e_flags 0x300`, `.comment` = `"XtensaTools-14.09 clang version 10.0.1"`). Every pool kernel opens with the textbook Xtensa windowed prologue `entry a1,<frame>` (byte `0x36…`); e.g. SUNDA `pool_gather` @ `0x01002590` = `36 01 02` (`entry a1,256`). The only TIE coprocessor *type* surviving in the stripped images is `_TIE_xt_ivp32_xb_vec2Nx8U` — the Cadence IVP32 "2N-wide unsigned byte" vector register class — in six `cptc_decode_impl<1..6>` instantiations in `lib3`. The on-disk `Xm_ncore2gp` "Cairo" config (`core.xparm`: `arch="Xtensa24" uarchName="Cairo" name="Xm_ncore2gp"`, Vision block `simd16="0x20" vq7_isa="1" dualquad8x8_mac="1"`) closes the identification and makes the 1065-op IVP ISA decode exactly.

> **NOTE —** the *host* carrier reports `x86-64` under `readelf -h libnrtucode_extisa.so` — that is the provider/loader library, not the firmware. The Xtensa proof is only visible after carving the 13 embedded ELF32 blobs out of `.rodata` (the 14 `\x7fELF` magics are at host offsets `0x14020`, `0x1a9c0`, `0x1bee0`, `0x1ce60`, `0x2f70a0`, `0x2fda40`, `0x2fef60`, `0x2ffee0`, `0x5a7e80`, `0x5ae820`, `0x5afd40`, `0x5b0cc0`, `0x921660`, plus the host ELF at `0x0`). CONFIDENCE: **CERTAIN**.

### Identity at a glance

| | |
|---|---|
| **Engine** | GPSIMD "pool" engine — firmware self-label **"Q7"** (`"P%i: Q7: rdma_desc_gen"` @`0x58f71`) |
| **ISA** | Cadence Tensilica **Xtensa LX** + **IVP** vector extension (Vision-Q7), `Xm_ncore2gp` "Cairo" |
| **Host machine type** | `e_machine=0x5e` ("Tensilica Xtensa Processor"), `ELFCLASS32` LE, `EXEC`, `e_flags=0x300` |
| **Toolchain (`.comment`)** | `XtensaTools-14.09 clang version 10.0.1`; config generator `RI-2022.9`, Customer ID `19270` |
| **In-blob proof type** | `_TIE_xt_ivp32_xb_vec2Nx8U` (six `cptc_decode_impl<1..6>` instantiations, `lib3`) |
| **Vector / accumulator** | `vec` 512-bit ×32 · `wvec` 1536-bit ×4 (3×512, 48-bit/lane MAC headroom) |
| **Pool ulibs (SUNDA)** | 17 ops: `pool_memset 73`, `pool_gather 104`, `pool_iota 126`, `pool_tensor_tensor_arith_op 65`, … (manifest @`0x920fa0`) |

### What is and is not on this page

The identity, the IVP register-file model, and the FLIX format scheme are the [Vision-Q7 Identification](xtensa-vision-q7.md) page. The 285 emitted / 1065 total `IVP_*` operation roster is the [IVP ISA Catalog](ivp-isa-catalog.md). The shipped `ncore2gp` config, `core.xparm`, `libisa-core.so`/`libtie-core.so`, and the `xt-objdump` that decodes the ISA are the [Xtensa Toolchain](xtensa-toolchain.md) page. This page owns only the **host provider + flow + boundary** that delivers those kernels to silicon.

---

## 2. Provider → Loader → Device Flow

### Purpose

The central mechanism Part XI documents is the path a custom-op's microcode travels: from the embedded blob in the provider's `.rodata`, through the `nrtucode_*` index/prelink/serialize layer, through the `ucode.c` dlopen facade in `libnrt`, and finally as a 64-byte instruction record pushed to device IRAM by a caller-supplied memory accessor. This section is the spine; the per-stage detail is split across the sibling pages named inline.

### The flow

```text
libnrtucode_extisa.so  (HOST provider — embeds device code, never touches the device)
  ├─ 13 Xtensa ELF32 microcode blobs   .rodata @0x14020 … 0x921660   → q7-blobs.md
  ├─ SUNDA opcode manifest JSON        @0x920fa0 (1728 B, 17 ops)    → q7-blobs.md
  └─ 52 nrtucode_* exports             (index / prelink / serialize) → extisa-provider.md
        │
        │  dlopen + dlsym(30 of 52)  ── API-level==3 asserted
        ▼
libnrt.so  tdrv/ucode.c  (RUNTIME consumer facade)                  → ucode-facade.md
  ucode_func_symbols@0xbf2ea0 (30×16B) → 30 fn-ptr globals @bss 0xc96a48..0xc96b30
  CSWTCH.113@0x86ada8 = [6,13,21]  (arch_type-2 → ext-isa coretype, scheme A)
        │
        │  context_create(3, rw_impl, &ctx) → core_create(ctx, kind, …, mailbox_base, …)
        ▼
LOADER / RELOCATOR  (ll_ family + UC-INTERNAL)                       → microcode-loader.md
  ll_get_libraries_from_opcodes → ll_create  [SUNDA minimal | Cayman prelink, <0x10000 cap]
  ll_get_load_sequence → ONE 0x40-byte "write block" record (tag 0x1095)
        │
        │  rw_impl->write / map  (caller-supplied host↔device accessor vtable)
        ▼
DEVICE  (GPSIMD Q7 pool core)
  IRAM ← microcode body ;  device dispatcher indexes kernel_info_table[opcode] → entry
  CSR mailbox @core->a4 : claim magic / log ring / DGE priority map / pc-bounds
```

The dispatch tables that map `arch_id → coretype → lib-count` (and route `arch_id-6` into the per-arch handler tables `T0..T3` @`0x934b00/10/50/90`, `JT_EXT`@`0x920e18`) are the [Dispatch Tables](dispatch-tables.md) page. The on-device `kernel_info_table` (N × 8-byte `[u32 BE opcode | u32 LE .text entry VA]` records) and the carved-blob layout are the [Q7 Blobs](q7-blobs.md) page. The RELA relocation + FLIX/UCPL device-header serialization the loader applies are the [Microcode Loader](microcode-loader.md) page.

### The 0x40-byte load/unload record

The one observable output of the loader at runtime is a single 64-byte instruction record emitted by `sub_9400` (@`0x9400`) into a caller-supplied, 8-byte-aligned buffer (mis-alignment → status `8`). Exactly one record is emitted per load and per unload; the 8-slot unrolled emitter in the same function is **dead code**, reached only on the `ll->context != core->context` error path.

```c
// nrtucode_ll_get_load_sequence(ll, core, a3, instr_buf, &n)  → sub_9400(ll, core, seq_type, …)
struct ucode_push_record {            // 0x40 bytes, byte-exact from disasm + IDA decompile
    u16 tag;          // +0x00  = 0x1095  ("write block" command opcode)
    u8  pad[10];      // +0x02  = 0
    u8  seq_type;     // +0x0c  = 1 (load) | 2 (unload)
    u8  flag;         // +0x0d  = 0xFF
    u16 rsvd;         // +0x0e  = 0
    u64 payload_ptr;  // +0x10  = rw_impl->map(ctx, ll->dram_alloc)  for load; 0 for unload
    u32 iram_target;  // +0x18  = ll->[+0x10]  (device/IRAM target addr field)
    u32 payload_len;  // +0x1c  = ll->[+0x18]  (library size)
    u8  zero[32];     // +0x20  = 0
};
```

This record, written through `rw_impl->write`, is what installs the microcode body into device IRAM. CONFIDENCE: **HIGH** (byte-exact).

### The three handle objects

The provider threads three opaque handles (plus `opset`, used only by the compiler-side consumer). A reimplementer must reproduce their sizes and the device CSR map; the full field tables are on the [provider page](extisa-provider.md).

| Handle | Size | Constructor | Key fields |
|---|---|---|---|
| `nrtucode_context_t` | `0x28` | `context_create` @`0xab10` | `+0x00 rw_impl` (host↔device accessor vtable) · `+0x08 memhandle_impl` · `+0x10 userdata` · `+0x18 scratch_cap=0x200` · `+0x20 scratch` |
| `nrtucode_core_t` | `0x70` | `core_create` @`0x99e0` | `+0x00 context` · `+0x10 kind` (scheme B `{2,9,17,25}`) · `+0x20 a4` = device mailbox/CSR base · `+0x30 boot_state` · `+0x38 logbuf_dev_ptr` · `+0x48 friendly_name[0x21]` |
| `nrtucode_ll_t` | `0x48` | `ll_create` @`0x8ef0` | `+0x00 context` · `+0x08 dram_alloc` (device buf) · `+0x10 flavor/IRAM-target` · `+0x18 library_size` |
| `nrtucode_opset_t` | `0x830` | `opset_create` @`0x8840` | `+0x00 context` · `+0x08 opcode_slots[256]` (opcode `0xF0` = extended w/ 256-byte specialization bitmap) |

The device CSR mailbox (offsets from `core->a4 = *(u64*)(core+0x20)`, all via `rw_impl` read/write): `+0` claim/boot magic (`UNCLAIMED=0x6099CB34`, `CLAIMED=0x502B2DA1`), `+4` log bufsize, `+8` log buffer dev ptr, `+16` log head, `+20` max loglevel, `+24` DGE priority-class map `u32[5]`, `+40` DGE mailbox slots `u32[5]`, `+56`/`+64` pc-bounds lo/hi. The claim handshake (`on_ucode_booted` @`0x9e50`: read `+0`, expect `0x6099CB34`, write `0x502B2DA1`, set `boot_state=1`) is the boot-state gate every DGE/pc-bounds call checks. CONFIDENCE: **HIGH** (every offset cross-checked across the log/DGE/pc-bounds functions).

> **CORRECTION —** an earlier scaffold (echoed by this page) gave the `UNCLAIMED`/release magic as **`0x60969274`**. The binary truth is **`0x6099CB34`** — `cmp $0x6099cb34,%r9d` @`0x9e8e` in `on_ucode_booted` and `movl $0x6099cb34,…` @`0x9b4b` in `core_destroy`, both byte-exact. `0x502B2DA1` (CLAIMED) is correct. This matches the [provider page](extisa-provider.md) §3 UC-API correction. CONFIDENCE: **HIGH**.

### The lifecycle the consumer drives

`libnrt`'s `ucode.c` walks the provider through a fixed init → bringup → load → query → teardown sequence; the [facade page](ucode-facade.md) has the call-by-call binding. The shape:

```text
INIT      dlopen → dlsym(30) → get_api_level()==3 → get_ext_isa(CSWTCH.113[arch-2], DEFAULT)
                              → get_memory_image(coretype, IRAM, DEFAULT) → strdup(build/git version)
BRINGUP   context_create(3, rw_impl) → set_memhandle_impl → core_create(ctx, kind, …, mailbox_base)
                              → on_ucode_booted (claim) → enable_logs → [dge_set_priority_class_map | enable_pc_bounds_check]
LOAD      ll_get_libraries_from_opcodes → ll_create → ll_get_load_sequence (ONE 0x40-byte record)
QUERY     (compiler side) opset_create → opset_add_instruction(per insn) → opset_get_library_index
TEARDOWN  print_logs → ll_get_unload_sequence → ll_destroy → core_destroy (writes 0x6099CB34 unclaim → mailbox+0)
                              → context_destroy → dlclose, NULL all fn-ptr globals
```

> **CORRECTION —** an earlier GOTCHA on this page claimed `core_destroy` (@`0x9b20`) writes the unclaim magic to **`mailbox+4`** (the log-bufsize slot), not `mailbox+0`. The disasm refutes this: the rw-write address operand is `mov 0x20(%rbx),%rsi` = `core->a4` with **no `+4` displacement** (@`0x9b56`), identical to the claim *read* `mov 0x20(%rbx),%rsi` in `on_ucode_booted` (@`0x9e6c`). Both claim and unclaim target **`mailbox+0`**; the `$0x4` operand seen alongside is the 4-byte rw length (`mov $0x4,%edx`), not a `+4` offset. A reimplementer must release the core by writing `0x6099CB34` to `+0` — the same slot the next boot's `UNCLAIMED` check reads. This matches the [provider page](extisa-provider.md) §3 UC-API correction. CONFIDENCE: **HIGH** (address operand has no displacement at either call site).

---

## 3. The 52 Exports by Role

### Purpose

The provider's entire surface is exactly 52 `nrtucode_*` functions (`nm -D`, type `T`, twice) and nothing else. They group into five families by role. The 30 that `libnrt` `dlsym`s are the runtime subset; the other 22 (the `opset_*` family + `_private_*`/getter extras) serve the compiler/assembler. The full per-function signatures, semantics, and the device side-effects of each are on the [provider page](extisa-provider.md); this table is the role map and the symbol+addr anchor.

| Family | Count | Role | Representative exports (addr) | dlsym'd by libnrt |
|---|---|---|---|---|
| `get_` | 7 | Provider query + image fetch | `get_api_level`@`0xaec0` (→3) · `get_build_version`@`0xaed0` · `get_git_version`@`0xaee0` · `get_ext_isa`@`0x87a0` · `get_num_ext_isa_libs`@`0x87b0` · `get_memory_image`@`0x8490` · `get_hwdecode_table`@`0x87f0` | 6 of 7 (not hwdecode) |
| `context_` | 5 | Provider context lifecycle | `context_create`@`0xab10` (api-level gate) · `context_destroy`@`0xac50` · `set_memhandle_impl`@`0xac10` · `get/set_userdata`@`0xacb0`/`0xacf0` | all 5 |
| `core_` | 22 | Per-core bringup, logs, DGE, pc-bounds | `core_create`@`0x99e0` · `core_destroy`@`0x9b20` · `on_ucode_booted`@`0x9e50` (claim) · `enable_logs`@`0x9f60` · `print_logs`@`0xa1a0` · `dge_set_priority_class_map`@`0xa3a0` · `enable_pc_bounds_check`@`0xa860` | 13 of 22 |
| `ll_` | 9 | Low-level loader / device push stream | `ll_create`@`0x8ef0` (prelink) · `ll_destroy`@`0x91e0` · `get_libraries_from_opcodes`@`0x8ce0` · `get_load_sequence`@`0x93f0` · `get_unload_sequence`@`0x9880` · `get_library_size`@`0x98e0` | 6 of 9 |
| `opset_` | 9 | Opcode-set build + query (compiler/asm) | `opset_create`@`0x8840` · `opset_add_instruction`@`0x89e0` · `opset_get_library_index`@`0x8db0` · `opset_private_has_opcode`@`0x8c50` · `…has_specialization`@`0x8c70` | **0** (compiler side only) |

> **CORRECTION —** an earlier scaffold of this table gave the per-family consumed split as `get_=5/7`, `context_=5/5`, `core_=14/22`, `ll_=7/9`, `opset_=0/9`, which sums to **31** — one more than the verified 30-entry `dlsym` table. The split above is re-derived as the intersection of the provider's exported `nrtucode_*` text symbols with the `nrtucode_*` name-strings present in `libnrt.so`: `comm -12 <(nm -D libnrtucode_extisa.so | grep ' T ' | grep nrtucode_ | awk '{print $3}' | sort) <(strings libnrt.so | grep '^nrtucode_' | sort -u)`. The verified split is `get_=6/7` (only `get_hwdecode_table` unused), `context_=5/5`, `core_=13/22`, `ll_=6/9` (`get_library_size`, `get_load_sequence_num_instrs`, `get_unload_sequence_num_instrs` unused), `opset_=0/9` — summing to **30 of 52**. CONFIDENCE: **HIGH** (build-id `7bb03bc4…`).
>
> **NOTE —** the `opset_*` family is the *producer-side* API: the compiler/assembler calls `opset_create` → `opset_add_instruction` (per kernel instruction; opcode `0xF0` carries a 256-byte specialization bitmap, `instr[12]` selecting the specialization) to build an opcode set, then `opset_get_library_index` to map it to a microcode library. `libnrt`'s runtime path never touches it — which is why the 30-entry `dlsym` table is a strict subset of the 52 exports. CONFIDENCE: **HIGH** (the 22 unused-by-libnrt extras are individually enumerated in the decompile).

### Status and result codes

Every `nrtucode_*` returning a result yields `nrtucode_result_t`: `0 SUCCESS · 1 UNKNOWN_CORE · 2 UNKNOWN_IMAGE · 3 MISSING_IMAGE · 4 VERSION · 5 ALLOCATION · 6 IO · 7 ENOSPC · 8 INVALID · 9 RELOCATION`. The two that bite a reimplementer: `4 VERSION` is the api-level-`≠3` reject; `7 ENOSPC` is the Cayman prelink "library would be larger than the available buffer on device" cap (total `< 0x10000`).

---

## 4. The Three Xtensa Paths — Q7 Pool vs NCFW Sequencer vs Q7 Management

### Purpose

Neuron firmware runs multiple Xtensa cores, and conflating them is the most common identification error in this stack. This section draws the boundary so a reader knows exactly what Part XI owns and what belongs to Part X and to the kernel-driver pages.

### The distinction

> **NOTE —** three Xtensa-adjacent paths must be kept apart:
>
> - **GPSIMD / Q7 *vector* pool cores (this Part).** The Vision-IVP32 config (`Xm_ncore2gp` "Cairo"). Runs the pool/compute kernels (`decode_pool`, `cptc_decode_impl`, `iota_kernel`, gather/embedding). Microcode = the 13 ELF32 blobs in `libnrtucode_extisa.so`, served by the 52 `nrtucode_*` exports. *Has* IVP vector files; opcode-driven via `kernel_info_table`.
> - **NCFW Xtensa *sequencer* ([Part X](../firmware/overview.md)).** A *different* Xtensa LX config — sequencer/collective-firmware, **no IVP vector files** (its custom TIE is the `op0=4` MAC16 window-save block). Microcode = 8 **raw, non-ELF** IRAM/DRAM blobs in `libncfw.so`, served by `libncfw_get_image`. Same ISA *family* (Tensilica Xtensa LX), different TIE config; it does **not** decode with the `ncore2gp` `.tie` and must not be tagged Vision-Q7.
> - **Q7 *management* CPU / FW-IO path.** The host↔device control mailbox (claim magic, log ring, DGE priority map, pc-bounds) the provider writes through `rw_impl` lands in a device CSR region driven by the management/FW-IO protocol — the [FW-IO MiscRAM Mailbox](../kernel/fw-io.md) cell — not the vector datapath. The provider serializes *into* that mailbox; it does not implement it.
>
> One ISA family, two TIE configs (Q7-vector, NCFW-sequencer), plus a separate management/FW-IO control plane. CONFIDENCE: **CERTAIN** (the NCFW blobs are non-ELF raw IRAM/DRAM; the Q7 blobs are ELF32 EM=0x5e with IVP TIE types — disjoint by construction).

### The two coretype schemes

The provider carries **two distinct coretype numbering schemes** that a reimplementer must not unify — they index different machinery and use different bitmasks:

| Scheme | Values (SUNDA/CAYMAN/MARIANA/MARIANA_PLUS) | Bitmask | Used by | Stored at |
|---|---|---|---|---|
| **A — ext-isa coretype / arch param** | `{6, 13, 21, 29}` | `0x20202040` | `get_ext_isa`, `get_num_ext_isa_libs`, `ll_create`, `get_libraries_from_opcodes`, `opset_get_library_index` (dispatch via `arch_id-6` jump tables) | function arg |
| **B — `core->kind` (`NRTUCODE_CORE_*_NX_POOL`)** | `{2, 9, 17, 25}` | `0x02020204` | DGE-mailbox / pc-bounds / priority-map gates (assert text `"…NX""_""POOL"`) | `core+0x10` |

`libnrt`'s `CSWTCH.113 = [6,13,21]` (@`0x86ada8`, `arch_type-2 → coretype`) feeds scheme A. The silicon binding (SUNDA = v2, CAYMAN = v3/v4/v4_plus) lives in `libnrt`, not the provider — see the [Dispatch Tables](dispatch-tables.md) page for the full `arch_id → coretype → lib-count` derivation. CONFIDENCE: **HIGH** (both bitmasks read from disasm); silicon naming **MED**.

---

## 5. Sub-Page Map

The nine Part-XI pages decompose this map as follows. Read top-to-bottom for a from-scratch reimplementation: identify the engine, get the toolchain, then the host API, then the data structures, then the device-side ISA.

| # | Page | What it owns |
|---|---|---|
| 1 | **[Overview (this page)](overview.md)** | The provider → loader → device map, the 52-export role groups, the Q7/NCFW/management boundary |
| 2 | **[Vision-Q7 Identification](xtensa-vision-q7.md)** | The three-way ISA proof; the 8 IVP register files; the FLIX format/length model |
| 3 | **[Xtensa Toolchain & TIE Config](xtensa-toolchain.md)** | The shipped `ncore2gp` config (`core.xparm`, `libisa-core.so`/`libtie-core.so`) and `xt-objdump` |
| 4 | **[ext-ISA Provider (`nrtucode_*` API)](extisa-provider.md)** | The 52 exports' signatures/semantics; the `context`/`core`/`ll`/`opset` handles; the device CSR map |
| 5 | **[Dispatch Tables & arch-id Scheme](dispatch-tables.md)** | `arch_id → coretype → lib-count`; `JT_EXT`@`0x920e18`; handler tables `T0..T3`@`0x934b00..`; both coretype schemes |
| 6 | **[The Microcode Loader (RELA + FLIX, UCPL)](microcode-loader.md)** | `ll_create` prelink (Cayman geometry, `<0x10000` cap); RELA relocation; the UCPL device header |
| 7 | **[The 13 Q7 Microcode Blobs](q7-blobs.md)** | Carving the ELF32 images (13 → 9 distinct); the `kernel_info_table` 8-byte record format |
| 8 | **[IVP Vector ISA Catalog (1065 Ops)](ivp-isa-catalog.md)** | The 285 emitted / 1065 ISA-wide `IVP_*` ops by category × FLIX format × lane-shape |
| 9 | **[The `ucode.c` dlopen Facade](ucode-facade.md)** | `libnrt`'s consumer binding: the 30-entry `dlsym` table, fn-ptr globals, api-level check, init/teardown |

---

## Related Components

| Name | Relationship |
|---|---|
| NCFW Collectives Firmware ([Part X](../firmware/overview.md)) | The *other* Xtensa config — sequencer-class, raw non-ELF blobs in `libncfw.so`; same ISA family, different TIE |
| FW-IO MiscRAM Mailbox ([kernel](../kernel/fw-io.md)) | The device management/control mailbox the provider serializes into via `rw_impl` — the Q7 management path |
| TPB Engine Instruction Model ([Part VI](../isa/overview.md)) | The Neuron-TPB op/dtype enums (`NEURON_ISA_TPB_{ALU_OP,REDUCE_OP,DTYPE}`) the pool kernels parameterize on |
| libnrt companions ([runtime](../runtime/libnrt/companions.md)) | Where `libnrt` `dlopen`s the provider and the other companion libraries |
| `neuronx-gpsimd` toolchain (sibling wiki) | The producer side — the GPSIMD/Q7 custom-op compiler that emits the kernels this provider ships |

## Cross-References

- [Tensilica Xtensa and Vision-Q7 Identification](xtensa-vision-q7.md) — the canonical ISA proof; what the Q7 engine *is*
- [The Xtensa Toolchain and TIE Config](xtensa-toolchain.md) — the shipped `ncore2gp` config and `xt-objdump` that decode the ISA
- [The ext-ISA Provider (`nrtucode_*` API)](extisa-provider.md) — the 52 exports, the three handles, and the device CSR map in full
- [ext-ISA Dispatch Tables and arch-id Scheme](dispatch-tables.md) — `arch_id → coretype → lib-count`; both coretype numbering schemes
- [The Microcode Loader (RELA + FLIX, UCPL)](microcode-loader.md) — Cayman prelink, RELA relocation, the UCPL device header
- [The 13 Q7 Microcode Blobs](q7-blobs.md) — carving the ELF32 images and the `kernel_info_table` opcode→entry map
- [The IVP Vector ISA Catalog (1065 Ops)](ivp-isa-catalog.md) — the operation roster the cataloged kernels emit
- [The ucode.c dlopen Facade](ucode-facade.md) — `libnrt`'s 30-entry `dlsym` binding to this provider
- [NCFW Collectives Firmware](../firmware/overview.md) — the sequencer Xtensa (Part X), the *other* config
- [FW-IO MiscRAM Mailbox](../kernel/fw-io.md) — the Q7 management/control path, distinct from the vector datapath
- [back to index](../index.md)
