# The Carrier Library (libncfw.so)

> *All addresses on this page apply to `libncfw.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (ELF64 x86-64 DYN, 615,640 B, md5 `e01ea384a76e59d511b4f005b7db98ac`, SONAME `libncfw.so.2.31.1.0.cf13a49f`, build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`; **not** stripped — full symtab, no DWARF). The object is position-independent (`PIE` base 0), so every `0xNNNN` is both an analysis VMA and, for the `0x65xxx`+ range, a host file offset: `.text` @ `0x10c0` and `.rodata` @ `0x65000` both have VMA == file offset (`readelf -S`). `NEEDED` is `libc.so.6` only. Other versions will differ.*
>
> *Evidence grade: **Confirmed (byte- and symbol-anchored)** — the 3-export surface (`nm -D`), the 4-import table and the JUMP_SLOT/GLOB_DAT relocation set (`nm -D` + `readelf -r`/`-d`), the `get_image`/`get_version`/`ctx_log` bodies (`objdump -d`), and the serializer `strlen`/`snprintf`/`buffer_full` idiom are all re-derived from the binary this pass. Silicon codenames are **MEDIUM** (inferred by switch position; the authoritative table lives in libnrt). · Part X — Collectives Firmware (libncfw) · [back to index](../index.md)*

## Abstract

`libncfw.so` is the **carrier** for the on-device NCFW ("Neuron Collective FirmWare") sync-core firmware — and nothing else. The cleanest familiar frame is a **board-support package compiled to a host shared object**: it bakes the device firmware images into its own `.rodata`, hands them out by integer device generation, and ships one debug utility that pretty-prints the on-device collective-communication context as JSON. What makes it unusual as a "firmware library" is that it is *entirely passive* — it cannot load, flash, reset, or even read the device. It owns **zero device I/O**. Its only four dynamic imports are `memset`, `snprintf`, `strlen`, and `__stack_chk_fail`; there is no `ioctl`, `open`, `mmap`, `write`, `read`, `socket`, or `dlopen` anywhere in the import table or relocation set. It is a *provider* (a source of bytes) and a *serializer* (a pretty-printer), and both jobs are pure functions of their arguments.

That passivity is the whole architectural point, and it is the fact a reimplementer must internalize first. libnrt `dlopen`s this object, calls `libncfw_get_image()` to pull the raw firmware bytes out of `.rodata`, and then does all the physical work itself — DMA the IRAM half into the NeuronCore TPB sequencer's instruction RAM, write the DRAM half to HBM, patch the descriptor template, release the core from reset. None of that is in `libncfw`. A carrier that cannot touch silicon is exactly what you want for a firmware blob store: it is trivially safe to load, has no driver dependency, and can be unit-tested on any host. The cost is that every device interaction lives one layer up, in libnrt and the kernel ([Firmware Upload Path](upload-path.md)).

The public surface is **three exported C symbols** and they realize **two jobs** keyed by one integer. Job A — the **firmware blob provider** — is `libncfw_get_image` @`0x1179`: a flat `switch` on the `nrtucode_coretype` arch ID (`5`/`12`/`20`/`28`) that fills a caller-supplied four-word descriptor with `{&iram, iram_size, &dram, dram_size}` pointing *directly into* the eight embedded blobs, with no copy and no header. Job B — the **CC-context JSON serializer** — is `libncfw_ctx_log` @`0x1309`: the same four-way `switch` dispatching into a 152-function recursive-descent tree that walks an in-memory collective-context struct and emits it as JSON into a 1 MB caller buffer. The third export, `libncfw_get_version` @`0x12fa`, is a one-instruction ABI gate (`mov $0x2; ret`) that libnrt asserts equals `2`. This page owns the **host object**: the ELF identity, the provider ABI as C pseudocode, the version gate, a representative serializer's bounds-checking idiom, and the byte-exact zero-device-I/O proof. It does **not** re-derive the blob inventory (owned by [Embedded Payloads](embedded-payloads.md)) or the Xtensa disassembly (owned by [The NCFW Sequencer](ncfw-sequencer.md)), and it summarizes — does not re-walk — the serializer tree ([Serializer Families](serializer-families.md)).

