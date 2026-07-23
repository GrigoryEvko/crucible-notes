# On-Device Virtual File-I/O Manager

This page reconstructs the GPSIMD compute core's **virtual file-I/O manager** — the
device-side `write(fd, buf, len)` machinery that a loaded custom-op kernel uses to
emit its `stdout`/`stderr` into a host-readable DRAM ring. It owns a **static
two-file set** (`stdout_file`, `stderr_file`), each a fixed-size object backed by a
**256-byte-slot single-producer DRAM ring**; it owns the chunked **240-byte line
buffer** that stages text before a flush; it owns the **flush → bump-head →
publish-head → `memw`-fence** producer protocol the host polls; and it owns the
**`OVERWRITE`-on-full** policy that lets a busy device drop the oldest slot rather
than stall.

This is the **Cadence Vision-Q7 GPSIMD compute core's** own firmware — windowed-ABI
Xtensa code on the `ncore2gp` (Cairo) core, the core that runs custom-op kernels —
**not** the SEQ/NX engine firmware. That distinction matters: the ubiquitous
`'S: <KernelName>'` log strings seen across every kernel-survey page are **not**
emitted by this manager. They come from a *separate* newlib `printf` logger in a
*different* core. §6 establishes that refutation byte-for-byte; this page is the
machinery behind the **kernel's `printf`** (a raw `write(fd,…)` byte pipe), which on
the SUNDA generation is exactly this DRAM-ring manager.

The host consumer that drains the *other* (SEQ/per-core) log ring — `print_logs`,
the `[severity][NUL-string]` record parser — is documented at
[nrtucode Logging + Leak-Tracking Allocator](../../runtime/nrtucode-logging-allocator.md)
(forward-link; that page is planned, not yet authored). §5 reconciles the two
channels so the relationship to `'S:'`/`'P%i:'` is unambiguous.

