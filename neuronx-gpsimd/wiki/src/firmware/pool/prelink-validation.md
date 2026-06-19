# External-Lib Prelink Validation + `NUM_POOL_CORES`

This page **owns and fully characterizes** the **validation layer** that wraps the device-side
external-library loader on the GPSIMD **POOL** engine. The loader proper —
selector→resident-image resolution, the record walk, the DMA-in of the host-staged `UCPL `
image, the `.kernel_info_table` bind, and `call_start_symbol` — is documented on its own page
([External-Lib Loader (device side)](external-lib-loader.md), *stub*). **This** page is the
definitive reference for everything that *guards* that load: the host-side **UCPL prelinker**
checks (ELF magic / class / segment-flag structure / segment-bounds / full relocation), the
device-side runtime checks (the `NUM_POOL_CORES` core-count invariant, the UCPL content
sanity), the **error handling** on a failed check, and the **HOST-vs-DEVICE split** of where
each check runs.

The central deliverable is the **`NUM_POOL_CORES = 8` `total_cpus` constraint** — the
compile-time invariant the device loader enforces on every library it binds. It is decoded
byte-exact below: the constant is the `bnei a*, 8` immediate baked into the firmware, not a
runtime CSR read, and it ties directly to the 8-core POOL Vision-Q7 cluster
([ADDR-05 / `tpb-pool`](../../control/address/tpb-pool.md), *forward — Part 13*).

The POOL firmware runs on the Vision-Q7 NX `ncore2gp` "Cairo" FLIX/VLIW datapath
(`XCHAL_HAVE_VISION = 1`, `XCHAL_VISION_TYPE = 7`, `IsaMaxInstructionSize = 32`). Every device
fact below was re-derived with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); the
scalar-LX decode rule belongs to the *different* NCFW management core and is wrong here. POOL
images are **flat (non-ELF) relocated blobs** loaded at VA `0x01000000`; carving them as raw
binary means L32R literal-pool loads cannot always be auto-resolved (such claims are flagged
`MED`/`INFERRED`). Tags follow the [Confidence & Walls Model](../../reference/confidence-model.md).

> **NOTE — exact objects re-carved this session, with `sha256`.** Every device fact below was
> re-carved independently out of the static archive
> `extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/.../custom_op/c10/lib/libnrtucode.a`.
> The image bytes live in each member's `.rodata` (file offset `0x60`); I sliced that section
> and verified the digest:
>
> | role | carved member `.rodata` | size (B) | `sha256` |
> |---|---|---|---|
> | POOL DEBUG IRAM (asserts) | `img_SUNDA_Q7_POOL_DEBUG_IRAM_contents.c.o` | 20752 | `d98519b4…f6988` |
> | POOL DEBUG DRAM (strings) | `img_SUNDA_Q7_POOL_DEBUG_DRAM_contents.c.o` | 42496 | `44e70bc5…c9ff2` |
> | POOL RELEASE IRAM (asserts) | `img_SUNDA_Q7_POOL_RELEASE_IRAM_contents.c.o` | 17104 | `9c0a678e…b2013` |
> | POOL RELEASE DRAM (strings) | `img_SUNDA_Q7_POOL_RELEASE_DRAM_contents.c.o` | 42304 | `8a22303a…fb282` |
> | DKL DEBUG IRAM (UCPL log checks) | `img_CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_IRAM_contents.c.o` | 81856 | `7e5e39ac…5231f` |
> | DKL DEBUG DRAM (UCPL log strings) | `img_CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_DRAM_contents.c.o` | 91776 | `6c716c5b…fe066` |
> | CAYMAN NX-POOL DEBUG IRAM (reference family) | `img_CAYMAN_NX_POOL_DEBUG_IRAM_contents.c.o` | 116768 | `8e4412b9…d70a` |
> | CAYMAN NX-POOL DEBUG DRAM (reference family) | `img_CAYMAN_NX_POOL_DEBUG_DRAM_contents.c.o` | 28448 | `7bdf6ed7…6ecd` |
>
> The host (x86-64) UCPL prelinker code lives in the un-stripped twin
> `libnrtucode_internal.so` (same package). It embeds all the per-generation device firmware
> blobs in its own `.rodata`, so the device assert/log strings also appear there — those are
> the compiled-in images, **not** host code.

---

## 1. The two surfaces at a glance

Validation is split across the host/device boundary into **two disjoint surfaces** — nothing
is checked twice:

```
   HOST  (libnrtucode_internal.so, x86-64, at staging/prelink time)
   ┌────────────────────────────────────────────────────────────────────┐
   │ xtlib_verify_magic   : ELF magic, EI_CLASS==32, EI_DATA∈{1,2}        │
   │ validate_dynamic_load: PT_LOAD[0]=R+X, PT_LOAD[1]=R+W, PT_DYNAMIC    │
   │ prelink_load_lib     : segment bounds vs device region map           │
   │ prelink_relocate_lib : apply ALL relocs to completion (none residual)│
   │ prelink (emit)       : write "UCPL " header  (no version, no checksum)│
   └───────────────────────────────┬────────────────────────────────────┘
                                    │  staged UCPL image  (DMA to device)
                                    ▼
   DEVICE (Q7_POOL firmware, in load_external_libraries_impl, at bind time)
   ┌────────────────────────────────────────────────────────────────────┐
   │ (b1) total_cpus ∈ {1, 8} / {0,1,8}  ← the NUM_POOL_CORES invariant   │  ← assert build (SUNDA)
   │ (b2) UCPL start_sym != 0 ;  library_size <= cap                      │  ← log build  (DKL)
   └────────────────────────────────────────────────────────────────────┘
```

**The device never re-checks ELF structure, segments, or relocations.** Because the host
applies *every* relocation and aborts before staging if any fails, the device only ever
receives a well-formed, fully position-resolved `UCPL ` image — so the device's job reduces to
the core-count invariant plus the UCPL content sanity. The evidence for that disjointness is
the *absence* of magic/PT_LOAD/reloc-walk strings or compares in the device loader window and
their *presence* in the host (§3, §6).

---

## 2. The `NUM_POOL_CORES` constraint — byte-exact (the central deliverable)

The constraint is the `entry.total_cpus` check inside the device-resident loader
(source `external_lib_loader.hpp`, recovered verbatim from the firmware DRAM string pool). It
is realized as **two assert sites** whose stringified conditions survive verbatim in the SUNDA
`Q7_POOL` DRAM image, and whose compare instructions are byte-pinned in the IRAM via the
`ncore2gp` disassembler.

### 2.1 The constant: `NUM_POOL_CORES = 8`  `[HIGH/OBSERVED]`

The `8` is **not** a symbol read — it is the encoded `b4const` immediate of the Xtensa `BNEI`
instruction, baked into the firmware at compile time:

| site | address (DEBUG) | opcode bytes | mnemonic |
|---|---|---|---|
| line-283 assert | `0x100142d` | `66 82 05` | `bnei a2, 8, …` |
| line-318 assert | `0x1001501` | `66 83 05` | `bnei a3, 8, …` |
| broadcast dispatch | `0x1001553` | `66 83 12` | `bnei a3, 8, …` |

There is **no CSR read** anywhere on this path. The `8` matches the POOL cluster's eight
Vision-Q7 cores exactly (`Q7_CORE0..7`; CSR `run_state_0..7`) — see §7.

> A census of `bnei a*, 8` in the SUNDA DEBUG IRAM
> (`xtensa-elf-objdump -D | rg -c 'bnei\s+a[0-9]+, 8,'`) returns **3** sites: the two asserts
> (`0x100142d`, `0x1001501`) and the dispatch branch (`0x1001553`). The constant is reused as
> *both* the integrity guard and the broadcast discriminator (§2.4).

### 2.2 Line-283 assert — `total_cpus ∈ {1, 8}`  `[HIGH/OBSERVED]`

Loaded at VA `0x01000000`, disassembled with `XTENSA_CORE=ncore2gp`:

```asm
0x100141d  l32i.n  a2, a1, 12        ; a2 = entry ptr   (stack slot +12)
0x100141f  l8ui    a2, a2, 4         ; a2 = entry.total_cpus   (1-byte field @entry+4)
0x1001422  beqi    a2, 1, 0x1001433  ; total_cpus == 1            -> OK
0x100142a  l8ui    a2, a2, 4         ; reload total_cpus
0x100142d  bnei    a2, 8, 0x1001436  ; total_cpus != NUM_POOL_CORES(8) -> ASSERT FAIL
0x1001433  j       0x1001442         ; OK path
0x1001436  const16 a10, 8 ; const16 a10, 0x469   ; a10 = DRAM 0x469 = assert-string ptr
0x100143f  call8   0x10039e0         ; __assert_fail handler  (§5.1)
```

The assert string at **DRAM `0x469`** reads verbatim
(`strings -a -t x sunda_q7_pool_debug_dram.bin`):

```
/opt/workspace/NeuronUcode/src/external_lib/external_lib_loader.hpp:283
  entry.total_cpus == 1 || entry.total_cpus == NUM_POOL_CORES
```

So line 283 enforces `total_cpus ∈ {1, 8}`. There is **no `== 0`** case here — a freshly loaded
entry must occupy at least one core.

### 2.3 Line-318 assert — `total_cpus ∈ {0, 1, 8}`  `[HIGH/OBSERVED]`

This site reads the entry pointer from a *different* stack slot (`+20`, not `+12`) and adds the
`== 0` case:

```asm
0x10014e7  l32i.n  a3, a1, 20        ; a3 = entry ptr   (stack slot +20)
0x10014e9  l8ui    a3, a3, 4         ; a3 = entry.total_cpus  (@entry+4)
0x10014ec  beqz.n  a3, 0x1001507     ; total_cpus == 0            -> OK   (the post-load state)
0x10014f6  beqi    a3, 1, 0x1001507  ; total_cpus == 1            -> OK
0x1001501  bnei    a3, 8, 0x100150a  ; total_cpus != 8            -> ASSERT FAIL
0x100150d  const16 a10, 0x4ed        ; a10 = DRAM 0x4ed = assert-string ptr
0x1001512  call8   0x10039e0         ; __assert_fail handler  (§5.1)
```

The assert string at **DRAM `0x4ed`**:

```
/opt/workspace/NeuronUcode/src/external_lib/external_lib_loader.hpp:318
  entry.total_cpus == 0 || entry.total_cpus == 1 || entry.total_cpus == NUM_POOL_CORES
```

> **NOTE — the `== 0` case is the post-load / freed state.** Line 283 (the *load* check) forbids
> `0`; line 318 (a *post-load / teardown* check) allows it. After a library is unloaded the
> entry's `total_cpus` slot drops to `0`, so the `{0,1,8}` predicate accepts a freed slot where
> the load-time predicate `{1,8}` would not. The two predicates are the same invariant evaluated
> at two lifecycle points.

### 2.4 What the constraint means, and how it drives dispatch  `[HIGH/OBSERVED]`

`total_cpus` is a **1-byte** entry field (`l8ui …, 4` at every check site) recording how many
POOL Q7 cores the library image occupies. The legal values:

| value | meaning |
|---|---|
| `0` | empty / freed slot (only legal at the line-318 post-state) |
| `1` | single-core library — occupies exactly one Q7 core |
| `8` | `NUM_POOL_CORES` — all-POOL broadcast (the same image on every Q7 core) |

Anything else (`2..7`, or `>8`) is a structural-integrity violation. The guard is *"the loaded
library's per-core image count matches the available POOL cores"* — a library is either
per-core (`1`) or fully broadcast across all `NUM_POOL_CORES` (`8`).

Immediately after the line-283 check, the loader **classifies** the entry into a
broadcast-vs-single boolean — the `8` is reused as the discriminator, not re-asserted:

```asm
0x1001448  addi    a2, a2, -8        ; a2 = total_cpus - 8
0x100144b  movi.n  a3, 1
0x100144d  saltu   a2, a2, a3        ; a2 = (total_cpus-8 < 1)  ==  (total_cpus == 8)
0x1001450  s8i     a2, a1, 28        ; store "is_broadcast" flag to stack+28
```

In the **RELEASE** build the same computation is the *return value* of a helper:

```asm
0x1000f97  { movi a3,1 ; nop ; addi.a a2,a2,-8 }   ; FLIX bundle
0x1000f9f  saltu   a2, a2, a3        ; a2 = (total_cpus == 8)
0x1000fa2  retw.n                    ; return the broadcast boolean
```

The dispatch branch at `0x1001553` then routes on that boolean:

```asm
0x1001550  l8ui    a3, a3, 4         ; total_cpus
0x1001553  bnei    a3, 8, 0x1001569  ; != 8 (i.e. single-core path)
0x100155b  l8ui    a3, a3, 3         ; entry.per_core_index  (@entry+3)
0x100155e  l32i.n  a4, a1, 40        ; a4 = this core's index (stack+40)
0x1001560  beq     a3, a4, 0x1001569 ; proceed only if entry's core index == this core
```