For reimplementation, the contract is:

- **The provider ABI** — the three exports, the `5/12/20/28` coretype `switch` (binary-search `cmpl` tree), the `{&iram, iram_size, &dram, dram_size}` out-descriptor *slot order and write offsets*, the `0`/`22`/`2` return codes, and the version gate (`get_version` must return `2`).
- **The provision mechanism** — `get_image` returns *pointers into `.rodata`* (no copy, no allocation, no parse): the out-words are addresses and the raw `*_bin_size` values, so the consumer's loader is a `memcpy`/DMA of `{ptr, len}`.
- **The serializer convention** — every node is `int ncfw_log_<x>(out, indent, key, struct_ptr)`; every `snprintf` is bounds-checked against `0x100000 - strlen(out)`; a global `ncfw_log_buffer_full` latches on truncation; the top function returns `28`/ENOSPC if it latched, else `0`.
- **The zero-device-I/O invariant** — exactly four libc imports, no syscall wrapper of any kind; the carrier is provably incapable of touching the device, and everything physical is the consumer's job.

| | |
|---|---|
| **Binary** | `libncfw.so`, 615,640 B, build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`, SONAME `libncfw.so.2.31.1.0.cf13a49f` |
| **Class / type** | ELF64 x86-64 DYN (PIE base 0); not stripped; `NEEDED libc.so.6` only |
| **Exports (3)** | `libncfw_get_image` @`0x1179` · `libncfw_get_version` @`0x12fa` (returns `2`) · `libncfw_ctx_log` @`0x1309` |
| **Imports (4)** | `memset`, `snprintf`, `strlen`, `__stack_chk_fail` — **all libc**; no syscall wrapper |
| **Device I/O** | **none** — pure host provider/serializer (proven by the import + reloc tables) |
| **Arch key** | `nrtucode_coretype` ∈ `{5, 12, 20, 28}` → `{v2/sunda, v3/cayman, v4/mariana, v4_plus/mariana_plus}` |
| **Job A — provider** | flat `switch` → `{&iram, iram_size, &dram, dram_size}` into 8 `.rodata` blobs (no copy/header/checksum) |
| **Job B — serializer** | `5/12/20/28` dispatch → 152-fn recursive descent → JSON into a 1 MB buffer |
| **Consumer** | `libnrt.so` `encd_libncfw_init` @`0x251cc0` (`dlopen`/`dlsym`) / `encd_ncfw_init` @`0x251eb0` (fptr call) |

> **QUIRK — the library does ZERO device I/O, and the import table proves it.** A "firmware library" that cannot talk to firmware is counter-intuitive, so this is the single most important fact on the page. `nm -D libncfw.so` lists **exactly four** undefined symbols — `memset`, `snprintf`, `strlen`, `__stack_chk_fail` — all `@GLIBC`. `readelf -r` shows the only four `R_X86_64_JUMP_SLOT` relocations bind those same four functions; the `R_X86_64_GLOB_DAT` set is the standard weak-stub quartet (`_ITM_deregisterTMCloneTable`, `_ITM_registerTMCloneTable`, `__gmon_start__`, `__cxa_finalize`). There is **no** `ioctl`/`open`/`mmap`/`write`/`read`/`pread`/`pwrite`/`socket`/`close`/`dlopen` anywhere — `nm -D | rg` for them returns nothing. A reimplementer must not add a device path "for completeness": the carrier's correctness depends on it being inert. All DMA, HBM patching, and reset-release happen in the consumer ([Firmware Upload Path](upload-path.md)).

---

## 1. The Host Object

### Purpose

Before either job, fix what kind of artifact this is. `libncfw.so` is a small, self-contained x86-64 shared object whose entire reason to exist is to *carry* device firmware across the host/device boundary. It is not the firmware, not a driver, and not a loader — it is the container the firmware travels inside on the host, plus a debug reflector for the context the firmware consumes. Everything else on the page is a consequence of that role.

### ELF identity

The version pin at the top is the identity; three facts about it drive the rest of the page. First, the object is **PIE with `.text` and `.rodata` at VMA == file offset**, so an address like `0x66a60` is simultaneously where a blob lives in memory and where `dd skip=0x66a60` finds it on disk — which is why the provider can hand out raw `.rodata` pointers with no fixup. Second, it is **not stripped**: the full symbol table is present (181 named functions, all eight blob data objects as `nm`-`'r'` symbols), so the provider switch, the serializer tree, and the blob layout are all directly named rather than inferred from cross-references. Third, `NEEDED` is **`libc.so.6` and only `libc.so.6`** — there is no dependency on libnrt, on a Neuron driver, or on `libdl`; the carrier is a leaf in the dependency graph.

### The section model

```text
libncfw.so  (host x86-64, PIE base 0; VMA == file offset for .text/.rodata)
  .text    @0x10c0  (0x63991 B)  -- code: 3 exports + 4 arch dispatchers + 152 serializer helpers + CRT
  .rodata  @0x65000 (0x2c8e4 B)  -- JSON-key strings (key block x4) + the 8 RAW firmware blobs + size words
  .bss     @0x95028 (8 B)        -- ncfw_log_buffer_full x4 (@0x95029..0x9502c) + CRT