> **NOTE — the exact objects used.** Every fact
> below is byte-pinned to a shipped artifact carved from
> `libnrtucode.a` (`10,235,636 B`, `435` members). The anchor image for the manager
> itself is `img_SUNDA_Q7_POOL_DEBUG_IRAM_contents.c.o` (code) /
> `img_SUNDA_Q7_POOL_DEBUG_DRAM_contents.c.o` (strings). Carved via
> `objcopy -O binary --only-section=.rodata`, disassembled with the native
> `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, Xtensa Tools 14.09 / RI binutils
> 2.34) that ships inside the gpsimd-tools package. The recovered source-file-name
> string `file_io_manager.hpp` and the enum token
> `SUNDA_UCODE_FILE_IO_QUEUE_FULL_POLICY_OVERWRITE` are themselves binary evidence
> (`.rodata` of the DEBUG/RELEASE images) and are cited as such.
>
> | object | size | sha256 (first 16) |
> |---|---:|---|
> | `…SUNDA_Q7_POOL_DEBUG_IRAM` .rodata | `20752 B` (`0x5110`) | `d98519b4394750fa` |
> | `…SUNDA_Q7_POOL_DEBUG_DRAM` .rodata | `42496 B` (`0xa600`) | `44e70bc520ca26b7` |
> | `…SUNDA_Q7_POOL_RELEASE_IRAM` .rodata | `17104 B` (`0x42d0`) | `9c0a678e8c85cb4e` |
> | `…CAYMAN_NX_POOL_DEBUG_IRAM` .rodata | `116768 B` | `8e4412b99201f62d` |
> | `…CAYMAN_NX_POOL_DEBUG_DRAM` .rodata | `28448 B` | `7bdf6ed7ccd27b37` |
> | `…CAYMAN_Q7_POOL_DEBUG_DRAM` .rodata | `89344 B` | `226f4254d4751903` |

Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Disassembly was forced past `retw.n`/FLIX-bundle
desync points with explicit `--start-address=`; the manager's translation unit
(IRAM `0xd10`..`0x1ffe`) is scalar `ncore2gp` code with clean `entry`/`retw.n`
boundaries — only the vector `memset`/`memcpy` helpers (`0x3340`/`0x3388`) decode as
FLIX bundles, and they decode cleanly.

---

## 1. Location, address model, and per-generation presence

The manager is a Q7-GPSIMD-core component, anchored by the source-path string and
**three** DEBUG asserts in `.rodata`:

| DRAM off | source line | assert expression |
|---:|---|---|
| `0x2ce` | `file_io_manager.hpp:259` | `stdout_file.desc.full_policy == SUNDA_UCODE_FILE_IO_QUEUE_FULL_POLICY_OVERWRITE` |
| `0x362` | `file_io_manager.hpp:264` | `stderr_file.desc.full_policy == SUNDA_UCODE_FILE_IO_QUEUE_FULL_POLICY_OVERWRITE` |
| `0x5f1` | `file_io_manager.hpp:182` | `bytes_to_copy > 0` |

Each is `const16`-referenced from the assert call site in IRAM, which proves the
manager code lives at those IRAM addresses. `[HIGH/OBSERVED — strings carved from
both DEBUG and RELEASE DRAM; assert sites disassembled in §3.]`

**Address model.** IRAM file-offset `==` device IRAM VA (reset vector `j 0x220` at
byte 0). DRAM string VA `== 0x80000 + file-offset`: const16 reconstructions in the
TU build `0x8xxxx` addresses (e.g. the SOC-window global is assembled as
`const16 a3, 8; const16 a3, 0x93c4` → DRAM VA `0x80093c4`, confirmed at IRAM
`0x1e42`). So DRAM offset `0x2ce` is device VA `0x802ce`. `[HIGH/OBSERVED]`

> **NOTE — the manager is NOT debug-only.** The three `file_io_manager.hpp` asserts
> appear in **both** `SUNDA_Q7_POOL_DEBUG` *and* `SUNDA_Q7_POOL_RELEASE` DRAM (3
> hits each: `strings … | rg -c 'file_io_manager'` → `3` / `3`). Only the assert
> *verbosity* differs by build; the ring machinery itself ships in RELEASE too
> (code-signature check in §7).

**Per-generation.** The `file_io_manager` token and its 256-byte-slot ring code
exist **only** on the SUNDA generation's Q7 POOL image; CAYMAN / MARIANA /
MARIANA_PLUS Q7 retired it in favour of a newlib `printf` path with a `'P%i:'`
prefix. The full per-gen byte-check is §7.

---

## 2. The file model — the file struct and the two virtual files

The manager owns an array of fixed-size **file objects**; the asserts name exactly
two — `stdout_file` and `stderr_file`. Each file object is **`0x130` (304) bytes**
(the `memset` stride in `init`, §4). The two are adjacent: `stdout_file` at
`manager+0`, `stderr_file` at `manager+0x130`. The recovered layout (all field
offsets OBSERVED from the write/flush/init field accesses):

```c
/* one virtual-file object — 0x130 (304) bytes; verified field offsets in []  */
struct file_io_file {                  /* SUNDA_Q7 file_io_manager.hpp        */
    uint8_t  enabled;        /* +0x00  write/flush/guard all bail if 0        */
    uint8_t  full_policy;    /* +0x01  asserted == 2 == ..._OVERWRITE         */
    /* +0x02..+0x0B reserved (part of the 40-byte ".desc" config sub-struct)  */
    uint32_t num_slots;      /* +0x0C  ring size in 256-byte slots (remu div) */
    uint64_t ring_dram;      /* +0x10  ring DRAM base (lo @+0x10, hi @+0x14)  */
    uint64_t headptr_dram;   /* +0x18  head-publish DRAM addr (lo@+0x18,hi@+1C)*/
    /* +0x20..+0x27 reserved                                                  */
    uint8_t  stage[256];     /* +0x28  the 256-byte staging slot:             */
                             /*        +0x28 16-byte slot HEADER (the ".desc") */
                             /*        +0x38 char[240] the line/text buffer    */
    /* fill_count ALIASES into the staging region — see GOTCHA below          */
    uint32_t fill_count;     /* +0x34  bytes currently in the 240-byte buffer */
    /* +0x128                                                                 */
    uint32_t head;           /* +0x128 monotonic write-sequence counter       */
};
```

`[Field offsets enabled@+0, full_policy@+1, num_slots@+0x0C, ring_dram@+0x10,
headptr_dram@+0x18, stage@+0x28, line-buffer@+0x38, fill_count@+0x34, head@+0x128:
HIGH/OBSERVED — every one read directly from a `l8ui`/`l32i [file+N]` in §3/§4.]`

> **GOTCHA — `fill_count@+0x34` aliases INSIDE the staging slot.** `+0x34` falls
> within the `+0x28` (offset `0x34-0x28 = 12`) staging block. The firmware reuses a
> word of the slot region as the live fill cursor while staging, then flushes the
> whole `+0x28..+0x127` 256-byte block to DRAM. A naïve reimplementation that puts
> `fill_count` in its own non-overlapping field is functionally fine, but it will
> **not** be byte-compatible with the shipped struct, and you cannot blindly `memcpy`
> the whole staging region without first re-deriving the live fill value. `[+0x34
> OBSERVED HIGH from `l32i a3,a3,52` at `0x1d8f`/`0x1e34`; the internal `.desc`
> 16-byte header layout MED — only `{enabled@0, full_policy@1}` are pinned.]`

There is **no `open()` / `close()` / `seek()`** in this image. The file set is
*static*: the two std streams are created at `init` (the only "open") and torn down
at `exit_flush` (the only "close"). The model is **append-only write + flush**, not
a general `fopen`/`fseek` VFS. No seek/open/close symbol or code path exists; the
strings name only `write` (`bytes_to_copy`), the two files, and `full_policy`.
`[HIGH/OBSERVED]`

---

## 3. The write path — chunked 240-byte line buffer → 256-byte ring slot

The buffered writer `file_io_write_buffered(file*, src*, len)` lives at IRAM
**`0x1d50`**. Disassembled instruction-exact:

```c
/* file_io_write_buffered @ IRAM 0x1d50  — HIGH/OBSERVED                       */
size_t file_io_write_buffered(file_io_file* f, const char* src, size_t len) {
    if (f->enabled == 0)                  /* 0x1d5f l8ui [f+0]; 0x1d62 bnez.n  */
        return len;                       /* disabled stream = silent success  */
    size_t written = 0;
    while (written < len) {               /* 0x1d79 blt written,len            */
        size_t remaining = len - written;                 /* 0x1d86 sub        */
        size_t space     = 240 - f->fill_count;  /* 0x1d91 movi 240; 0x1d94 sub*/
        size_t n = (space >= remaining) ? remaining : space; /* 0x1d97 bge sel */
        ASSERT(n > 0);   /* file_io_manager.hpp:182 "bytes_to_copy > 0" @0x5f1  */
                         /* 0x1dae blti n,1; 0x1db7 const16 a10,0x5f1; call8   */
                         /*        0x39e0 (assert handler)                      */
        memcpy(/*dst*/ (uint8_t*)f + 0x28 + 0x10 + f->fill_count,
               /*src*/ src + written, n); /* 0x1dc4 addi 40; 0x1dc7 addi 16;   */
                                          /* 0x1dca +fill; 0x1dd7 call8 0x3388  */
        written          += n;            /* 0x1dde add                        */
        f->fill_count    += n;            /* 0x1dea s32i [f+52]                 */
        if (f->fill_count == 240)         /* 0x1df0 movi 240; 0x1df3 bne       */
            file_io_flush_one(f);         /* 0x1e02 call8 0x1e18 -> 0x1e1b      */
    }
    return written;
}
```

The device buffers raw bytes into the 240-byte (`0xF0`) line buffer at `f+0x38`;
whenever the buffer fills to exactly 240 it flushes **one** 256-byte ring slot. The
`memcpy` destination is assembled as `f + 0x28` (slot base) `+ 0x10` (past the
16-byte header) `+ fill_count` — i.e. the 240-byte body starts 16 bytes into the
256-byte slot. `[HIGH/OBSERVED]`

> **GOTCHA — a write of `len` bytes can produce many slots, and the last partial
> line is NOT flushed.** The loop flushes only on the exact `fill_count == 240`
> boundary. A `write` that leaves the buffer partially full returns success but
> leaves those bytes staged in SRAM until either the next `write` tops the buffer to
> 240 or an explicit `flush`/`exit_flush` drains it. The host will not see the tail
> line until a flush occurs. `[HIGH/OBSERVED — no flush on the `written < len`
> exit; the only flush triggers are `==240` and the explicit `flush`/`exit_flush`
> entries of §5.]`

### The flush — `file_io_flush_one(file*)` @ IRAM `0x1e1b`

```c
/* file_io_flush_one @ IRAM 0x1e1b  — HIGH/OBSERVED                            */
int file_io_flush_one(file_io_file* f) {
    if (f->enabled == 0)     return 0;    /* 0x1e23 l8ui [f+0]; 0x1e26 bnez.n  */
    if (f->fill_count == 0)  return 0;    /* 0x1e34 l32i [f+52]; 0x1e36 bnez.n */

    /* translate the ring DRAM addr to the SOC-window addr the Q7 stores through */
    void* ring_soc = dram_addr_to_soc_addr(   /* 0x1e50 call8 0xe40            */
            GLOBAL_soc_window /* DRAM 0x80093c4, 0x1e42 const16 8/0x93c4 */,
            f->ring_dram /* lo@+0x10, hi@+0x14, 0x1e4c/0x1e4e */);

    uint32_t slot = f->head % f->num_slots;   /* 0x1e57 l32i [f+0x128];        */
                                              /* 0x1e5a l32i [f+12]; 0x1e5c remu*/
    memcpy(ring_soc + (slot << 8),  (uint8_t*)f + 0x28,  256);
            /* 0x1e6a slli a4,a4,8 (slot*256); 0x1e63 addi a11,a3,40 (src=f+0x28)*/
            /* 0x1e6f movi a12,0x100 (256); 0x1e72 call8 0x3388                  */
    memset((uint8_t*)f + 0x28, 0, 256);   /* 0x1e7c call8 0x1e9c -> 0x3340     */
    f->head += 1;                         /* 0x1e81 l32i; 0x1e84 addi.n;       */
                                          /* 0x1e86 s32i [f+0x128]             */
    publish_head(f);                      /* 0x1e8d call8 0x1eb4               */
    return 0;
}

