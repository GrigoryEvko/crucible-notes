# The aws_hal_q7_* HAL

This page reconstructs the **host-side, per-architecture register programmer** for the
Vision-Q7 "Pool" engine — the GPSIMD/custom-op DSP — as it ships inside `libnrt.so`. The
`aws_hal_q7_*` family is the slice of the vendored **KaenaHal** (version `2.31.0.0`) that lays
out the BAR0 register map for the Q7 cluster, validates and installs the Q7 IRAM/DRAM ucode
into every core over programmed-IO, programs the per-engine NX/Q7 local registers, and drives
the full reset → start bring-up (run-stall release). It is the host counterpart of the
device-side bring-up documented in [Boot / Reset Sequence](../uarch/boot-reset.md): the HAL is
what BAR0-writes the image and clears the run-stall that the device reset vector waits behind.

Everything here is **byte-pinned to a shipped artifact**: the host x86-64 ELF
`libnrt.so.2.31.24.0` (`122,956,336` B, BuildID `8bb57aba…387c102e`, `2.31.24.0` git
`0b044f4ce`, **with `debug_info`, not stripped**), disassembled with stock `objdump` at the
addresses recovered from the IDA function sidecar. For `.text`/`.rodata` the rule
**VMA == file offset** holds (confirmed via `readelf -SW`: `.text` VMA `0x3dbc0` / off `0x3dbc0`;
`.rodata` VMA `0x7cf000` / off `0x7cf000`), so every address below is both a VA and a file
offset. The KaenaHal source-path `__FILE__` strings (`…/KaenaHal-2.31.0.0/…/src/{common,sunda,
cayman,mariana}/…`) are read out of `.rodata` and are the per-arch navigation key — the Q7 HAL
lives in `common/q7/aws_hal_q7.c`, the Pool bring-up in `common/tpb/aws_hal_stpb_pooling.c`,
and the per-arch register offsets in `{sunda,cayman,mariana}/arch/aws_hal_arch_offsets_*.c`.
Where prompt-level folklore or a sibling note disagrees with the binary, **the binary wins** —
and an in-place **CORRECTION** callout says so.

Confidence tags follow [the Confidence & Walls Model](../reference/confidence-model.md):
`OBSERVED` = a byte/string/instruction read from a shipped artifact; `INFERRED` =
reasoned over OBSERVED facts; `CARRIED` = consolidated from a cited cross-page anchor; crossed
with `HIGH`/`MED`/`LOW`. The page default is `[HIGH/OBSERVED]`; claims that depart from it carry
an explicit tag. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE** (orientation).

> **WALL — v5 (Maverick / NC-v5) is not in this binary.** This `libnrt.so` registers Q7 HAL
> implementations for exactly three architectures: Sunda (NC-v2), Cayman (NC-v3), Mariana
> (NC-v4). Maverick (NC-v5) is header-OBSERVED only elsewhere in the corpus; **any v5 claim on
> this page is INFERRED** by extrapolation from the Cayman/Mariana pair, and the `arch_id 36`
> binding for v5 is itself INFERRED. The byte-grounded walls below are Sunda/Cayman/Mariana.

---

## 0. One-screen orientation — what this HAL is

`aws_hal_q7_*` is ~16 Q7-named symbols plus their supporting per-arch NX/Q7 local-register
writers, all inside KaenaHal. It does **four** things:

1. **Parameter lookup** — `aws_hal_get_q7_params{,_<arch>}` returns the Q7 engine geometry
   tuple `{base_offset, iram_size, dram_size, iram_rsvd, dram_rsvd, num_q7}`, keyed on an
   `al_hal_q7_owner` value (`POOLING` vs `COMPUTE_CLUSTER`). [§3](#3-aws_hal_get_q7_params)
2. **Ucode install** — `aws_hal_q7_ucode_eng_init{,_<arch>}` validates the `{iram,dram}` image
   sizes against the params, then for each of `num_q7` cores streams the **DRAM then IRAM**
   image into the engine over BAR0 via `write_padded → al_mem_write_buf → axi_write →
   ndl_bar_write`. [§4](#4-aws_hal_q7_ucode_eng_init)
3. **Swap / DKL** — `aws_hal_q7_swap_table` / `_swap_table_get_register_offsets` /
   `_swap_file_io_table`: the runtime dynamic-kernel-load hook. **REAL only on Sunda** (pokes
   per-core local regs); **NO-OP STUBS on Cayman + Mariana** (their DKL is firmware-resident).
   [§5](#5-the-swap-dynamic-kernel-load-dkl-mechanism)
4. **Engine control** — the surrounding `aws_hal_stpb_<arch>_pooling_init`: the full Pool
   bring-up (`regs_init → ucode_seq_init → q7_ucode_eng_init → [hw_decode] → dma_init →
   release_seq_run_stall → release_eng_run_stall`), i.e. reset/start/halt control plus the
   SEQ/Q7 local-register programming. [§6](#6-the-per-engine-local-register-map),
   [§7](#7-the-full-pool-bring-up-sequence-reset-start-halt-control)

The single dispatch substrate is a global struct **`kaena_khal` @`0xcaeb80`** whose per-arch
function pointers are filled at init. `al_hal_tpb_get_arch_type()` (reads `hal_target` @`0xcaeb60`,
asserts the value is in `{1..4}`) is checked first, then the generic trampoline tail-jumps
through the populated slot. **The writes never leave the BAR0 MMIO aperture** (`axi_write` /
`ndl_bar_write`) **or the per-CSR aperture** (`csr_write`). No DMA installs the ucode — it is a
programmed-IO copy.

---

## 1. The Q7 HAL symbol set

Recovered with the IDA function sidecar; each address below is independently disassembled in
the sections that follow.

**Generic arch-dispatching trampolines** (`.text`):

| Address | Symbol | Dispatches to slot |
|---|---|---|
| `0x451080` | `aws_hal_q7_ucode_eng_init` | `khal+0x3a8` |
| `0x451120` | `aws_hal_q7_swap_table` | `khal+0x390` |
| `0x4511a0` | `aws_hal_q7_swap_table_get_register_offsets` | `khal+0x398` |
| `0x451220` | `aws_hal_q7_swap_file_io_table` | `khal+0x3a0` *(INFERRED slot, MED)* |
| `0x44bfb0` | `aws_hal_get_q7_params` | `khal+0xb8` |
| `0x44c970` | `aws_hal_arch_get_xt_local_reg_offset` | `khal+0xf0` |
| `0x44bca0` | `al_hal_tpb_get_arch_type` | reads `hal_target` @`0xcaeb60` |

**Per-arch implementations:**

| Arch | get_q7_params | ucode_eng_init | swap_table | …get_register_offsets | swap_file_io_table | get_xt_local_reg_offset |
|---|---|---|---|---|---|---|
| **Sunda** (NC-v2) | `0x478f90` | `0x46b9a0` | `0x46b7d0` (REAL) | `0x46b890` (REAL) | `0x46b8e0` (REAL) | `0x479070` |
| **Cayman** (NC-v3) | `0x47ae70` | `0x4714c0` | `0x471470` (STUB) | `0x471480` (STUB) | `0x4714b0` (STUB) | `0x47afe0` |
| **Mariana** (NC-v4) | `0x477610` | `0x464b00` | `0x464ab0` (STUB) | `0x464ac0` (STUB) | `0x464af0` (STUB) | *(Cayman-like, MED)* |

**Per-arch Pool bring-up callers** (`aws_hal_stpb`): `0x46e8d0` `stpb_sunda_pooling_init`,
`0x4737b0` `stpb_cayman_pooling_init`, `0x467d30` `stpb_mariana_pooling_init`, `0x458350`
`aws_hal_stpb_init` (top), `0x45c170` `aws_hal_stpb_pooling_init` (dispatch via `khal+0x368`).

Provenance `.rodata` strings (binary-derived, citeable): `common/q7/aws_hal_q7.c`,
`common/tpb/aws_hal_stpb_pooling.c`, `common/arch/aws_hal_arch_offsets.c`,
`{sunda,cayman,mariana}/arch/aws_hal_arch_offsets_*.c`.

---

## 2. The generic dispatch model (kaena_khal slot table)

Every generic `aws_hal_q7_*` function follows **one** pattern. Disassembling
`aws_hal_get_q7_params @0x44bfb0`:

```c
// aws_hal_get_q7_params @0x44bfb0 — the canonical trampoline shape.
// Args are forwarded UNCHANGED; the per-arch impl has the same signature.
int aws_hal_get_q7_params(int owner, u64 *base, u64 *iram_sz, u64 *dram_sz,
                          u64 *iram_rsvd, u64 *dram_rsvd, u32 *num_q7) {
    if (al_hal_tpb_get_arch_type() == 0 /* AL_HAL_TPB_ARCH_TYPE_INVALID */)
        __assert_fail("al_hal_tpb_get_arch_type() != AL_HAL_TPB_ARCH_TYPE_INVALID");
    void *fn = *(void **)(&kaena_khal + 0xb8);     // khal+0xb8 = get_q7_params slot
    if (fn == NULL)
        __assert_fail("kaena_khal.khal_arch.get_q7_params");
    return ((q7_params_fn)fn)(owner, base, iram_sz, dram_sz,
                              iram_rsvd, dram_rsvd, num_q7);   // jmp *%rax (tail call)
}
```

The disassembly shows it literally: `call al_hal_tpb_get_arch_type` → `test %eax,%eax; je <assert>`
→ `lea 0x862b95(%rip),%rax  # caeb80 <kaena_khal>` → `mov 0xb8(%rax),%rax` → `test %rax,%rax;
je <assert>` → restore the saved args (`%r14→0x50(%rsp)`, `%r15→%r8`, `%r13→%rcx`, …) →
`jmp *%rax`. The two `__assert_fail` arms carry the exact `__PRETTY_FUNCTION__` and the assert
text.

**Confirmed `kaena_khal` slot offsets** (the per-Q7 dispatch table fields): `[HIGH/OBSERVED;
0x3a0 MED]`

| Slot | Field |
|---|---|
| `khal+0x0b8` | `get_q7_params` |
| `khal+0x0f0` | `get_xt_local_reg_offset` |
| `khal+0x368` | `stpb_pooling_init` |
| `khal+0x390` | `q7_swap_table` |
| `khal+0x398` | `q7_swap_table_get_register_offsets` |
| `khal+0x3a0` | `q7_swap_file_io_table` *(adjacency-inferred, MED)* |
| `khal+0x3a8` | `q7_ucode_eng_init` |

`al_hal_tpb_get_arch_type @0x44bca0` reads `hal_target` @`0xcaeb60`; valid `{1..4}`; `0` asserts.
The enum (carried from the arch-type model): `0=INVALID`, `2=Sunda` (NC-v2), `3=Cayman` (NC-v3),
`4=Mariana` (NC-v4). Value `1` passes the range check but no Q7 impl is registered for it — a
legacy/Inf1 slot. `[HIGH/OBSERVED; enum binding CARRIED]`

### 2.1 The two physical write primitives

The HAL ultimately reaches BAR0 through exactly two primitives.

**(a) Bulk image write — `al_mem_write_buf @0x265990`.** Asserts `dst` and `len` are 4-byte
aligned, then `len >>= 2` to a word count and calls `axi_write(dst, src, words)`:

```c
// al_mem_write_buf @0x265990
//   26599a: test $0x3,%al  ; jne <assert>     -- dst addr 4B-aligned
//   26599e: test $0x3,%dl  ; jne <assert>     -- len     4B-aligned
//   2659a3: shr  $0x2,%edx                    -- len >>= 2  => word count
//   2659a6: call axi_write                    -- 315850
//   2659ab: test %eax,%eax ; jne <assert>     -- axi_write must succeed
int al_mem_write_buf(u64 dst, const void *src, u32 len) {
    assert((dst & 3) == 0 && (len & 3) == 0);
    return axi_write(dst, src, len >> 2);
}
```

`axi_write @0x315850` resolves the BAR-region table via `tdrv_arch_ops+0x1b0` (a callback
returning an array of `0x30`-byte records `{valid@0, base@8, limit@0x10, bar-handle@0x28}`),
finds the record whose `[base, limit-4]` spans `dst`, and calls `ndl_bar_write(handle, 0, src,
words, &ctx)` — the BAR0 MMIO write down to the `ndl` driver portal.

**(b) Single-CSR write — `al_reg_write32 @0x265c50`.** `csr_write @0x315820 →
csr_write_array(addr,&val,1)` → same BAR-region resolve → `ndl_bar_write` of one 32-bit word.
`al_reg_read32 @0x2658a0` is the read peer. **Every per-register HAL helper** (run-stall,
dma_ctrl, engine_base, …) uses (b).

> **NOTE.** Image payload goes through (a), word-blasted; control registers through (b). Both
> land in BAR0 via `ndl_bar_write`. **No DMA is used to install the ucode — it is a
> programmed-IO copy.**

---

## 3. `aws_hal_get_q7_params` — the Q7 engine parameter map

Recovered signature (x86-64 SysV ABI):

```c
// owner = al_hal_q7_owner: 2 = AL_HAL_POOLING_Q7 (the GPSIMD/custom-op engine)
//                          5 = AL_HAL_COMPUTE_CLUSTER_Q7 (collectives CC engine)
void aws_hal_get_q7_params(int owner /*edi*/, u64 *base /*rsi*/, u64 *iram_size /*rdx*/,
                           u64 *dram_size /*rcx*/, u64 *iram_rsvd /*r8*/, u64 *dram_rsvd /*r9*/,
                           u32 *num_q7 /*[rsp+0x10]*/);
```

### 3.1 Sunda — pooling only

`aws_hal_get_q7_params_sunda @0x478f90` accepts **`owner==2` only**; any other value falls
through to `__assert_fail("aws_hal_get_q7_params_sunda")` (note: no CC branch). The constants
are stored as immediate `movq`s, read directly from the disasm:

```c
// aws_hal_get_q7_params_sunda @0x478f90
//   478f94: cmp $0x2,%edi ; jne <assert>          -- POOLING only
//   478f9e: movq $0x2980000,(%rsi)   base_offset
//   478fa5: movq $0x10000, (%rdx)    iram_size  = 64 KiB
//   478fac: movq $0x10000, (%rcx)    dram_size  = 64 KiB
//   478fb3: movq $0x0,     (%r8)     iram_rsvd  = 0
//   478fba: movq $0x0,     (%r9)     dram_rsvd  = 0
//   478fc1: movl $0x8,     (%rax)    num_q7     = 8   (%rax = num_q7 ptr from [rsp+0x10])
```

→ Sunda Q7 = **8 cores, 64 KiB IRAM + 64 KiB DRAM each, no reserved tail.**

### 3.2 Cayman / Mariana — dual owner, byte-identical to each other

`aws_hal_get_q7_params_cayman @0x47ae70` and `aws_hal_get_q7_params_mariana @0x477610` return
the **same geometry**; only `num_q7` depends on the owner. Disasm of the Cayman variant:

```c
// aws_hal_get_q7_params_cayman @0x47ae70
//   47ae74: cmp $0x2,%edi ; je 0x47aeb1 (POOLING => num_q7=8)
//   47ae7e: cmp $0x5,%edi ; jne <assert>     (else __assert_fail aws_hal_get_q7_params_cayman)
//   ---- owner==5 path (CC), num_q7=4 ----   ---- owner==2 path (POOL), num_q7=8 ----
//   movq $0x3100000,(%rsi)   base_offset
//   movq $0x20000, (%rdx)    iram_size  = 128 KiB
//   movq $0x40000, (%rcx)    dram_size  = 256 KiB
//   movq $0x60000, (%r8)     iram_rsvd  = 0x60000
//   movq $0x40000, (%r9)     dram_rsvd  = 0x40000
//   movl $0x4 / $0x8, (%rax) num_q7     = 4 (CC) / 8 (POOLING)
```

→ Cayman/Mariana Q7 = **128 KiB IRAM + 256 KiB DRAM per core** (double Sunda's IRAM, 4× DRAM),
with a **reserved IRAM tail `0x60000`** and **reserved DRAM tail `0x40000`** — the DKL/ext-ISA
staging window (see [§5](#5-the-swap-dynamic-kernel-load-dkl-mechanism)).

> **CROSS-CHECK.** The 256 KiB per-core Q7 DRAM matches `POOL_Q7_CORE{n}_DRAM` (256 KiB @ SoC
> `0x2803180000 + n*0x100000`) on the device-side SoC map. `base_offset` is the **BAR0-relative**
> offset of the Q7 IRAM aperture; the nrtucode layer adds `aws_get_tpb_addr(pcore)` to turn it
> into the absolute SoC address. `[HIGH structure; base→SoC composition CARRIED]`

### 3.3 Per-arch geometry summary

| Field | Sunda (v2) | Cayman (v3) | Mariana (v4) |
|---|---|---|---|
| `base_offset` | `0x2980000` | `0x3100000` | `0x3100000` |
| `iram_size` / core | `0x10000` (64 K) | `0x20000` (128 K) | `0x20000` (128 K) |
| `dram_size` / core | `0x10000` (64 K) | `0x40000` (256 K) | `0x40000` (256 K) |
| `iram_rsvd` (DKL tail) | `0` | `0x60000` | `0x60000` |
| `dram_rsvd` (DKL tail) | `0` | `0x40000` | `0x40000` |
| `num_q7` (POOLING / CC) | `8` / — | `8` / `4` | `8` / `4` |
| owner support | POOLING only | POOLING + CC | POOLING + CC |

---

## 4. `aws_hal_q7_ucode_eng_init` — the ucode install

Recovered signature:

```c
// imgs points at TWO nrt_ucode_img records:
//   imgs[0] = IRAM { bin@+0x00, size@+0x08 }
//   imgs[1] = DRAM { bin@+0x10, size@+0x18 }
int aws_hal_q7_ucode_eng_init(void *tpb_handle /*rdi*/, u64 image_base /*rsi*/,
                              nrt_ucode_pair *imgs /*rdx*/, void *swap_cb /*rcx*/,
                              void *swap_ctx /*r8*/, int owner_idx /*r9d*/);
```

### 4a. Owner mapping — the v2-vs-v3/v4 split

- **Sunda** (`@0x46b9a0`): `r9d` must be `0`. The disasm opens `test %r9d,%r9d; jne <reject>`;
  the reject arm logs `"Invalid q7 owner %u for v2 (only pooling supported)"` and returns `-1`.
  Internally it calls `aws_hal_get_q7_params_sunda(owner=2)`.
- **Cayman** (`@0x4714c0`) / **Mariana** (`@0x464b00`): `r9d==0` → `get_q7_params(owner=2,
  POOLING)`; `r9d==1` → `get_q7_params(owner=5, COMPUTE_CLUSTER)`; else → log
  `"Invalid q7 owner %u"` and return `-1`.

→ The HAL's `owner_idx ∈ {0,1}` maps to the `al_hal_q7_owner ∈ {2,5}`. Sunda is pooling-only;
Cayman/Mariana are dual-owner.

### 4b. Size validation — the host-side admission gate

After resolving params, the loader compares each image size against its aperture. Both error
strings are present verbatim in `.rodata`.

```c
if (imgs[0].size /* IRAM, +0x08 */ > iram_size)
    { al_hal_log("IRAM ucode too big! doesn't fit, size=%lu", imgs[0].size); return -1; }
if (imgs[1].size /* DRAM, +0x18 */ > dram_size)
    { al_hal_log("DRAM ucode too big! doesn't fit, size=%lu", imgs[1].size); return -1; }
```

This is the host-side per-image bounds gate complementing the device prelink segment-bounds
check and the device `NUM_POOL_CORES` assert (security posture; CARRIED).

### 4c. The per-core install loop

```c
// Per-core IRAM/DRAM destination addresses are computed from base + iram_rsvd/dram_rsvd and
// image_base (the iram_total/dram_total stride). The loop bound is the params' num_q7 word,
// read back from [rsp+0x10] into the loop counter (0x24(%rsp)).
for (u32 core = 0; core < num_q7; core++) {
    // (i) DRAM FIRST — the data image lands before the code image:
    if (write_padded(dst_dram[core], imgs[1].bin, imgs[1].size, dram_alloc_size,
                     swap_cb, swap_ctx, "POOL eng dram") != 0)
        { al_hal_log("Failed to load pooling q7 dram core %u", core); return -1; }
    // (ii) IRAM SECOND:
    if (write_padded(dst_iram[core], imgs[0].bin, imgs[0].size, iram_alloc_size,
                     swap_cb, swap_ctx, "POOL eng iram") != 0)
        { al_hal_log("Failed to load pooling q7 iram core %u", core); return -1; }
}
```

> **QUIRK — broadcast-by-replication.** With `num_q7 == 8` for POOLING, the **same image is
> written to all 8 cores** — the engine is an 8-core SPMD cluster running one identical program
> (consistent with the device-side "all-POOL broadcast", total_cpus == 8).

> **GOTCHA — order is DRAM-then-IRAM, per core.** A reimplementation that writes IRAM first (or
> all DRAM then all IRAM) diverges from the shipped order. The data image is installed before
> the code image on each core.

Cayman and Mariana loop bodies are byte-identical to each other and structurally identical to
Sunda; only the owner-branch and the params differ.

### 4d. `write_padded @0x473eb0` — the pad+copy+write primitive

```c
// write_padded @0x473eb0
//   473ec3: cmp %r8,%rcx          assert(padded_size >= size)
//   473ee3: call al_zalloc        buf = al_zalloc(padded_size)   -- zero-fills the pad tail
//   473ee8: test %rax,%rax ; je <"calloc failed! size=%lu">
//   473efd: call memcpy           memcpy(buf, src, size)
//   473f02: test %r12,%r12 ; je <else>     -- swap_cb present?
//   473f1a: call *%r12            rc = swap_cb(ctx_lo, ctx_hi, buf, padded_size, owner)  (DKL hook)
//   473f26: call al_free          al_free(buf)
int write_padded(u64 dst, const void *src, u64 size, u64 padded_size,
                 swap_fn swap_cb, void *swap_ctx, const char *tag) {
    assert(padded_size >= size);
    void *buf = al_zalloc(padded_size);        // zero pad tail
    if (!buf) { al_hal_log("calloc failed! size=%lu", padded_size); return -1; }
    memcpy(buf, src, size);
    int rc = swap_cb ? swap_cb(swap_ctx_lo, swap_ctx_hi, buf, padded_size, owner)  // DKL seam
                     : al_mem_write_buf(dst, buf, padded_size);                    // BAR0 PIO
    al_free(buf);
    return rc;   // on swap_cb error: "memcpy_fn(%p) failed with %d"
}
```

> **NOTE — the zero-pad matters.** The image is zero-padded to the per-core aperture size
> **before** the word-blast, so the engine IRAM/DRAM is fully initialized with no stale tail.
> The optional `swap_cb` is the seam the nrtucode layer can interpose to stream/swap images
> instead of a straight MMIO copy (see [The nrtucode Subsystem](nrtucode-bringup.md)); in the
> `stpb` bring-up path it is passed through from the stpb config.

---

## 5. The swap / dynamic-kernel-load (DKL) mechanism

`aws_hal_q7_swap_table` / `_swap_table_get_register_offsets` / `_swap_file_io_table` are the
runtime image-swap (DKL) control surface. The critical finding is **per-arch**.

### 5a. Sunda — REAL (pokes per-core NX local registers)

`aws_hal_q7_swap_table_sunda @0x46b7d0` computes
`base = aws_hal_arch_get_xt_local_reg_offset(...)` then `al_reg_write32`s **three** registers —
the swap-table descriptor triple. The disasm shows the three `lea 0x83c/0x840/0x844(%rbx),%rdi;
call al_reg_write32` sequences (each preceded by an `al_hal_log`):

```c
// aws_hal_q7_swap_table_sunda @0x46b7d0
//   46b7f4: call aws_hal_arch_get_xt_local_reg_offset -> base in %rbx
//   46b819: lea 0x83c(%rbx),%rdi ; call al_reg_write32   nx_rsvd_space0  (value 0 = base/lo)
//   46b845: lea 0x840(%rbx),%rdi ; call al_reg_write32   nx_rsvd_space1  (value 1 = len/hi)
//   46b870: lea 0x844(%rbx),%rdi ; call al_reg_write32   nx_rsvd_space2  (value 2 = index/ctrl)
```

The `nx_rsvd_space0..2` registers are **repurposed reserved local regs** carrying the DKL
swap-table pointer triple. `aws_hal_q7_swap_table_get_register_offsets_sunda @0x46b890`
returns those three addresses (`base+0x83c/0x840/0x844`) so the firmware/nrtucode layer can DMA
a swap descriptor to them directly. `aws_hal_q7_swap_file_io_table_sunda @0x46b8e0`
`al_reg_write32`s the **host↔device file/stdio I/O table**: `base+0x70` `dma_tx_ring_base`,
`base+0x74` `dma_tx_ring_length`, `base+0x838` `q7_intr_info` — wiring the `pool_stdio` channel
into the SEQ DMA TX ring + the Q7 interrupt-info register.

### 5b/5c. Cayman + Mariana — NO-OP stubs

```c
// aws_hal_q7_swap_table_cayman @0x471470  (Mariana @0x464ab0 byte-identical):
//   471470: xor %eax,%eax ; ret
// aws_hal_q7_swap_table_get_register_offsets_cayman @0x471480  (Mariana @0x464ac0):
//   writes 0 to each of the 3 out-pointers (null-guarded), then repz ret
// aws_hal_q7_swap_file_io_table_cayman @0x4714b0  (Mariana @0x464af0):
//   4714b0: xor %eax,%eax ; ret
```

> **CORRECTION — only Sunda uses the HAL swap registers.** An earlier framing treated the
> `swap_table` family as the *generic* DKL mechanism for all arches. The binary disagrees: on
> Sunda the DKL swap + the stdio/file-IO table are programmed by the **host** poking per-core NX
> local registers; on **Cayman and Mariana these HAL hooks are empty** — DKL is handled
> **entirely on the device firmware side** (the dynamic-kernel-load path) over the prelinked
> ext-ISA image streamed into the **reserved IRAM/DRAM tail** (`iram_rsvd 0x60000` /
> `dram_rsvd 0x40000` of [§3](#3-aws_hal_get_q7_params)). **This is precisely why Cayman/Mariana
> carry reserved IRAM/DRAM tails that Sunda does not** — those tails are the DKL staging region,
> managed by firmware, not by the host HAL. `[HIGH for the stub/real split; firmware-resident DKL
> CARRIED]`

---

## 6. The per-engine local-register map

The HAL addresses every engine's control registers as `tpb_base + per-engine-type local-reg base
+ register-offset`. The base computer is
`aws_hal_arch_<arch>_get_xt_local_reg_offset(u64 tpb_base /*rdi*/, int eng_type /*esi*/,
int owner /*dl*/) → tpb_base + table[eng_type]`, with the Q7 type folding `owner` into the base.

### 6a. Sunda — 5 engine types

`aws_hal_arch_sunda_get_xt_local_reg_offset @0x479070` is a jump table (`cmp $0x4,%esi; ja
<assert>`; `movslq (%rcx,%rsi,4),%rax; add %rcx,%rax; jmp *%rax` with table @`0x9f2700`). The
five arms read as immediate `mov $imm,%eax; add %rdi,%rax; ret`:

| `eng_type` | Base (added to `tpb_base`) | Engine |
|---|---|---|
| `0` | `0x2660000` | — |
| `1` | `0x2460000` | — |
| `2` | `0x2960000` | **POOL** (the GPSIMD seq engine; used by run-stall) |
| `3` | `0x2b60000` | — |
| `4` | `owner==1 ? 0x2860000 : 0x60000` | **Q7** (the 8 compute cores) |

`esi>4 → __assert_fail`. The Q7 arm (`@0x4790ab`) is the `cmp $0x1,%dl; sbb %rax,%rax;
and $0xfffffffffd800000,%rax; add $0x2860000,%rax` idiom — i.e. `owner==1 ? 0x2860000 : 0x60000`.

### 6b. Cayman — 6 engine types (incl. the bit-35 secure-aperture fold)

`aws_hal_arch_cayman_get_xt_local_reg_offset @0x47afe0` (table @`0x9f41e0`):

| `eng_type` | Base | Engine |
|---|---|---|
| `0` | `0x2660000` | — |
| `1` | `0x2460000` | — |
| `2` | `0x3060000` | **POOL** (moved up vs Sunda's `0x2960000`) |
| `3` | `0x2b60000` | — |
| `4` | `owner==0 ? 0x60000 : 0x2860000` | **Q7** |
| `5` | `0x3060000` | — |

`esi>5 → __assert_fail`. The Q7 `owner!=0` base is computed as `0x802860000` **minus** a
`0xfffffff800000000` add (i.e. subtract `0x800000000`) → `0x2860000`. That `0x8_00000000` is
**bit 35** — the `PEB_APB_IO` secure/privileged aperture — masked off in the host view. (The
neighboring `aws_hal_get_eng_hw_decode_table_params_cayman @0x47af40` shows the same
`movabs $0xfffffff800000000` fold for its `0x802b88000` base, independently confirming the idiom.)

> **NOTE — Mariana eng-type table is treated as Cayman-like (MED).** Mariana's
> `get_xt_local_reg_offset` was not separately tabled here, but its swap stubs and
> `ucode_eng_init` mirror Cayman, and its run-stall path uses the Cayman-style real eng-release
> (see [§7](#7-the-full-pool-bring-up-sequence-reset-start-halt-control)). `[MED/INFERRED]`

### 6c. The HAL-flattened NX/Q7 local-register offsets

Relative to the [§6a](#6a-sunda-5-engine-types)/[§6b](#6b-cayman-6-engine-types-incl-the-bit-35-secure-aperture-fold)
base, recovered from the per-register writer functions (each is a one-line
`add $off,%rdi; jmp al_reg_write32` thunk, or names the register in its `al_hal_log`):

| Offset | HAL name | Role |
|---|---|---|
| `+0x004` | `release_run_stall` | write `0` = release. **SEE [§6d](#6d-the-offset-vs-spec-reconciliation) nuance** |
| `+0x014` | `run_state.state` | read; `bit0 & 0x1` = run/idle flag |
| `+0x028` | `engine_base_address_lo` | SEQ instruction-fetch base, low |
| `+0x02c` | `engine_base_address_hi` | …high |
| `+0x040` | `dma_ctrl` | SEQ DMA enable/start (written enable, then start) |
| `+0x058` | `dma_rx_ring_head_ptr` | |
| `+0x05c` | `dma_rx_ring_tail_ptr` | |
| `+0x0..` | `dma_rx_ring_tail_inc_ptr` | the iDMA doorbell (+next) |
| `+0x070` | `dma_tx_ring_base` | file-IO/stdio ring base ([§5a](#5a-sunda-real-pokes-per-core-nx-local-registers)) |
| `+0x074` | `dma_tx_ring_length` | |
| `+0x210` | `mem_window2_lo` | NX address-translation window 2, low |
| `+0x214` | `mem_window2_hi` | …high |
| `+0x804` | `instr_dbg_ctrl` | instruction-debug-level control |
| `+0x838` | `q7_intr_info` | |
| `+0x83c` | `nx_rsvd_space0` | DKL swap-table triple ([§5a](#5a-sunda-real-pokes-per-core-nx-local-registers)) |
| `+0x840` | `nx_rsvd_space1` | |
| `+0x844` | `nx_rsvd_space2` | |
| `+0xa00` | `notific_sw_queue_num0` | completion/notification SW-queue base ([§7](#7-the-full-pool-bring-up-sequence-reset-start-halt-control) STEP 1) |

### 6d. The offset-vs-spec reconciliation

This is the single most important reimplementation trap on the page. `[HIGH/OBSERVED that the
offsets differ; MED on the cause]`

The **device-side register spec** ([the `tpb_xt_local_reg` CSR
block](../control/csr/tpb-xt-local-reg.md), Part 13) lays the NX bundle out as
`release_run_stall@0x0000`, `start_ctrl@0x0004`,
`run_state@0x0008`, `dma_rx_base@0x000C`, `dma_tx_base@0x0010`, `instr_halt_ctrl@0x0014`,
`intr_ctrl@0x0018`, `intr_info@0x001C`; the Q7 bundle @`0x3000` with
`release_run_stall@0x3000 [7:0]` reset `0xFF`, `start_ctrl@0x3004`,
`run_state_0..7@0x3008..0x3024`, `intr_ctrl@0x3028`, `intr_info_0..7@0x302C..0x3048`; a window
bundle @`0x2000` (stride `0x1C`); `hw_decode@0x4000`; `tensor_replace@0x5000`; `notific@0x6000`.

The **host HAL uses a different, flattened offset table** (the [§6c](#6c-the-hal-flattened-nxq7-local-register-offsets)
numbers) whose register **names** match the device spec's logical registers but whose **byte
offsets do not coincide** with the spec's bundle offsets. Concretely:

- The HAL writes "`release_run_stall`" at **`+0x04`** — which is `start_ctrl`'s offset in the
  device spec.
- The HAL reads "`run_state`" at **`+0x14`** — which is `instr_halt_ctrl`'s device offset.

KaenaHal was generated from a **separate (Sunda-physical / register-spec) layout**, not the
device-side per-arch register spec. The two are **two views of the same registers** and must not
be conflated.

> **GOTCHA.** For a reimplementation: trust the device CSR spec
> ([`tpb_xt_local_reg`](../control/csr/tpb-xt-local-reg.md), Part 13)
> for the **device register semantics**, and the [§6c](#6c-the-hal-flattened-nxq7-local-register-offsets)
> table for the **host HAL's actual write offsets**. The functional intent is unchanged — write
> `0 → release run-stall`, read low bit → running — but the *byte* offsets differ. The `+N` shift
> is OBSERVED; the "separate spec" cause is MED (inferred from the consistent shift and the
> `sunda_*` string prefix). `[HIGH offsets / MED cause]`

---

## 7. The full Pool bring-up sequence (reset / start / halt control)

`aws_hal_stpb_init @0x458350` (top) runs `aws_hal_notific_init` (per-TPB notification ring) →
`stpb_eng_notif_enable` / `evsem_notif_enable` / `error_notif_enable` (arm the 3 notification
classes — ERROR/EVT_SEM/INST) → `aws_hal_stpb_regs_init` → `aws_hal_stpb_pooling_init @0x45c170`
(dispatch via `khal+0x368` to the per-arch impl). The per-arch `pooling_init` is the actual Q7
bring-up. The **Sunda** variant `aws_hal_stpb_sunda_pooling_init @0x46e8d0` recovers this config
struct (`rdi=cfg`):

| `cfg` offset | Field |
|---|---|
| `+0x00` | tpb device/handle (passed as `rdi` to every sub-init) |
| `+0x08` | `tpb_mem_handle` (log `"tpb_mem_handle=%p, tpb_regs=%p"`) |
| `+0x10` | `image_base` (`rsi` to `ucode_eng_init` / `ucode_seq_init`) |
| `+0x18` | `swap_cb` (`rcx` → `write_padded` swap callback) |
| `+0x20` | `swap_ctx` (`r8` → `write_padded` swap context) |
| `+0x28` | SEQ ucode `{iram,dram}` pair (→ `ucode_seq_init` `rdx`) |
| `+0x48` | Q7 ucode `{iram,dram}` pair (→ `q7_ucode_eng_init` `rdx`; [§4](#4-aws_hal_q7_ucode_eng_init)) |
| `+0x88` | DMA config (→ `dma_init` `rsi`) |
| `+0xe0/0xe4/0xe8` | engine-id / cluster bitfields (→ `regs_init`) |

The steps, with the disassembled addresses:

```text
STEP 1  regs_init @0x46e5d0
        - instr_debug_level_set @0x46e4b0: arch_stpb_to_instr_debug_level();
          write seq instr-debug ctrl @ tpb_regs+0x104; write NX-local instr_dbg_ctrl @+0x804;
          read NX-local run_state @+0x14 (bit0).
        - read tpb_regs+0xa00 (notific_sw_queue_num0); splice engine-id nibbles from
          cfg+0xe4 (<<0x14, mask 0xF00000) and cfg+0xe8 (<<0x18 & <<0x10); write back
          tpb_regs+0xa00.  => programs the notification SW-queue identity for the Pool eng.

STEP 2  ucode_seq_init @0x46e6a0
        get_seq_params_sunda(); validate seq iram/dram sizes;
        write_padded(seq IRAM "iram") + write_padded(seq DRAM "dram");
        init_nx_registers @0x46cf10; write NX-local engine_base_address_lo@+0x28 /
        _hi@+0x2c (the SEQ I-fetch base from cfg+0x10).

STEP 3  q7_ucode_eng_init(handle, image_base, &cfg[0x48], swap_cb, swap_ctx, owner_idx=0)
        -> §4: streams the (possibly overridden) Q7 image into all 8 cores' IRAM/DRAM over BAR0.

STEP 3b (CAYMAN/MARIANA only) hw_decode_table_init(&cfg[0x68])
        programs the HW-decode CAM/table (see HW-Decode CAM-Table Programming), interposed
        between Q7 install and dma_init.  (Sunda HAS aws_hal_..._hw_decode_table_init @0x46e8c0
        but its pooling_init does not call it in-line; MED whether Sunda programs it elsewhere.)

STEP 4  dma_init @0x46e840
        - dma_ctrl(base, 0)  : write NX-local dma_ctrl @+0x40 = 0  (DMA disabled/reset).
        - if cfg+0x88[0]!=0  : dma_regs(rx: cfg+0x18/0x20/0x28, sel=0) then
                               dma_regs(tx: cfg+0x40/0x48/0x50, sel=1) — programs
                               mem_window2_lo/hi @+0x210/+0x214 and the dma_rx ring
                               head/tail/tail_inc @+0x58/+0x5c/+next (the SEQ iDMA
                               instruction-cache-fill ring).
        - dma_ctrl(base, 1)  : write dma_ctrl @+0x40 = 1  (DMA enabled/started).

STEP 5  release_seq_run_stall @0x46e7e0  => RELEASES THE NX/SEQ CORE FROM RESET RUN-STALL.
STEP 6  release_eng_run_stall  @0x46e810  => RELEASES (or not) the 8 Q7 cores — see §7.1.
```

### 7.1 The run-stall release — the Sunda-vs-Cayman+ split

This is the heart of reset/start control, and the disasm is unambiguous. **Both** STEP 5 and
STEP 6 resolve the same base via `get_xt_local_reg_offset(eng_type=2 POOL, owner=1)`, then
tail-jump a per-arch writer.

```c
// aws_hal_stpb_sunda_pooling_release_seq_run_stall @0x46e7e0
//   46e7e4: mov $0x2,%esi ; mov $0x1,%edx          eng_type=2 (POOL), owner=1
//   46e7ee: call aws_hal_arch_sunda_get_xt_local_reg_offset
//   46e7f3: xor %esi,%esi                           value = 0 (release)
//   46e7fc: jmp aws_hal_arch_sunda_write_tpb_nx_local_reg_release_run_stall  // @0x4796a0
//
// aws_hal_arch_sunda_write_tpb_nx_local_reg_release_run_stall @0x4796a0
//   4796cb: lea 0x4(%rbx),%rdi                      base + 0x04
//   ...     call al_reg_write32                     write 0 to NX release_run_stall
//   => NX/SEQ core begins executing.

// aws_hal_stpb_sunda_pooling_release_eng_run_stall @0x46e810
//   46e814: mov $0x2,%esi ; mov $0x1,%edx ; call get_xt_local_reg_offset
//   46e82c: jmp aws_hal_arch_sunda_write_tpb_xt_local_reg_q7_release_run_stall  // @0x4796e0
//
// aws_hal_arch_sunda_write_tpb_xt_local_reg_q7_release_run_stall @0x4796e0
//   4796e0: repz ret                                 <<< NO-OP STUB >>>
```

> **CORRECTION / QUIRK — on Sunda the host does NOT release the Q7 cores.** STEP 6's Sunda Q7
> writer `@0x4796e0` is a bare `repz ret`: the 8 Q7 compute cores are **not** released by this
> register on Sunda — they are driven by the SEQ via EVT_SEM/`run_state`. On **Cayman/Mariana**,
> `release_eng_run_stall` (e.g. Cayman `@0x4735a0`) is a **real** function that computes the Q7
> base and clears its per-core `release_run_stall` (the `0xFF → 0x00` of the device Q7 bundle
> `release_run_stall@0x3000`). **i.e. Cayman/Mariana release the 8 Q7 cores from the host; Sunda
> releases them from the SEQ.** The matching device-side detail (Sunda SEQ `EVT_SEM` vs
> Cayman/Mariana host `0xFF→0x00` stall-clear) is the sibling page
> [HW-Decode CAM-Table Programming](hw-decode-cam-programming.md). `[HIGH/OBSERVED for the
> stub-vs-real split]`

### 7.2 Reset / start / halt — the control-register summary

`[HIGH/OBSERVED for the start path; halt-being-device-driven CARRIED]`

- **RESET state.** NX `release_run_stall` reset = `1` (held stalled); Q7 `release_run_stall`
  reset = `0xFF` (all 8 stalled) — device CSR spec.
- **START.** Write `0` to NX `release_run_stall` (HAL `+0x04`) → NX runs. On Cayman/Mariana,
  write `0x00` to the Q7 per-core `release_run_stall` bits → Q7 cores run. The `start_ctrl.ctrl`
  bit (device NX@`0x04` / Q7@`0x3004`) latches `start_addr` valid and exits Halt.
  `dma_ctrl@+0x40` enable+start gates the SEQ I-fetch DMA.
- **HALT.** The HAL has **no explicit "halt" writer** in the Pool bring-up. Halt is a
  device-driven (firmware) state — the device fault path writes a halt CSR and spins; the host
  observes it via the notification ring + `run_state @+0x14` `bit0`.

---

## 8. SBUF / PSUM / state-buffer relationship

The `aws_hal_q7_*` HAL does **not** program SBUF/PSUM banks directly — those are on-chip SRAMs
the engines address through the datapath, not host CSRs. What this HAL **does** program that
touches the buffer model: `[HIGH structure; bank geometry CARRIED]`

- **(a)** The NX address-translation **windows** (`mem_window2_lo/hi @+0x210/+0x214`, written in
  `dma_init` STEP 4) remap a requester (NX, or a selected Q7) address into a SoC address — they
  pin the Q7's view of SBUF / HBM-scratch / dynamic HBM windows.
- **(b)** The per-core Q7 DRAM ([§3](#3-aws_hal_get_q7_params)): `iram_size`/`dram_size` define
  the per-core apertures the ucode is installed into; the DKL reserved tails (Cayman/Mariana)
  carve the ext-ISA staging region within DRAM.
- **(c)** The notification SW-queue base (`notific_sw_queue_num0 @+0xa00`, STEP 1) + the
  file-IO/stdio TX ring ([§5a](#5a-sunda-real-pokes-per-core-nx-local-registers)) bind the Pool
  engine's completion + host↔device console channels into SBUF/dataram.

The actual SBUF (32 MiB, 16 ECC banks) and PSUM (4 MiB, 32 banks = 4 clusters × 8) banking is
device-side datapath geometry driven by the PE/Pool sequencers, **not** by `aws_hal_q7_*`.

---

## 9. Per-arch difference matrix

`[HIGH/OBSERVED except MED rows flagged]`

| Aspect | Sunda (NC-v2) | Cayman (NC-v3) | Mariana (NC-v4) |
|---|---|---|---|
| Q7 `base_offset` | `0x2980000` | `0x3100000` | `0x3100000` |
| Q7 IRAM / core | `0x10000` (64K) | `0x20000` (128K) | `0x20000` (128K) |
| Q7 DRAM / core | `0x10000` (64K) | `0x40000` (256K) | `0x40000` (256K) |
| Q7 IRAM rsvd tail | `0` | `0x60000` | `0x60000` |
| Q7 DRAM rsvd tail | `0` | `0x40000` | `0x40000` |
| `num_q7` (POOL / CC) | `8` / — (no CC) | `8` / `4` | `8` / `4` |
| owner support | POOLING only | POOLING + CC | POOLING + CC |
| `ucode_eng_init` owner | `r9d==0` only | `r9d` `0→2`, `1→5` | `r9d` `0→2`, `1→5` |
| HAL swap / file_io | **REAL** (local regs) | NO-OP STUB | NO-OP STUB |
| DKL location | **HOST** (HAL regs) | FIRMWARE (ext-ISA) | FIRMWARE (ext-ISA) |
| Q7 `release_run_stall` (host) | **NO-OP** (SEQ-driven) | REAL (host clears) | REAL (host clears) |
| POOL eng local-reg base | `0x2960000` | `0x3060000` | `~0x3060000` *(MED)* |
| xt_local_reg eng-type count | `5` (0..4) | `6` (0..5) | `~6` *(MED)* |
| `pooling_init` hw_decode step | not in-line | in-line (`+0x68`) | in-line *(MED)* |
| Q7 base secure-aperture fold | none | bit35 (`0x8_…`) off | none-confirmed |
| `arch_offsets` source file | `aws_hal_arch_offsets.c` | `…_cayman.c` | `…_mariana.c` |

> **NOTE.** Cayman & Mariana `get_q7_params` + `ucode_eng_init` are **byte-identical** to each
> other; the (NC-v3 vs NC-v4) divergence is in the firmware DKL flavor + the device rev, **not**
> the host HAL. Sunda is the structural outlier (smaller apertures, no CC, host-resident swap,
> SEQ-released Q7).

---

## 10. The end-to-end programming trace (one picture)

`[steps OBSERVED; the full chain is INFERRED from the per-step disasm]`

```text
[register] nrt_set_pool_eng_ucode(iram,dram) -> pool_eng_*_bin globals
[init]     nrt_init -> tpb_eng_init_hals_v2 builds the 5-engine stpb cfg; sets
           cfg[Q7].{iram,dram} = stock q7 ucode bins, then OVERRIDES with the
           registered globals if non-NULL ->
           aws_hal_stpb_init @0x458350 -> notific_init + 3x notif_enable + regs_init
           -> aws_hal_stpb_pooling_init (khal+0x368) -> stpb_<arch>_pooling_init:
             regs_init        (notific SW-queue id + instr-dbg)
             ucode_seq_init   (seq IRAM/DRAM write_padded + engine_base_addr_lo/hi)
             q7_ucode_eng_init(owner=0) -> get_q7_params(owner=2) -> validate sizes
               -> per-core loop: write_padded(DRAM) then write_padded(IRAM)
               -> al_mem_write_buf -> axi_write -> ndl_bar_write (BAR0 PIO)
             [cayman/mariana] hw_decode_table_init
             dma_init         (dma_ctrl=0; rx/tx ring regs + mem_window2; dma_ctrl=1)
             release_seq_run_stall (NX +0x04 <- 0)                 => SEQ runs
             release_eng_run_stall (Q7: sunda=noop / cayman+=clear) => Q7 runs
[run]      Q7 Pool engine executes the (custom) kernel; completion via the notific
           SW-queue + file-IO/stdio ring (Sunda local regs / firmware-side Cayman+);
           faults via the 16-B notification record.
```

---

## 11. Self-verification — the top-5 claims

Each is disassembled at the named address with bounded `objdump` against this `libnrt.so`:

1. **Dispatch shape + `khal+0xb8` slot.** `aws_hal_get_q7_params @0x44bfb0`: `call
   al_hal_tpb_get_arch_type` → `je <assert>` → `lea …#caeb80 <kaena_khal>; mov 0xb8(%rax),%rax`
   → `je <assert>` → `jmp *%rax`. **Confirmed** — `kaena_khal @0xcaeb80`, slot `+0xb8`,
   tail-jump.
2. **Sunda geometry.** `get_q7_params_sunda @0x478f90`: `cmp $0x2,%edi; jne <assert>`, then
   `movq $0x2980000 / $0x10000 / $0x10000 / $0x0 / $0x0` and `movl $0x8`. **Confirmed** — base
   `0x2980000`, IRAM/DRAM 64 K, no rsvd, 8 cores, pooling-only.
3. **Cayman dual-owner geometry + secure fold.** `get_q7_params_cayman @0x47ae70`: `je` for
   `owner==2` (`num_q7=8`), `cmp $0x5` for CC (`num_q7=4`); both base `0x3100000`, IRAM `0x20000`,
   DRAM `0x40000`, rsvd `0x60000`/`0x40000`. The sibling `…hw_decode_table_params_cayman`
   independently shows the `movabs $0xfffffff800000000` bit-35 fold. **Confirmed.**
4. **Sunda swap REAL vs Cayman STUB.** `swap_table_sunda @0x46b7d0` `lea 0x83c/0x840/0x844(%rbx);
   call al_reg_write32` ×3; `swap_table_cayman @0x471470` = `xor %eax,%eax; ret`;
   `…get_register_offsets_cayman @0x471480` zeroes its 3 out-pointers. **Confirmed.**
5. **Run-stall split.** `release_seq_run_stall @0x46e7e0` → writes NX `+0x04 = 0` (via
   `@0x4796a0`, `lea 0x4(%rbx),%rdi`); `release_eng_run_stall @0x46e810` tail-jumps the Sunda Q7
   writer `@0x4796e0` which is `repz ret` (no-op). **Confirmed** — host releases NX, Sunda's Q7
   release is a stub.

Supporting confirmations: `al_mem_write_buf @0x265990` (`test $0x3` align asserts, `shr $0x2`
word-count, `call axi_write`), `ucode_eng_init_sunda @0x46b9a0` (`test %r9d,%r9d; jne <reject>`,
then `call get_q7_params_sunda`, size `cmp`s), `write_padded @0x473eb0` (`al_zalloc → memcpy →
call *%r12 (swap_cb) | al_mem_write_buf → al_free`). All five error strings
(`"IRAM ucode too big! …"`, `"DRAM ucode too big! …"`, `"Invalid q7 owner %u for v2 …"`,
`"Failed to load pooling q7 {dram,iram} core %u"`) read verbatim from `.rodata`.

---

## 12. Confidence, gaps, corrections

**HIGH/OBSERVED:** the `kaena_khal` dispatch slots (`0xb8/0xf0/0x368/0x390/0x398/0x3a8`); the
three `get_q7_params` value tuples; the `ucode_eng_init` owner mapping (`0→2` pooling, `1→5` CC;
Sunda pooling-only), size-validation gates, and per-core DRAM-then-IRAM `write_padded` loop;
`write_padded` (zalloc+pad / memcpy / swap_cb-or-`al_mem_write_buf`); the `al_mem_write_buf`
word-blast → `axi_write` → `ndl_bar_write` BAR0 path; the `al_reg_write32` single-CSR path; the
swap real-vs-stub per-arch split; the per-engine local-reg base tables (Sunda 5-type, Cayman
6-type incl. bit-35 fold); the [§6c](#6c-the-hal-flattened-nxq7-local-register-offsets)
HAL-flattened offsets; the full bring-up call sequence and the Sunda-noop / Cayman-real Q7
release split.

**MED:** the `khal+0x3a0` slot = `swap_file_io_table` (adjacency-inferred); the Mariana
xt_local_reg eng-type table + hw_decode in-line step (mirror-inferred from Cayman); the
[§6d](#6d-the-offset-vs-spec-reconciliation) "separate register spec" *cause* (the functional
shift is OBSERVED, the cause MED); the Sunda `hw_decode_table_init` programming site.

**LOW / not covered:** the exact `write_padded` `swap_cb` body when interposed by nrtucode (the
seam is OBSERVED; the callback target is a runtime pointer); the device-side firmware DKL bind
for Cayman/Mariana; the notification/EVT_SEM completion-ring byte format.

**CORRECTIONS made on this page:**

1. **Swap family is NOT a generic DKL mechanism.** Only Sunda uses the HAL swap registers;
   Cayman/Mariana are NO-OP stubs with firmware-resident DKL ([§5](#5-the-swap-dynamic-kernel-load-dkl-mechanism)).
2. **HAL register offsets differ from the device CSR spec offsets.** Two views of the same
   registers — trust the device spec for semantics, [§6c](#6c-the-hal-flattened-nxq7-local-register-offsets)
   for host write offsets ([§6d](#6d-the-offset-vs-spec-reconciliation)).
3. **Cayman/Mariana `get_q7_params` + `ucode_eng_init` are byte-identical;** the NC-v3/v4 split
   is firmware/rev, not host HAL ([§9](#9-per-arch-difference-matrix)).

---

### Cross-references

- [The nrtucode Subsystem + Device Bring-Up](nrtucode-bringup.md) — the layer **above** this HAL
  (the `swap_cb` interposer, the `platform_rw`/memhandle impls, the ext-ISA DKL libs).
- [HW-Decode CAM-Table Programming](hw-decode-cam-programming.md) — STEP 3b and the device-side
  Q7 run-stall-release detail (Sunda SEQ `EVT_SEM` vs Cayman/Mariana host `0xFF→0x00`).
- [Boot / Reset Sequence + Startup Config](../uarch/boot-reset.md) — the device-side reset vector
  that waits behind the run-stall this HAL clears.
- [`tpb_xt_local_reg` CSR](../control/csr/tpb-xt-local-reg.md) (Part 13) — the authoritative
  device-side register-bundle layout this HAL drives, reconciled in
  [§6d](#6d-the-offset-vs-spec-reconciliation).