```

The blobs and key strings *are* `.rodata`; there is no separate firmware section, no `.fw`, no compressed segment — the firmware images are ordinary read-only data objects the linker placed inline. Their carve, sizes, digests, and on-device layout are owned by [Embedded Payloads](embedded-payloads.md); this page treats them only as the *targets* the provider returns pointers to.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `libncfw_get_image` | `0x1179` | Job A — provider: coretype `switch` → fill `{&iram, isz, &dram, dsz}` | CERTAIN |
| `libncfw_get_version` | `0x12fa` | ABI gate: `mov $0x2; ret` | CERTAIN |
| `libncfw_ctx_log` | `0x1309` | Job B — serializer dispatch by coretype → `<arch>_ncfw_ctx_log` | CERTAIN |
| `sunda_ncfw_ctx_log` | `0x1a12b` | arch-`5` dispatch wrapper → `ncfw_ctx_log` @`0x19f01` | HIGH |
| `cayman_ncfw_ctx_log` | `0x32ed2` | arch-`12` wrapper → `ncfw_ctx_log_0` @`0x32ca8` | HIGH |
| `mariana_ncfw_ctx_log` | `0x4bc79` | arch-`20` wrapper → `ncfw_ctx_log_1` @`0x4ba4f` | HIGH |
| `mariana_plus_ncfw_ctx_log` | `0x64a20` | arch-`28` wrapper → `ncfw_ctx_log_2` @`0x647f6` | HIGH |

---

## 2. Job A — The Firmware Blob Provider

### Purpose

`libncfw_get_image` is the carrier's reason to exist: given an architecture generation, return the matching `{IRAM code, DRAM data}` firmware pair. The defining design choice is that it returns **pointers, not copies** — the out-descriptor's words are addresses into the object's own `.rodata` plus the raw size values. There is no allocation, no buffer the caller must free, no container to parse, and no header/magic/checksum to validate. The consumer DMAs straight out of the returned `{ptr, len}`.

### Entry Point

```text
libnrt encd_ncfw_init @0x251eb0
  └─ (cached fptr) libncfw_get_image(coretype, ncfw_image_t* out)   @0x1179
       └─ switch(coretype) { 5 | 12 | 20 | 28 }   -- per-arch lea of 4 .rodata symbols ; no callees