/* publish_head @ IRAM 0x1eb4  — HIGH/OBSERVED                                 */
static void publish_head(file_io_file* f) {
    void* head_soc = dram_addr_to_soc_addr(   /* 0x1ecd call8 0xe40            */
            GLOBAL_soc_window /* 0x1ebf const16 8/0x93c4 */,
            f->headptr_dram /* lo@+0x18, hi@+0x1C, 0x1ec9/0x1ecb */);
    *(uint32_t*)head_soc = f->head;       /* 0x1ed4 l32i [f+0x128]; 0x1ed9 s32i*/
    __memw();                             /* 0x1edb memw  — *** the fence ***  */
}
```

**One flush = one 256-byte slot** written to `ring_soc[(head % num_slots) * 256]`,
the staging slot cleared, `head` incremented, and the new head index published to a
**separate** DRAM word with a `memw` fence so the host observes the slot *data*
before the head *advances*. This is a textbook single-producer/single-consumer
producer: write payload → fence → bump head. `[HIGH/OBSERVED — instruction-exact;
the `memw` at `0x1edb` is the visible barrier.]`

`dram_addr_to_soc_addr` (IRAM `0xe40`) takes the SOC-window base `GLOBAL[0x80093c4]`
plus a 64-bit DRAM address and returns the SOC-window address the Q7 issues stores
through; both `flush` and `publish_head` route their DRAM writes through it.
`[HIGH/OBSERVED that `0xe40` translates DRAM→SOC via the `0x93c4` global; the exact
window arithmetic in its sub-helpers is MED.]`

---

## 4. Init + the `.desc` / `full_policy` — the descriptor published to DRAM

The manager constructor at IRAM **`0x624`** loads the singleton GLOBAL at DRAM
**`0x809ef8`** (`const16 a2,8; const16 a2,0x9ef8`), takes a spinlock
(`call8 0xce0`/`0xcf0`), and calls `init @0xd10` under the lock. `init` is
**`prid`-gated** (`rsr.prid a3` @`0xd17` — per-core):

```c
/* file_io_manager::init @ IRAM 0xd10  — prid-gated, under the singleton lock  */
void file_io_init(file_io_file* mgr /*=a2*/) {
    uint32_t prid = rsr_prid();               /* 0xd17                         */
    memset(&mgr[0],     0, 0x130);            /* 0xd1c movi 0x130; call8 0x3340 */
    memset(&mgr[0x130], 0, 0x130);            /* 0xd29 add; call8 0x3340        */
    if (prid != 0) return;                    /* 0xd33 beqz.n -> only core 0    */

    uint64_t ring_dram = (DRAM[124] << 32) | DRAM[112]; /* 0xd40 const16 116;  */
                                              /* 0xd48 const16 112 (host-provd) */
    if (ring_dram == 0) return;               /* 0xd5b bnez (unprovisioned)     */
    void* ring_soc = dram_addr_to_soc_addr(GLOBAL_soc_window, ring_dram);
                                              /* 0xd6f const16 0x93c4; 0xd75    */
    /* install a 40-byte descriptor template into the DRAM ring, per file:      */
    memcpy(ring_soc + 0    /* + slot*40 */, /*ctx src=a2*/ ..., 40); /* 0xd8e   */
    memcpy(ring_soc + 0x140/* + slot*40 */, ...,            40); /* 0xda9        */

    ASSERT(stdout_file.enabled ? stdout_file.full_policy == 2 : 1); /* hpp:259  */
                              /* 0xdac l8ui [a2+0]; 0xdb4 l8ui [a2+1]; bnei a3,2 */
    ASSERT(stderr_file.enabled ? stderr_file.full_policy == 2 : 1); /* hpp:264  */
}
```

`[memset stride 0x130 (×2), the prid gate, the DRAM `112`/`124` ring-config words,
the two 40-byte `memcpy`s, and both `full_policy==2` asserts: HIGH/OBSERVED.]`

> **CORRECTION (vs the backing report) — the 40-byte `.desc` template is published
> to the *DRAM ring*, not copied into the in-SRAM file struct.** The backing report
> read the two `memcpy(…, 40)` as "install a 40-byte descriptor into each file."
> Re-disassembling the dst computation shows the destination is
> `ring_soc` (the SOC-translated ring base, `a1+8`) `+ slot_idx*40` for `stdout`,
> and `ring_soc + 0x140 + slot_idx*40` for `stderr` (`0xd80` `addx4 a4,a4,a4`
> `→ 5x`; `0xd85` `slli a4,a4,3` `→ *8`; net `*40`). So `init` writes a **40-byte
> ring-header descriptor into host-visible DRAM** (two of them, `stderr`'s at
> `+0x140 = 320 B` from `stdout`'s), advertising the ring layout to the host — it is
> **not** an SRAM-side struct copy. `[CORRECTION folded in place. HIGH/OBSERVED —
> dst = `ring_soc + slot*40` read directly at `0xd7e`..`0xda9`.]`

### The queue-full policy enum

The `full_policy` byte at `file+1` is asserted `== 2 ==
SUNDA_UCODE_FILE_IO_QUEUE_FULL_POLICY_OVERWRITE` at init for **both** std files.

> **QUIRK — `OVERWRITE` is enforced structurally; the assert merely guards it.** The
> flush ring math (`slot = head % num_slots`, §3) *always* wraps and overwrites the
> oldest slot. There is **no full-check, no back-pressure, no drop-counter** branch
> anywhere in `0x1d50`/`0x1e1b`. A host that falls behind silently loses the oldest
> lines. The init assert exists only to catch a mis-provisioned descriptor whose
> `full_policy` was set to anything other than `OVERWRITE`. The other enum members
> (e.g. `0`/`1` = `BLOCK`/`DROP`) are **inferred** to exist — only `OVERWRITE (2)`
> is referenced by code in this image. `[value `2`==`OVERWRITE`: HIGH/OBSERVED. The
> absence of any blocking branch: HIGH/OBSERVED. Other enum members: LOW/INFERRED.]`

---

## 5. The public API surface + the backing channel

The manager exposes a POSIX-fd-style API keyed by `fd ∈ {1=stdout, 2=stderr}`:

| entry | IRAM | role |
|---|---|---|
| `file_io_write(mgr, fd, src, len)` | `0x1c87` | the `write`/`fputs` entry; fd-dispatch → `0x1d50` |
| `file_is_writable(mgr, fd)` | `0x1d10` | per-fd `enabled` predicate |
| `file_io_flush(fd)` | `0x1c6c` | flush one std stream → `0x1ee0` → `0x1e1b` |
| `exit_flush()` | `0x1fc0` | teardown: flush stderr+stdout, fence, spin |
| `file_io_init` / ctor | `0xd10` / `0x624` | per-core, singleton-locked, static "open" |

**fd dispatch** (`file_io_write @0x1c87`, OBSERVED): guard via `0x1d10`; then
`bnei a3,1` → `file = mgr+0` (stdout); `bnei a3,2` → `file = mgr+0x130` (stderr);
either way `call8 0x1d50`; a SOC-window save/restore brackets the write
(`const16 0x93c4` reads at `0x1ca6`/`0x1cf4`, `call8 0xe90` at `0x1d02`). A disabled
or unknown fd is a silent no-op that returns the full `len` so callers see success.

**`exit_flush @0x1fc0`** (OBSERVED): writes a per-core teardown CSR
`(prid<<24 | 1<<29 | hdr@+24)` to DRAM `124 = 0x7c`, then `flush(2)` then `flush(1)`
(both via `call8 0x1c6c`), a `memw` fence, then an unconditional self-loop
`j 0x1ffe` — the device flushes both std streams to DRAM, fences, and halts.
`[HIGH/OBSERVED.]`

### The backing channel — a host-backed DRAM ring of 256-byte slots

A "file" is a **host-backed DRAM ring buffer**. The device is the *sole producer*;
the host reads device DRAM. Putting §3/§4 together:

- Each file carries a ring descriptor: base (`f+0x10`, 64-bit), slot count
  (`f+0x0C`), and a monotonic write `head` (`f+0x128`).
- The transfer unit is a **256-byte (`0x100`) slot** = a 16-byte header (`f+0x28`) +
  a 240-byte body (`f+0x38`). A flush copies the whole 256-byte staging block into
  `ring_base[(head % slot_count) * 256]` through the SOC window.
- After the slot, the device bumps `head` and **publishes** the new head value to a
  *second* DRAM word (`f+0x18` head-pointer addr) followed by `memw`. Protocol:
  **write slot → bump head → publish head (fenced).**

```
        device DRAM ring (host maps + polls)
        +------------------------------------------------+