So `total_cpus == 8` takes the broadcast path (loop over all 8 cores); `total_cpus == 1` takes
the single-core path with a per-core-index match (`entry+3` vs this core's index). This
broadcast-vs-single split is the contract the [POOL dispatch loop](pool-dispatch.md) consumes;
the multicore/SPMD semantics are detailed on [ABI multicore-spmd](../../abi/multicore-spmd.md)
(*forward — Part 7*).

> **QUIRK — `total_cpus` is the load-balance descriptor, not just a sanity field.** The same
> byte that the assert validates is also the runtime broadcast switch. A reimplementation must
> populate `entry.total_cpus` with exactly `1` (per-core) or `8` (broadcast) — any in-between
> value both trips the assert *and* would mis-route the dispatch (`saltu` would compute a
> nonsensical boolean). The field is doing double duty.

---

## 3. The host-side checks (UCPL prelinker, x86-64)

These run inside `prelink` in `libnrtucode_internal.so` **before** the `UCPL ` header is
emitted and before any record is sent to the device. A host failure means the prelinker returns
nonzero, the library is never staged, and the device is never touched. Every byte below was
re-disassembled from the un-stripped twin (`objdump -d`).

### 3.1 ELF magic / class / endianness — `xtlib_verify_magic @0x9b6d40`  `[HIGH/OBSERVED]`

```asm
0x9b6d40  mov    $0xffffffff, %eax   ; preload rc = -1
0x9b6d45  cmpb   $0x7f, (%rdi)       ; e_ident[0] == 0x7F
0x9b6d4a  cmpb   $0x45, 1(%rdi)      ; 'E'
0x9b6d50  cmpb   $0x4c, 2(%rdi)      ; 'L'
0x9b6d56  cmpb   $0x46, 3(%rdi)      ; 'F'      -> any mismatch: ret eax=-1
0x9b6d5c  cmpb   $0x1,  4(%rdi)      ; EI_CLASS == 1 (ELFCLASS32 required)
0x9b6d62  movzbl 5(%rdi), %ecx       ; EI_DATA
0x9b6d6b  cmp    $0x1, %ecx ; je …   ; EI_DATA == 1 (LSB) -> swap flag = 0
0x9b6d70  cmp    $0x2, %ecx ; jne …  ; EI_DATA == 2 (MSB) -> swap flag = 1 ; else ret -1
0x9b6d7a  mov    %edx, 0x47ec(%rip)  ; xtlib_globals+0x4 = byte-swap flag (VMA 0x9bb56c)
0x9b6d80  xor    %eax, %eax          ; rc = 0 on success
```

Magic `\x7fELF`, `EI_CLASS == 1` (32-bit), `EI_DATA ∈ {1,2}`. Returns **`-1` on any mismatch,
`0` on success**.

> **NOTE — `EI_DATA` is not just validated, it configures the prelinker.** A valid `EI_DATA`
> sets a process-global byte-swap flag at `xtlib_globals+0x4` (`1`→native LE/no swap, `2`→BE/
> bswap) that every subsequent field read (`xtlib_host_word`, `xtlib_host_half`) consults. The
> check is also the endianness handshake.

### 3.2 Split-load segment structure — `validate_dynamic_load @0x9b71f0`  `[HIGH/OBSERVED]`

```asm
0x9b720e  call   xtlib_verify_magic
0x9b7215  test   %eax,%eax ; setne %cl ; mov %ecx,(%rbx) ; jne 0x9b7307   ; out[0]=1 on fail, bail
0x9b724c  and    $0x7, %eax ; cmp $0x5, %eax ; jne 0x9b7301  ; PT_LOAD[0] (p_flags&7)==5 = PF_R|PF_X (text)
0x9b7277  not    %eax ; test $0x6, %al ; jne 0x9b7301          ; PT_LOAD[1] requires PF_W|PF_R (data)
0x9b72a9  and    $0x6, %eax ; cmp $0x2, %eax …                 ; optional 3rd PT_LOAD p_flags
0x9b72d7  cmp    $0x2, … ; and $0x7, %eax ; cmp $0x6, %eax     ; a PT_DYNAMIC (PF_W|PF_R) must follow
; FAIL targets:
0x9b72ed  movl   $0x7, (%rbx)        ; structural split-load failure  (inline 3rd-seg fallthrough)
0x9b7301  movl   $0x7, (%rbx)        ; structural split-load failure  (shared early-fail target)
```

The library must be a **two-segment split-load** object: a read+execute text `PT_LOAD`, a
read+write data `PT_LOAD`, an optional third segment, and a `PT_DYNAMIC`. A magic/class failure
stores error **`1`**; a structural-shape failure stores error **`7`**.

> **CORRECTION — the data-segment check is `not; test $0x6`, not `test $6; cmp $6`.** A backing
> analysis recorded the second-`PT_LOAD` predicate as `test $0x6` / `cmp $6`. Re-disassembly
> shows the actual encoding is `not %eax ; test $0x6, %al ; jne` at `0x9b7277` — it requires
> *both* bit 1 and bit 2 set (PF_W\|PF_R) by testing the complement against `0x6`. The intent
> (data segment must be R+W) is unchanged; the instruction form is corrected here.

### 3.3 Segment bounds vs the device region map — `prelink_load_lib @0x9b5e70`  `[HIGH/OBSERVED]`

The bounds check is inline (object `prelink_memory_bounds.c.o`). Each copied segment is
range-checked against a device region map: code region at struct offsets `[0x10]`(base)/`[0x18]`
(limit), data region at `[0x30]`/`[0x38]`:

```asm
0x9b5f9f  sub  0x10(%rcx), %r12d      ; code: seg_addr - code_region.base
0x9b5fa5  lea  (%r12,%rdx,1), %rax    ;       + seg_size
0x9b5fa9  cmp  0x18(%rcx), %rax       ;       vs code_region.limit
0x9b5fad  ja   0x9b6033               ;       overflow -> error
0x9b6022  sub  0x30(%rcx), %r15d      ; data: - data_region.base
0x9b602d  cmp  0x38(%rcx), %rax       ;       vs data_region.limit
0x9b6031  jbe  0x9b604e               ;       ok
; error block:
0x9b6033  lea  … # 0x49bd  "Segment exceeds the size of the split region on device"
0x9b603f  call 0x9b60a0  <log_error>
0x9b6044  mov  $0xd, %ebp             ; rc = 13
```

The error string is at host `.rodata` offset **`0x49bd`** and the failure return code is
**`13`** (`mov $0xd, %ebp` @ `0x9b6044`).

### 3.4 Relocation completeness — `prelink_relocate_lib @0x9b6160`  `[HIGH/OBSERVED]`

The relocator iterates **all** reloc entries (count = signed 32-bit at descriptor `+0x28`),
applying each into the staged buffer in place:

```asm
0x9b6160  cmpl   $0x0, 0x28(%rsi) ; jle <success>   ; count <= 0 -> nothing to do
0x9b61b4  movslq 0x28(%r14), %rax                    ; loop bound = count
0x9b61c5  mov    -0x4(%rbp), %edx                    ; reloc type
0x9b61c8  cmp    $0xff, %edx
0x9b61ce  ja     0x9b6625                            ; type > 0xFF -> error
0x9b61a4  call   0x9b6660 <relocate_op>
0x9b61ab  test   %eax,%eax ; jne 0x9b662e            ; any failing reloc -> abort whole prelink
; error returns:
0x9b6625  mov    $0x5, %eax                          ; rc = 5  (reloc type out of range)
0x9b664e  mov    $0x8, %eax                          ; rc = 8  (relocate_op failed)
```

Because every `R_XTENSA` relocation is applied here on the host and **any** failure aborts the
whole prelink (returns rc `5` or `8`, no `UCPL ` emitted), the staged image is **fully
position-resolved**. There is **no residual relocation table** in the `UCPL ` image, and
therefore **no relocation re-check on the device** — the "completeness check" *is* the host
relocator running to completion.

> **CORRECTION — the reloc failure codes are `5` and `8`, not a generic "propagated rc".** A
> backing analysis described the reloc abort as returning an unspecified propagated code.
> Re-disassembly pins them: **`5`** for a reloc whose type exceeds `0xFF` (`0x9b6625`), **`8`**
> for a `relocate_op` that fails on a valid type (`0x9b664e`). Both are bare immediates with no
> associated diagnostic string.

### 3.5 The `UCPL ` header emit — and what it does *not* contain  `[HIGH/OBSERVED]`

On success, `prelink` writes the header magic and a handful of size/offset fields:

```asm
0x9b5e1e  movabs $0x204c504355, %rcx  ; bytes 55 43 50 4c 20 = "UCPL " (+3 NUL pad)
0x9b5e28  mov    %rcx, (%rax)         ; header[0..7] = magic
0x9b5e2b  …                  %ecx, 0x8(%rax)   ; header+0x8  = aligned code size
0x9b5e38  …                  %ecx, 0xc(%rax)   ; header+0xc  = 32-byte-aligned size
0x9b5e41  …                  %ecx, 0x10(%rax)  ; header+0x10 = aligned data size
0x9b5e4e  …                  %rcx, 0x14(%rax)  ; header+0x14 = qword (entry/base)
0x9b5e57  …                  %ecx, 0x1c(%rax)  ; header+0x1c = dword (start_sym)
```

`strings -a -t x libnrtucode_internal.so | rg UCPL` returns **exactly one** occurrence, at file
offset `0x9b4e20` — and that is a **code immediate** inside `.text` (the `movabs` above), not a
data string.

> **GOTCHA — the `UCPL ` header has NO version and NO checksum.** The only identifying constant
> the prelinker writes is the 8-byte `"UCPL "` magic; the rest of the header is size/alignment/
> offset metadata. Nothing versions or hashes the image. Consequently the **device does not
> re-validate the magic, version, or any checksum** — there is none to check. The one header
> field the device *does* read is `start_sym` (header `+0x1c`), checked for NULL in §4.2. A
> reimplementation that expects a version byte or a CRC in the `UCPL ` header will not find one.

---

## 4. The device-side runtime checks (Q7_POOL firmware)

These run in `load_external_libraries_impl` **after** the UCPL image is DMA'd in. They split
across the two firmware build flavors (§6): the **core-count invariant** is the assert-build
(SUNDA) face; the **UCPL content sanity** is the log-build (DKL) face.

### 4.1 `(b1)` the `NUM_POOL_CORES` / `total_cpus` invariant — §2  `[HIGH/OBSERVED]`

Fully decoded above (§2). It is the central FW-17 device check: `total_cpus ∈ {1,8}` at load
(line 283) / `{0,1,8}` post (line 318), enforced by `bnei a*, 8`, routed to `__assert_fail` on
violation (§5.1).

### 4.2 `(b2)` the UCPL content sanity (DKL log build)  `[strings HIGH/OBSERVED; guard-branch MED]`

The DKL firmware logs two content-validation errors through the `'P%i:'` per-core logger
(`call8 0x100e010`). The string references are byte-pinned; the precise compare polarity is
`MED` (§8 FLIX desync):

```asm
; NULL start-symbol check:
0x100775a  const16 a10, 0x18a2        ; DRAM 0x18a2 = "P%i: Corrupted prelink library; NULL start symbol"
0x100775d  call8   0x100e010          ; -> 'P%i:' per-core logger
; library_size cap check:
0x1007567  const16 a10, 0x17f6        ; DRAM 0x17f6 = "P%i: Invalid library_size: %u (max: %u)"
0x100756a  call8   0x100e010
; backward-compat fallback (not a failure):
0x100757e  const16 a10, 0x179f        ; DRAM 0x179f = "P%i: library_size not set, defaulting to data_scratch_size …"
; success fall-through:
0x1007702  const16 a10, 0x18d5        ; DRAM 0x18d5 = "P%i: Library loaded"
0x1007705  call8   0x100e010 ; retw.n
```

The validation strings, verbatim from the DKL DEBUG DRAM:

| DRAM off | string | role |
|---|---|---|
| `0x18a2` | `P%i: Corrupted prelink library; NULL start symbol` | `start_sym == 0` → fail (UCPL hdr +0x1c) |
| `0x17f6` | `P%i: Invalid library_size: %u (max: %u)` | `library_size > max` → fail |
| `0x179f` | `P%i: library_size not set, defaulting to data_scratch_size …` | back-compat fallback (not fatal) |
| `0x1876` | `P%i: Failed to allocate memory for library` | companion alloc failure |
| `0x1774` | `P%i: iDMA channel 0 failed to initialize!` | companion DMA failure |
| `0x18d5` | `P%i: Library loaded` | all-checks-pass success |
| `0x18f8` | `P%i: Library unloaded` | teardown success |

> The `start_sym` checked here is exactly the `UCPL ` header `+0x1c` dword the host writes
> (§3.5). The `library_size` cap is the device-side counterpart of the host segment-bounds
> check (§3.3): the host bounds the segments against the device region map; the device caps the
> declared `library_size` against its own scratch budget.

> **NOTE — the `'P%i:'` prefix is the POOL per-core log stream.** `%i` is the core's `PRID`
> (Xtensa processor ID), loaded via `rsr.prid a11` immediately before each `const16 a10, <fmt>`
> — that is why these log lines carry a per-core number. A census of `'P%i:'`-prefixed strings
> in the DKL DEBUG DRAM (`strings -a | rg -c 'P%i:'`) returns **136**; the validation subset is
> the seven rows above. There are **57** `call8 0x100e010` sites across the DKL IRAM (the full
> `'P%i:'` log surface), of which the two failure-log sites above are the validation guards.

---

## 5. Error handling on a failed check

Three distinct failure responses, by surface:

### 5.1 Device core-count assert → `__assert_fail` (abort)  `[HIGH/OBSERVED]`

Both `total_cpus` assert sites build the assert-string pointer (`const16 a10, <DRAM off>`) and
`call8 0x10039e0`. The DEBUG handler at `0x10039e0` is a classic `__assert_fail`: it formats
`{file, line, function, condition}` using DRAM format strings (`"In function "` @`0x964`,
`" -- "` @`0x971`, `" -- assertion failed"` @`0x976`) via repeated `call8 0x1004328` (the print
primitive), then terminates:

```asm
0x1003a36  movi.n a10, 6 ; call8 0x10038b0   ; exception type 6
0x1003a3d  movi.n a10, 1 ; call8 0x1003c68
0x1003a49  ill                               ; illegal-instruction trap (noreturn)
```

A bad core-count is therefore a **FATAL assert (abort)**, not a recoverable error code. Six
assert sites in the SUNDA DEBUG loader route to this handler
(`rg -c 'call8\s+0x10039e0'` = **6**); the two `total_cpus` sites are among them. The RELEASE
build uses a separate, leaner handler at `0x1002c00` (same `entry a1,32` shape, different print
primitive `0x10034cc`).

> **NOTE — this assert is a guard on the loader's *own* entry table, expected never to fire.**
> Because the host always stages a library as per-core (`total_cpus=1`) or broadcast (`=8`), a
> well-formed image never violates `{1,8}`. The assert catches loader-internal corruption, not a
> hostile input; it is a structural-integrity invariant, not an input validator.

### 5.2 Device UCPL/size validation → `'P%i:'` log + abort-load  `[HIGH/OBSERVED strings; MED rc]`

On NULL `start_sym` or oversized `library_size`, the DKL loader **logs** through
`call8 0x100e010` (the `'P%i:'` per-core logger) and aborts the load of that library. The
success log `"P%i: Library loaded"` (`0x18d5`) is reached only on the all-checks-pass
fall-through. The per-error device return code is **not** byte-pinned from the flat carve (`MED`
— the guard region is FLIX-desynced, §8).

### 5.3 Host prelink → xtlib error code + abort-before-stage  `[HIGH/OBSERVED]`

The host returns a bare integer error code and aborts: nothing is staged, no record is emitted,
the device is never touched. The provable codes:

| code | meaning | site |
|---|---|---|
| `0` | success | `xor eax` / no store |
| `1` | ELF/magic failure | `validate_dynamic_load`, `setne` @ `0x9b721a` |
| `5` | reloc type > `0xFF` | `prelink_relocate_lib` @ `0x9b6625` |
| `7` | split-load structural failure | `validate_dynamic_load` @ `0x9b72ed`, `0x9b7301` |
| `8` | `relocate_op` failed | `prelink_relocate_lib` @ `0x9b664e` |
| `13` | segment exceeds device region | `prelink_load_lib` @ `0x9b6044` |

> **CORRECTION — there is NO symbolic error enum or code→string table.** A backing analysis
> labelled these codes `NOT_ELF`/`NOT_SPLITLOAD`/`INTERNAL_ERR`. Those names are **documentation
> labels only**: `strings -a libnrtucode_internal.so | rg -i 'splitload|not.*elf|internal'`
> finds **no** such tokens. The only split-region string is the rc-13 message at `.rodata`
> `0x49bd`; the integers are returned as bare immediates with no name mapping in the binary. Use
> the numbers, not the invented names.

### 5.4 Relation to the SEQ error model  `[LOW/NOTED — negative result]`

The device validation failures here are POOL-local: the assert handler (`0x10039e0`) and the
`'P%i:'` logger (`0x100e010`) are both in-image. No cross-engine call into the SEQ-core error
handler ([SEQ error model](../seq/dispatch-hub.md)) was observed in the decoded loader window.
This is a **negative result** (absence of a SEQ-error xref), not a positive proof — flagged
`LOW`. A reimplementation should route POOL loader validation failures to a POOL-local
abort/log path, not to the SEQ error sink.

---

## 6. Why the two checks live in different images (build flavors)  `[token split HIGH/OBSERVED; cause MED/INFERRED]`

A full DRAM-token diff (SUNDA base assert-build vs CAYMAN/MARIANA DKL log-build) shows the
core-count assert and the UCPL content logs occupy **different firmware images**:

| validation token | SUNDA assert-build | DKL log-build |
|---|---|---|
| `external_lib_loader.hpp:283` / `:318` | **yes** | no |
| `total_cpus` / `NUM_POOL_CORES` | **yes** | no |
| `"Corrupted prelink … NULL start symbol"` | no | **yes** |
| `"Invalid library_size"` | no | **yes** |
| `"library_size not set …"` | no | **yes** |
| `"Library (un)loaded"` | no | **yes** |
| `.kernel_info_table` / `call_start` / shared loader vocabulary | yes | yes |

Both images are the **same** `external_lib_loader` source; they differ only in build config:

- **SUNDA assert-build** — compiled with `assert()` enabled (carries the `__FILE__`/`__LINE__`
  condition strings) but without the verbose runtime log narrative. It exposes the `total_cpus`
  *structural assert* (§2) and routes failure to `__assert_fail` (§5.1).
- **CAYMAN/MARIANA DKL log-build** — compiled `NDEBUG` (the `total_cpus` assert is **elided** —
  no `{beqi a,1; bnei a,8}` pattern survives, verified by adjacency scan) but with the verbose
  `'P%i:'` runtime log path. It exposes the UCPL `start_sym`/`size` *content* validation (§4.2)
  and routes failure to the `'P%i:'` logger.

The two are **complementary windows** onto one validation layer: neither image alone shows
both, but together they pin the full surface. That the `{1,8}` contract *still holds* in the DKL
build without the device assert is `MED/INFERRED` — the host always stages `total_cpus ∈ {1,8}`,
so the DKL build trusts it without the redundant device check.

---

## 7. Per-generation `NUM_POOL_CORES`  `[HIGH/OBSERVED]`

`NUM_POOL_CORES = 8` on **every** generation, cross-checked three independent ways:

1. **The address map** — the TPB POOL Q7 cluster has `Q7_CORE0..CORE7` (8 cores) on all TPB
   instances; the `LOCAL_REG` q7 bundle declares `run_state_0..7` and `intr_info_0..7` (8
   per-core CSR slots). See [tpb-pool / ADDR-05](../../control/address/tpb-pool.md) (*forward —
   Part 13*).
2. **The firmware compare immediate** — `bnei a*, 8` (§2.1), identical DEBUG and RELEASE, baked
   into the instruction (not a CSR read).
3. **The multicore ABI** — the per-core libraries are `name_cpu0.so .. name_cpu7.so` (8 cores),
   `cpu_id < 8` enforced, domain `{0..7}`; see
   [multicore-spmd](../../abi/multicore-spmd.md) (*forward — Part 7*).

The constant does **not** vary across SUNDA / CAYMAN / MARIANA / MARIANA_PLUS — it is `8` for
the TPB POOL Q7 cluster across the board. (The only "4" in the SoC is the PREPROC Q7 cluster,
which is a *different* engine and is **not** an external-lib/customop host.) The
`total_cpus ∈ {1,8}` contract is gen-invariant.

> **Entry-record stride (refines the loader's entry layout).** The resident entry array is
> indexed with a **`0x118`-byte (280 B) stride**: `movi a7, 0x118 ; mull a6, a6, a7` at
> `0x10015e4` (and at `0x10013f3`, `0x10014bd`). So `object_array<entry>` has 280-byte elements,
> and `total_cpus` is the byte at element `+4`. The `mull`-by-`0x118` is byte-pinned
> `[HIGH/OBSERVED]`; naming the stride "entry record size" is `MED/INFERRED`.

---

## 8. The `.kernel_info_table` presence check

The host writes the `.kernel_info_table` section into the staged image; the device loader
**resolves it by name** and binds it as the POOL dispatcher table — the *same* table the
[POOL dispatch loop](pool-dispatch.md) reads, keyed `(opcode<<24)\|(spec<<16)`. The string
`.kernel_info_table` is at SUNDA DEBUG DRAM **`0x8da`** and is referenced by the loader via
`const16 a2, 0x8da @0x100222a`  `[HIGH/OBSERVED — name + ref]`.

FW-17 does **not** add a separate `.kernel_info_table` validation pass: the table's presence is
a consequence of the host emit, and its *binding* (the resolve-by-name + the dispatcher install)
is the loader's job, not a guard. The table's binary layout — record shape, key encoding, row
enumeration — is the subject of the companion page
([kernel_info_table Binary Layout](kernel-info-table.md), *stub*). The `MED` here is on the
exact *bind instruction* (named, not insn-pinned); the string and its `const16` reference are
`HIGH/OBSERVED`.

---

## 9. FLIX / flat-image decode quality  `[HIGH/OBSERVED]`

The Q7_POOL images are flat (non-ELF) relocated blobs at VA `0x01000000`. Two decode regimes
were measured:

- **SUNDA loader region** (`0x1001400..0x1001700`, the `total_cpus` asserts): decodes
  **cleanly**. The `{beqi a,1 ; bnei a,8}` compares, the `const16` assert pointers, the
  `saltu/s8i` classify, the `0x118`-stride index — all byte-pinned with only a few legitimate
  `j`/`const16` markers. **The `NUM_POOL_CORES` constraint has no FLIX ambiguity.**
- **DKL UCPL-validation region** (`0x1007500..0x1007800`): the wide-instruction (FLIX/IVP)
  bundles around the validation log calls lose alignment in the flat carve, leaving `.byte`
  fragments (`af`, `4d`) interleaved with real opcodes. The `const16 → 'P%i:' log` references
  (NULL `start_sym`, `library_size`) are pinned, but the precise **guard-branch polarity** is
  `MED` there. This is the one honestly-flagged limit — to byte-pin the DKL content guards,
  carve a DKL image **as an ELF** (with section headers / the FLIX config) so the bundles
  re-align. No FLIX desync affects the SUNDA core-count constraint.

---

## 10. Cross-references

- [External-Lib Loader (device side)](external-lib-loader.md) — the load driver this layer
  wraps (#679, *stub* — concurrently authored).
- [POOL Engine Main Dispatch Loop](pool-dispatch.md) — consumes the broadcast-vs-single
  `total_cpus` contract and reads the bound `.kernel_info_table` (#677).
- [kernel_info_table Binary Layout](kernel-info-table.md) — the table whose presence the host
  emits and the device binds (#681, *stub* — **NOTE:** not yet authored).
- [tpb-pool / ADDR-05](../../control/address/tpb-pool.md) — the architectural source of the
  8-core POOL cluster = `NUM_POOL_CORES` (*forward — Part 13*, **NOTE:** not yet authored).
- [runtime / prelinker-ucpl](../../runtime/prelinker-ucpl.md) — the host UCPL prelink chain in
  full (*forward — Part 8*, **NOTE:** not yet authored).
- [abi / multicore-spmd](../../abi/multicore-spmd.md) — the per-core SPMD model the
  `{1,8}` contract realizes (*forward — Part 7*, **NOTE:** not yet authored).
- [Confidence & Walls Model](../../reference/confidence-model.md) — the tag system used above.