```

`get_image` is a **leaf** — it makes no calls at all (not even `memset`); every arm is straight-line `lea`/`mov`. That is the structural signature of a pure provider.

### Algorithm

```c
// Models libncfw_get_image @0x1179. Verified objdump: a null-out guard, then a
// binary-search cmpl tree on the arch id, each arm writing four words into out[].
//
// out layout (byte offsets into the caller's 4-qword struct, from the mov disp):
//   out+0x00 = &<vN>_ncfw_iram_bin      (IRAM code pointer)
//   out+0x08 =  <vN>_ncfw_iram_bin_size (u32, loaded `mov (%rax),%eax` then stored as qword)
//   out+0x10 = &<vN>_ncfw_dram_bin      (DRAM data pointer)
//   out+0x18 =  <vN>_ncfw_dram_bin_size (u32 -> qword)
int libncfw_get_image(uint32_t arch_id, uint64_t out[4]):
    if (out == NULL)                       // 0x1188  cmpq $0x0,-0x10(%rbp)
        return 22;                         // 0x118f  EINVAL -- null descriptor

    // Binary-search switch (cmpl/je with ja->default at each level).
    switch (arch_id) {                     // 0x1199..0x11cd
    case 0x05:  /* v2 / sunda  */          // 0x11d2
        out[2] = (uint64_t)&v2_ncfw_dram_bin;        // store @out+0x10
        out[3] = (uint32_t) v2_ncfw_dram_bin_size;   // store @out+0x18  (load is `mov (%rax),%eax`)
        out[0] = (uint64_t)&v2_ncfw_iram_bin;        // store @out+0x00
        out[1] = (uint32_t) v2_ncfw_iram_bin_size;   // store @out+0x08
        break;
    case 0x0c:  out = {v3_iram, v3_isz, v3_dram, v3_dsz};   break;    // 0x121a
    case 0x14:  out = {v4_iram, v4_isz, v4_dram, v4_dsz};   break;    // 0x1262
    case 0x1c:  out = {v4p_iram, v4p_isz, v4p_dram, v4p_dsz};         // 0x12a7
                break;
    default:                               // 0x12ec
        return 2;                          // ENOENT -- unknown arch
    }
    return 0;                              // 0x12f3  success
}
```

Three details bite a reimplementer. **(1) The size is read as a `u32`.** Each `*_bin_size` word is loaded with `mov (%rax),%eax` — a 32-bit load — then widened into the 64-bit out-slot. That is deliberate: the `v4_plus_ncfw_iram_bin_size` word's *upper* bytes overlap the SONAME tail that follows it in `.rodata` (raw 8 bytes at `0x918e0` = `20 4c 00 00 01 1b 03 3b`; the true length is the low dword `0x4c20` = 19,488). A reimplementer who reads the size as a `u64` gets garbage for that one symbol — read it as a `u32`. **(2) The out-write order is DRAM-then-IRAM in the assembly, but the *slot* order is IRAM-first** (`out[0]`/`out[1]` = IRAM, `out[2]`/`out[3]` = DRAM). The descriptor's meaning is by offset, not by write order. **(3) The provider makes no copy** — `out[0]`/`out[2]` are addresses *into this object's `.rodata`*, valid only while the carrier stays mapped, so the consumer must DMA/`memcpy` before unmapping.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `libncfw_get_image` | `0x1179` | the provider — coretype `switch`, fills 4-word descriptor, leaf | CERTAIN |
| `v2_ncfw_iram_bin` / `_size` | `0x6a140` / `0x74a20` | arch-`5` IRAM target + size word | CERTAIN |
| `v3_ncfw_iram_bin` / `_size` | `0x79860` / `0x7e420` | arch-`12` IRAM target + size word | CERTAIN |
| `v4_ncfw_iram_bin` / `_size` | `0x83260` / `0x87e80` | arch-`20` IRAM target + size word | CERTAIN |
| `v4_plus_ncfw_iram_bin` / `_size` | `0x8ccc0` / `0x918e0` | arch-`28` IRAM target; size = **low u32** only | CERTAIN |

(The four `*_dram_bin`/`_size` symbols are the analogous DRAM targets; full carve and digests in [Embedded Payloads](embedded-payloads.md).)

### Considerations

The provider has no version field, no magic, and no integrity check *inside the bytes* — the only metadata is the host-side symbol and the size word. That pushes all validation onto the consumer: libnrt's `encd_set_ncfw_ucode_bins` is the layer that adds any container/header/checksum on the way to the device, and it is also where the coretype value originates. The provider's contract is therefore minimal and total: a valid `arch_id` yields four words; an invalid one yields `2`; a null descriptor yields `22`. Nothing else can go wrong, because nothing else happens.

---

## 3. The Version Gate

### Purpose

`libncfw_get_version` exists so the consumer can refuse an ABI-incompatible carrier before trusting the descriptor shape `get_image` writes. It is the smallest possible function that does that job.

### Algorithm

```c
// Models libncfw_get_version @0x12fa -- verified objdump, the whole body.
int libncfw_get_version(void):
    return 2;                              // 0x1302  mov $0x2,%eax ; ret