slot 0  | [16B hdr | 240B line ]  256 B                  |
slot 1  | [16B hdr | 240B line ]                         |
  ...   | ...                                            |
slot N-1| [16B hdr | 240B line ]   (N = num_slots@+0x0C) |
        +------------------------------------------------+
        head index (@ headptr_dram, a SEPARATE DRAM word) -- written last, memw-fenced
        wrap: slot = head % N     full policy: OVERWRITE (no back-pressure)
```

The host **polls the published head word**, sees it advance, and drains the new
256-byte slots out of the ring. This is neither a memhandle-staged DMA nor an MSI-X
surprise — it is a poll-a-DRAM-head SPSC ring, written through the Q7's SOC window,
read by the host from DRAM. The host code is the NRT custom-op runtime, out of this
device blob's scope. `[device producer half HIGH/OBSERVED; host poll-read half HIGH/INFERRED]`

---

## 6. Relationship to `'S:'` / `'P%i:'` and the `pool_stdio` ring

The pinned cross-cluster framing — that this manager is the engine behind the
ubiquitous `'S: <KernelName>'` DEBUG-DRAM log strings — is, on close inspection,
**only half true**, and the precise reconciliation matters because every kernel page
has tracked those strings. There are **two distinct device→host text channels**:

> **CORRECTION — `file_io_manager` is NOT the `'S:'` emitter. The `'S:'`/`'P%i:'`
> lines come from a separate newlib `printf` logger in a different core.**
> Re-disassembled in `CAYMAN_NX_POOL_DEBUG_IRAM`:

**(A) The `'S:'` / `'P%i:'` channel = a newlib `FILE*` `printf` logger.** The SEQ
emitter at IRAM **`0x18b84`** is a `vfprintf`-style logger: it builds a newlib
`FILE*` at DRAM `0x84d28` (`const16 a3,8; const16 a3,0x4d28` @`0x18b9c`), takes a
lock (`call8 0x19ae4`), formats (`call8 0x18d14`), and releases the lock
(`call8 0x19b3c`). The `'S: …'` strings (e.g. `S: Dispatch opcode=0x%x`,
`S: BEGIN on cayman`, `S: TensorStore`) are the **format-string arguments** to that
`fprintf`; `'S: '` is just a hard-coded line prefix in those formats. The `'P%i:'`
family (`P = POOL core`, `%i = core index`) is the same `printf` family for the
POOL cores. `[HIGH/OBSERVED — `0x18b84` disassembled; FILE@`0x84d28`; the `'S:'`
strings are `printf` formats, not record framing.]`

**(B) `file_io_manager` = the Q7 GPSIMD compute core's external-lib kernel stdio.**
It is the hand-rolled 256-byte-slot DRAM ring of §3–§5 — **no** `printf`, **no**
`'S:'` format strings, **no** severity byte. It is a raw `write(fd, buf, len)` byte
pipe for a *loaded kernel's* `stdout`/`stderr`. `[HIGH/OBSERVED — the SUNDA Q7 image
has **zero** `'S:'` and **zero** `'P%i:'` strings: `strings sunda_q7_dbg_dram.bin |
rg -c '^S: '` → `0`, `… rg -c 'P%i:'` → `0`.]`

The two are **structurally different** channels:

| | `'S:'` channel (SEQ / `'P%i:'` POOL) | `file_io_manager` (Q7 kernel stdio) |
|---|---|---|
| core | SEQ/NX (and CAYMAN+ POOL) | Vision-Q7 GPSIMD compute |
| mechanism | newlib `FILE*` `fprintf` @`0x84d28` / `0x18b84` | hand-rolled 256-B-slot DRAM ring |
| record framing | `[severity:u8][C-string\0]` packed back-to-back | fixed 256-B slot, no severity byte |
| line prefix | `'S: '` / `'P%i: '` baked in the `printf` format | none (raw bytes) |
| host consumer | `print_logs` (`[sev][\0str]` parser) — see below | NRT custom-op runtime (out of blob scope) |
| full policy | host-tail-tracked SPSC drain | device `OVERWRITE` wrap |

**How a kernel log line becomes a DRAM record.** On the `'S:'`/`'P%i:'` path (the
one the host's `print_logs` drains), the device `printf` logger writes
`[severity_byte][NUL-terminated string]` records back-to-back into a device-memory
SPSC ring (the **`pool_stdio`** ring); the device advances a producer `head` in the
per-core control block; the host `print_logs` reads that head, bulk-copies the new
`[head − tail]` bytes, parses each `[sev][\0str]` record, and re-emits it as
`"<friendly_name>: <device-message>"`. The `'S:'`/`'P%i:'` prefix rides **inside**
the message body; the host adds its **own** prefix (the per-core `friendly_name`).
That host-consumer half is documented at
[nrtucode Logging + Leak-Tracking Allocator](../../runtime/nrtucode-logging-allocator.md).
`[record format + `friendly_name` re-emit HIGH/OBSERVED host-side; the "same ring as the
`0x18b84` logger feeds" claim MED — the on-device head-advance is SEQ-firmware scope]`

> **NOTE — count metric for the survey strings.** The DEBUG-DRAM `'S:'` figure is a
> **format-string** count, not a runtime-record count: `CAYMAN_NX_POOL_DEBUG_DRAM`
> holds **187** `'S: '`-prefixed format strings (`grep -ao 'S: ' … | wc -l` → `187`;
> `strings … | rg -c '^S: '` → `187`). `CAYMAN_Q7_POOL_DEBUG_DRAM` holds **156**
> `'P%i:'` format strings (`grep -ao 'P%i:' … | wc -l` → `156`). These are the
> *number of distinct log call sites* (one format string each), not the 178-entry
> SEQ dispatch table — the two counts (`178` dispatch records vs `187` `'S:'`
> formats) are different quantities and should not be conflated. `[HIGH/OBSERVED]`

**Verdict.** `file_io_manager` is the SUNDA-era Q7 custom-op kernel's
`stdout`/`stderr`-to-DRAM channel — the device side of "`printf` from inside a
custom op." It is **not** the cross-generation `'S: <KernelName>'` firmware-log
channel that the kernel-survey pages keep seeing. The pinned framing conflated two
same-purpose but distinct device→host text channels. `[HIGH/OBSERVED — distinguished
by core, mechanism, and string evidence.]`

---

## 7. Per-generation presence + DEBUG-vs-RELEASE (byte-checked)

The manager is a **SUNDA-generation Q7-POOL** artifact. Two independent byte-checks
across the Q7 images:

**(1) String token.** `strings <DRAM>.bin | rg -c 'file_io_manager'`:

| image | `file_io_manager` token | `'S:'` | `'P%i:'` |
|---|---:|---:|---:|
| `SUNDA_Q7_POOL_DEBUG_DRAM` | `3` | `0` | `0` |
| `SUNDA_Q7_POOL_RELEASE_DRAM` | `3` | `0` | `0` |
| `CAYMAN_Q7_POOL_DEBUG_DRAM` | `0` | `0` | `156` |

**(2) Code signature** (the file_io ring: `movi …,240` line buffer + `remu`
head%slots + `movi a12,0x100` 256-byte slot copy), counted over the native disasm:

| image (IRAM) | `movi …,240` | `slot256` (`movi a12,0x100`) | ring present? |
|---|---:|---:|---|
| `SUNDA_Q7_POOL_DEBUG` | yes | `2` | **PRESENT** |
| `SUNDA_Q7_POOL_RELEASE` | yes | `2` | **PRESENT** |
| `CAYMAN_Q7_POOL_DEBUG` | `0` | `0` | **ABSENT** |
| `MARIANA_Q7_POOL_DEBUG` | `0` | `0` | **ABSENT** |

Both checks agree: the 256-byte-slot file_io ring ships on SUNDA Q7-POOL in **both**
DEBUG and RELEASE, and is **retired** on CAYMAN / MARIANA / MARIANA_PLUS Q7 (which
have zero `file_io_manager` tokens and zero `slot256` copies). The `slot256` count is the
decisive code signal, because the `memw`/`remu` opcodes also occur in unrelated DMA code.
`[HIGH/OBSERVED]`

**What replaced it.** CAYMAN+ Q7 emits kernel `stdout`/`stderr` via the newlib
`printf` path with a `'P%i:'` per-POOL-core prefix (e.g.
`P%i: Running max_pool with period = %0d`, `P%i: Assertion failure! %s(%s:%u)`) —
the *same* stdio family as the SEQ `'S:'` engine. So:

- **SUNDA Q7-POOL** — kernel stdio via `file_io_manager` (256-B-slot DRAM ring).
- **CAYMAN+ Q7** — kernel stdio via newlib `printf` → `'P%i:'` lines (unified with
  the SEQ `'S:'` stdio); `file_io_manager` retired.

`file_io_manager` is therefore the **SUNDA-era predecessor** of the unified
`printf`/`'S:'`/`'P%i:'` stdio logging the later generations converged on. It is the
GPSIMD core's own kernel-stdout mechanism, not the cross-gen `'S:'` firmware-log
channel.

---

## 8. Error / full handling

- **Queue full** = `OVERWRITE` (`full_policy == 2`, asserted at init for both std
  files). The ring head wraps via `head % num_slots`; the oldest slot is
  overwritten. There is **no** blocking/back-pressure and **no** drop-counter in the
  write/flush path — a host that falls behind silently loses the oldest lines.
  `[HIGH/OBSERVED.]`
- **Disabled file** — every entry (`write 0x1d50`, `flush 0x1e1b`, guard `0x1d10`)
  bails if `file->enabled (file+0) == 0`. A disabled stream is a silent no-op that
  returns the full `len` (write), so callers see success.
- **`bytes_to_copy > 0`** (`hpp:182`) — a DEBUG sanity check that each per-chunk copy
  is non-empty (`blti n,1 → const16 a10,0x5f1 → call8 0x39e0`).
- **The Q7 fault path is SEPARATE.** The Q7 core's exception handler
  (`GPSIMD EXCEPTION OCCURRED:` @DRAM `0x806c0`; `exception.hpp:172` @`0x8067b`;
  `ILLEGAL INSTRUCTION` / `STACK OVERFLOW` / …) is the Q7 analogue of the
  [SEQ Error-Handler](../seq/error-handler.md), **not** part of `file_io_manager`.
  Note `exit_flush` references that exception string region via `call8 0x2004`
  (`const16 a11,0x6c0` → `0x806c0`) on its teardown path, but the fault detection
  itself is a distinct TU. `[HIGH/OBSERVED — distinct strings/source file.]`

---

## 9. Cross-references

- [POOL Engine Main Dispatch Loop](../pool/pool-dispatch.md) — the dispatch hub a
  loaded kernel runs under; this manager is the kernel's stdout sink while it runs.
- [SEQ Error-Handler / Fault Reporting](../seq/error-handler.md) — the SEQ-side fault
  log sink and the `'S:'` emitter's home; the Q7 exception path of §8 mirrors it.
- [nrtucode Logging + Leak-Tracking Allocator](../../runtime/nrtucode-logging-allocator.md)
  — **forward-link (planned, not yet authored)**: the *host* consumer that drains
  the SEQ/`pool_stdio` `[sev][\0str]` ring (`print_logs`, the `friendly_name`
  re-emit) reconciled in §6. This manager is the SUNDA-Q7 *device* analogue; the
  host code for the Q7 ring is the NRT custom-op runtime, out of the device blob's
  scope.
- [Confidence & Walls Model](../../reference/confidence-model.md) — the
  HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED tags used throughout.

---

## 10. Confidence ledger

**HIGH / OBSERVED** (direct disassembly or byte read):

- Manager located in `img_SUNDA_Q7_POOL_{DEBUG,RELEASE}` (Q7 GPSIMD core); source
  `file_io_manager.hpp`; 3 asserts at DRAM `0x2ce`/`0x362`/`0x5f1`; DRAM VA model
  `0x80000 + off` (SOC-window global `const16 8/0x93c4` → `0x80093c4`).
- The two virtual files `stdout_file` (mgr+0) / `stderr_file` (mgr+0x130), each
  `0x130 B`; fd `1=stdout`, `2=stderr` (dispatch `bnei a3,1` / `bnei a3,2`).
- Field offsets `enabled@+0`, `full_policy@+1 (==2 OVERWRITE)`, `num_slots@+0x0C`,
  `ring_dram@+0x10 (64b)`, `headptr_dram@+0x18 (64b)`, `stage@+0x28`,
  `line-buffer@+0x38`, `fill_count@+0x34`, `head@+0x128`.
- Write `0x1d50`: 240-byte chunked line buffer, `bytes_to_copy>0` assert, `memcpy`
  (vector `0x3388`), flush-on-240. Flush `0x1e1b`: `memcpy` 256-B slot to
  `ring[head%slots]`, `memset` slot, `head++`, `publish_head` (`0x1eb4`) writes head
  to a separate DRAM word + `memw` fence.
- Init `0xd10`/ctor `0x624`: per-core `prid`-gated, singleton @DRAM `0x809ef8`,
  spinlock; `memset` both files (`0x130`); **40-byte desc published to the DRAM ring
  at `ring_soc + slot*40` (stderr +`0x140`)** [CORRECTION]; assert both
  `full_policy==2`.
- `exit_flush 0x1fc0` flushes stderr+stdout, `memw`, spins.
- REFUTATION: SEQ `'S:'` logger `0x18b84` = newlib `fprintf` → FILE@`0x84d28`
  (locks `0x19ae4`/`0x19b3c`); SUNDA Q7 has **0** `'S:'`/`'P%i:'` strings; the Q7
  file_io ring has no `printf`. Distinct cores + distinct mechanisms.
- Per-gen: `file_io_manager` token only in SUNDA Q7-POOL (both builds, 3 hits each);
  `slot256` code signature present in SUNDA DEBUG+RELEASE, **absent** in
  CAYMAN/MARIANA Q7 (which use `'P%i:'` `printf`). Counts: `S:`=187 / `P%i:`=156 in
  the CAYMAN DEBUG DRAM images.

**MED / INFERRED:**

- The host READ side (poll the published head, drain 256-B slots) — inferred from the
  fenced producer protocol; the host consumer is the NRT custom-op runtime, out of
  scope of this device blob.
- That the host `print_logs` `[sev][\0str]` ring is the **identical** ring the
  device `0x18b84` logger feeds — the host-consumer format matches; the on-device
  head-advance is SEQ-firmware scope.
- The 16-byte slot header's per-field meaning, and the 40-byte `.desc` template's
  layout beyond `{enabled@0, full_policy@1}`.

**LOW / UNRECOVERED:**

- The enum's non-`OVERWRITE` members (`BLOCK`/`DROP` at `0`/`1`) — only `OVERWRITE
  (2)` is referenced in this image; the rest are inferred to exist.
- The host-side provisioning of the ring-config DRAM words (`112`/`124`) and the
  SOC-window global `0x93c4` — done by the host/loader (NRT scope, runtime-zero in
  the static image).

**Divergences fed to the per-Part reconcile:**

1. The backing report described the init 40-byte `memcpy` as installing a descriptor
   "into each file"; re-disassembly shows it is published to the **DRAM ring**
   (`ring_soc + slot*40`, stderr at `+0x140`). Folded as a CORRECTION in §4.
2. The cross-cluster pinned framing ("the engine behind `'S:'`") is **refuted** for
   `file_io_manager` specifically (§6) — it is the SUNDA-Q7 kernel-stdio ring, a
   different channel from the SEQ/`'P%i:'` `printf` log. Other pages that imply
   `file_io_manager` emits `'S:'` should be reconciled against §6/§7.
3. The `'S:'` survey figure is a **format-string** count (187), not the 178-entry
   SEQ dispatch-record count — keep the two metrics distinct on reconcile.