```

libnrt's `encd_libncfw_init` (@`0x251cc0`) `dlsym`s this symbol and asserts the result equals `2` immediately after `dlopen`; a mismatch is the carrier-rejection path. The constant `2` is therefore the **ABI version of the `{get_image, ctx_log}` contract** — the four-word out-descriptor layout and the `(ctx, out, model, arch)` serializer signature. A reimplementer who changes either must bump this constant in lockstep, or libnrt will load the new carrier and trust the old layout.

> **NOTE —** the gate is checked at load time, not per call. A carrier that returns the wrong version is rejected once at `dlopen`; there is no per-image versioning inside the blobs (see [Embedded Payloads](embedded-payloads.md)).

---

## 4. Job B — The CC-Context Serializer (dispatch + convention)

### Purpose

`libncfw_ctx_log` is the only non-trivial control flow in the library. Given a parsed in-memory collective-communication (CC) context struct — built by libnrt's collectives layer, **not** by libncfw — it pretty-prints the entire structure as JSON into a 1 MB caller buffer. Because the serializer is the structural reflection of the on-device sequencer's context, recovering its keys and dereferences reconstructs the CC-context schema *without the device*. This page owns the **dispatch and the per-node convention**; the 38-family tree and the recovered field offsets are owned by [Serializer Families](serializer-families.md).

### Entry Point

```text
libncfw_ctx_log(ctx, out, model_name, arch_id)   @0x1309
  └─ switch(arch_id) { 5 | 12 | 20 | 28 }            -- same binary-search tree as get_image
       └─ <arch>_ncfw_ctx_log(ctx, out, model_name)  -- arch_id dropped from the tail call
            └─ ncfw_ctx_log[_N]  -- memset(out,0,0x100000); "{"; "model_name"; ncfw_log_ctx(root)
```

### Algorithm — the dispatcher

```c
// Models libncfw_ctx_log @0x1309. The arch switch is byte-identical in shape to
// get_image's; the difference is the arm bodies tail-call the arch serializer
// (and DROP arch_id -- the callee is already arch-specialized).
int libncfw_ctx_log(void *ctx, char *out /* >=1 MiB */, const char *model_name, uint32_t arch_id):
    switch (arch_id) {                                 // 0x1324..0x1348
    case 0x05: return sunda_ncfw_ctx_log(ctx, out, model_name);          // 0x135c
    case 0x0c: return cayman_ncfw_ctx_log(ctx, out, model_name);         // 0x1375
    case 0x14: return mariana_ncfw_ctx_log(ctx, out, model_name);        // 0x138e
    case 0x1c: return mariana_plus_ncfw_ctx_log(ctx, out, model_name);   // 0x13a7
    default:   return 22;                              // 0x13ae  EINVAL -- unknown arch
    }
}
```

### Algorithm — the per-node bounds-checking convention

Every one of the 152 serializer helpers shares one signature and one safety idiom. The idiom is what makes the 1 MB buffer safe and what sets the `28`/ENOSPC return; a reimplementer must reproduce it exactly or risk an overflow the original cannot have.

```c
// The universal serializer-node shape (sunda base + 3 arch clones each).
// `out` is the 1 MiB buffer; the running write cursor is implicit = out + strlen(out).
int ncfw_log_<node>(char *out, int indent, const char *key, void *struct_ptr):

    // --- the EMIT idiom, repeated once per snprintf in every node ---
    size_t budget = 0x100000 - strlen(out);            // remaining room  (0x100000 = 1 MiB)
    int n = snprintf(out + strlen(out), budget,        // write at the cursor
                     "%*s\"%s\": {", indent, "", key); // e.g. an object-open  (fmt @0x65000)
    if (n < 0 || (size_t)n >= 0x100000 - strlen(out))  // truncated or error
        ncfw_log_buffer_full = 1;                      // .bss @0x95029 -- latch, never reset here

    // --- recurse / emit children, then close ---
    // ... emit scalar fields and child nodes (each via the same EMIT idiom) ...
    // close with "}," or "}" ;  indent grows +2 per nesting level
    return n;
```

Two structural facts the dispatcher relies on. The top-level `ncfw_ctx_log[_N]` first `memset`s the whole 1 MB buffer to zero (so `strlen` reads a clean cursor), emits `"{\n"` then `"model_name": "<model>",\n`, and calls the root node `ncfw_log_ctx(out, 2, "ncfw_ctx_top_sp", ctx+…, …)`. After the tree returns, it inspects the **`ncfw_log_buffer_full` latch** and returns `28`/ENOSPC if any emit truncated, else `0`. The latch is a global, not a per-call flag — there are **four copies** in `.bss` (`@0x95029`/`0x9502a`/`0x9502b`/`0x9502c`, one per arch clone; `nm` shows all four named `ncfw_log_buffer_full`), and the sunda tree writes the `0x95029` copy. Because it is never reset inside a node, a single truncation anywhere in a multi-thousand-node dump turns the entire return into ENOSPC — the buffer is all-or-nothing.

### A representative serializer — `ncfw_log_dma_channel_apb_bcast`

The DMA/barrier/semaphore serializers all instantiate the convention over a small device-address struct. `ncfw_log_dma_channel_apb_bcast` @`0x4899` is the clearest: a 20-byte APB-broadcast DMA descriptor of two SoC tail-pointer addresses and a mask. It shows the full node shape — object-open, the empty-`{}` short-circuit, two `soc_addr` leaves via `ncfw_log_addr`, a scalar `mask`, object-close.

```c
// Models ncfw_log_dma_channel_apb_bcast @0x4899 (verified objdump).
// struct (20 B, ptr = a4):  +0 u64 m2s_tail_ptr (soc_addr)
//                           +8 u64 s2m_tail_ptr (soc_addr)
//                          +16 u32 mask
int ncfw_log_dma_channel_apb_bcast(char *out, int indent, const char *key, void *p):

    EMIT("%*s\"%s\": {", indent, "", key);             // 0x4900  open object  (paraphrase of %*s @0x65001 + "%s": {\n @0x65005)

    if (*(uint8_t*)p == 0) {                            // 0x4938  movzbl (p) -- empty-descriptor guard
        EMIT("{}");                                     // 0x49f5  short-circuit empty form (paraphrase; open-brace frag {\n @0x6500e)
        return ...;
    }
    EMIT("%*s\"%s\": {\n", indent, "", key);            // 0x4983  (non-empty: re-open keyed object)

    // two SoC device-address leaves; ncfw_log_addr's LEADING char arg = "emit trailing comma?"
    ncfw_log_addr(/*trail=*/1, out, indent, "m2s_tail_ptr", (uint64_t*)(p + 0));   // 0x4a4a  key @0x65150
    ncfw_log_addr(/*trail=*/1, out, indent, "s2m_tail_ptr", (uint64_t*)(p + 8));   // 0x4a70  key @0x6515d

    EMIT("%*s\"mask\": %u\n", indent, "", *(uint32_t*)(p + 16));   // key "mask" @0x6516a -- last field, no comma
    EMIT("%*s},\n", indent, "");                        // close object  (paraphrase; close frag },\n @0x65026)
    return ...;
}

// The shared SoC-address leaf the two calls above route through:
// Models ncfw_log_addr @0x41c3 -- note the EXTRA leading char arg (saved %al @-0x34).
int ncfw_log_addr(char trail_comma, char *out, int indent, const char *key, uint64_t *p):
    EMIT("%*s\"%s\": \"0x%016lX\"%s", indent, "", key, *p, trail_comma ? ",\n" : "\n");  // paraphrase; actual constant @0x65127 is `%s: "0x%016lX"\n`
```

> **NOTE — `ncfw_log_addr` breaks the uniform signature with a leading flag byte.** The 38 families almost all share `(out, indent, key, struct_ptr)`, but the `soc_addr` leaf `ncfw_log_addr` @`0x41c3` prepends a `char trail_comma` (the firmware-side "is this the last field in its object?" flag), passed in `%al` and saved at `-0x34`. A reimplementer who assumes a uniform 4-arg signature for *every* node will mis-call the single most-used leaf in the tree. The non-trailing variant exists because the last field in a JSON object must omit the comma.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `libncfw_ctx_log` | `0x1309` | serializer dispatch — coretype `switch` → arch wrapper | CERTAIN |
| `ncfw_ctx_log` (sunda base) | `0x19f01` | top node: `memset` 1 MB, `"{"`, `model_name`, root | HIGH |
| `ncfw_log_ctx` | `0x19c0e` | root `ncfw_ctx_top_sp` → configs / neff / algo | HIGH |
| `ncfw_log_addr` | `0x41c3` | SoC-address leaf — `"0x%016lX"`, leading trail-comma arg | HIGH |
| `ncfw_log_dma_channel_apb_bcast` | `0x4899` | representative DMA serializer — 20 B `{m2s, s2m, mask}` | HIGH |
| `ncfw_log_buffer_full` | `0x95029`..`0x9502c` | `.bss` truncation latch ×4 (one per arch clone) | CERTAIN |

### Considerations

The serializer is the only place the CC-context schema is observable on the host. It does **not** define the on-device *meaning* of the scheduler (that runs in the Xtensa IRAM, partially opaque behind the sequencer TIE — [The NCFW Sequencer](ncfw-sequencer.md)); it merely *reflects* the struct libnrt's collectives layer builds. Because every helper is `arch`-cloned ×4 over byte-identical key strings (the key block is replicated four times in `.rodata` at `0x65000`/`0x65695`/`0x65d2f`/`0x663c4`), a reimplementer should write **one** serializer parameterized by the two top-level context offsets rather than four. The byte-level family tree and the recovered struct offsets are owned by [Serializer Families](serializer-families.md).

---

## 5. The Consumer Boundary

The carrier is inert until libnrt loads it; everything device-facing is on the other side of a `dlopen`/`dlsym` handshake gated by `get_version`. The boundary is owned by [Firmware Upload Path](upload-path.md); the essentials, so the provider ABI reads in context:

```text
libnrt encd_libncfw_init @0x251cc0
   dlopen("libncfw.so", RTLD_NOW)                       -- libncfw itself has no dlopen
   dlsym libncfw_get_version  -> assert == 2            -- ABI gate (else reject carrier)
   dlsym libncfw_get_image    -> cache fptr @ libnrt bss 0xc96d10
   dlsym libncfw_ctx_log      -> cache fptr @ libnrt bss 0xc96d08
libnrt encd_ncfw_init @0x251eb0  ->  encd_set_ncfw_ucode_bins
   fptr(coretype, ncfw_image_t* out)   // = libncfw_get_image -- returns {&iram,isz,&dram,dsz}
   -> IRAM bytes DMA'd to TPB IRAM ; DRAM bytes -> HBM, patched by
      compose_ncfw_configs_for_barrier ; core released from reset via the DHAL kernel path
```

The split is absolute and is the carrier's defining property: **libncfw produces bytes and JSON; libnrt and the kernel do everything physical.** The DMA-to-IRAM, the DRAM-to-HBM template patch, and the reset-release through DHAL are all out of this part — they are exactly the syscalls the carrier provably does not make.

---

## 6. Verification Notes

> Every claim on this page was re-derived from the binary this pass with `nm`/`objdump`/`readelf`:
> - **3 exports / 4 imports** — `nm -D`: exports `libncfw_get_image` @`0x1179`, `libncfw_get_version` @`0x12fa`, `libncfw_ctx_log` @`0x1309` (exactly three `T`); imports exactly four undefined `U` symbols (`memset`, `snprintf`, `strlen`, `__stack_chk_fail`), all `@GLIBC`. `readelf -r` confirms the four `JUMP_SLOT` relocs bind those four; `GLOB_DAT` are the TM-clone/`gmon`/`cxa_finalize` weak stubs. `nm -D | rg ioctl|open|mmap|dlopen|write|read|socket|close` → **empty**. `readelf -d` `NEEDED` = `libc.so.6` only.
> - **`get_version` = 2** — whole body `mov $0x2,%eax; ret` (objdump).
> - **`get_image` switch** — null-out guard → `mov $0x16` (22/EINVAL); binary-search `cmpl $0x1c`/`$0x14`/`$0x5`/`$0xc` (with `ja`→default at each level) → per-arch `lea` of the four `*_bin`/`*_bin_size` symbols; default → `mov $0x2` (2/ENOENT); success → `mov $0x0`. Out-write offsets `0x10`/`0x18` (DRAM) then `0x0`/`0x8` (IRAM); size loads are `mov (%rax),%eax` (u32).
> - **`ctx_log` switch** — same `cmpl` tree; arms tail-call `{sunda,cayman,mariana,mariana_plus}_ncfw_ctx_log` @`0x1a12b`/`0x32ed2`/`0x4bc79`/`0x64a20` (arch_id dropped); default → `mov $0x16` (22/EINVAL).
> - **Serializer idiom** — every node opens with `budget = 0x100000 - strlen(out)`, `snprintf(out+strlen(out), budget, …)`, then sets `ncfw_log_buffer_full` (`.bss` @`0x95029`, four copies @`0x95029`..`0x9502c`) on `n < 0` or `n >= budget`. `ncfw_log_addr` @`0x41c3` carries a leading `char` (trail-comma) saved at `-0x34`.
>
> **[MED]** Silicon codenames (`sunda/cayman/mariana/mariana_plus` ↔ `v2/v3/v4/v4_plus`) are inferred by switch position and the `ctx_log` symbol strings; the authoritative coretype↔silicon table lives in libnrt ([Coretype Numbering Reconciliation](../arch/coretype-numbering.md)).

---

## Related Components

| Name | Relationship |
|---|---|
| `libnrt.so` (collectives) | the consumer: `dlopen`s libncfw, calls `get_image`, builds the CC-context that `ctx_log` reflects, does all device I/O |
| `libnrtucode_extisa.so` (Part XI) | the *sibling* carrier — GPSIMD/Q7 Vision-Q7 microcode; same passive provider model, different Xtensa TIE config |
| `compose_ncfw_configs_for_barrier` (libnrt) | patches the DRAM descriptor template before HBM upload — the producer of the fields `ctx_log` dumps |
| DHAL kernel path (Part III) | performs the DMA-to-IRAM and reset-release the carrier provably cannot |

## Cross-References

**Part X siblings (this subsystem)**
- [Overview](overview.md) — the two-job map and the three-Xtensa-core distinction
- [Embedded Payloads (8 Xtensa Blobs)](embedded-payloads.md) — the 8 `.rodata` firmware blobs `get_image` points into; carve, sizes, digests
- [Serializer Families (per-arch CC-context dumpers)](serializer-families.md) — the 38×4 JSON tree behind `ctx_log`, field-for-field
- [The NCFW Sequencer (Xtensa LX Disassembly)](ncfw-sequencer.md) — the on-device firmware the carrier carries
- [Firmware Upload Path (DKMS → Device DRAM)](upload-path.md) — the `dlopen`/version-gate boundary and the DMA/reset path the carrier does not own

**Consumers and the CC-context schema (Part IX)**
- [Overview: the Collective-Compute Architecture](../collectives/overview.md) — the consumer that builds the CC-context libncfw reflects
- [Coretype Numbering Reconciliation](../arch/coretype-numbering.md) — the canonical `5/12/20/28` ↔ silicon mapping
- [back to index](../index.md)
