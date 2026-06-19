# iDMA / Legacy DMA (IRAM Cache-Fill)

The SEQ engine — the Vision-Q7 NX "Cairo" sequencer core (`ncore2gp`) — runs a
**software-managed IRAM overlay cache** because its firmware *image* (≈114 KiB) is larger
than the core's instruction-RAM aperture (64 KiB). When the fetch FSM misses, a single
**cache line** has to be pulled out of the backing instruction stream in DRAM/HBM and
dropped into the resident IRAM cache window. The mechanism that performs that one transfer
is the subject of this page: the **iDMA**, whose C++ source file is named
`iram_dma.hpp` in CAYMAN and later, and `legacy_dma.hpp` in the older SUNDA firmware. The
two names are the same engine across two generations — and that older name is exactly why
the harness labels this lane "legacy DMA".

This is the **DMA-engine side** of the cache documented in
[SEQ IRAM Instruction Cache / Overlay](../seq/iram-cache.md). That page reconstructed the
*consumer* — the tag scan, the victim policy, the per-line state machine, the fetch glue.
This page reconstructs the *transfer itself*: the flat `src[len] → dst` descriptor, the two
backends it can drive (the Xtensa core's own iDMA hardware port through the general
local-register CSR mailbox, or a software DRAM ring), the poll-only completion, and — the
key framing — **why this is the "legacy" path** versus the modern descriptor-generation
engine (DGE) that fills the rest of this cluster. Everything below is re-disassembled from
the carved device image; no DMA hardware was run.

The substrate is the carved `CAYMAN_NX_POOL` **DEBUG** firmware image — the same object the
companion cache page used. The DEBUG build keeps every `'S:'` log format string in its DRAM
data segment, and each function loads its string with a two-halfword `const16` pair, so a
function's identity, its arguments, and its assertion call-site are recoverable byte-exact
from the string it references. The carve:

- DRAM blob `CAYMAN_NX_POOL_DEBUG_DRAM_get.data`, **28448 B**, sha256
  `7bdf6ed7ccd27b376a652064e2d191e3ada5dbbab3d4427eaaa0306ad6816ecd`.
- IRAM blob `CAYMAN_NX_POOL_DEBUG_IRAM_get.data`, **116768 B**, sha256
  `8e4412b99201f62d97110c7c641cf420fee361140763019c7f944c40ab9ed70a`.

Both were carved this pass from members `img_CAYMAN_NX_POOL_DEBUG_{DRAM,IRAM}_contents.c.o`
of `libnrtucode.a` (the firmware image is stored as a host-x86 `.rodata` OBJECT inside the
`.c.o`; `dd skip=0x60 count=<symsize>`), and both sha256 values match the
[`iram-cache.md`](../seq/iram-cache.md) anchors exactly. IRAM offsets equal device IRAM
virtual addresses (`.rodata` VMA == file offset for this carve). DRAM string addresses are
device DRAM VAs; the file offset into the carved DRAM blob is `VA − 0x80000`.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte / string / instruction read or re-disassembled from the carved image
**this pass**; `INFERRED` = reasoned over those reads; `CARRIED` = taken from a cited sibling
page at its original confidence. Crossed with `HIGH` / `MED` / `LOW`. Callouts: **QUIRK**
(counter-intuitive but real), **GOTCHA** (a reimplementation trap), **CORRECTION** (overturns
a naive reading), **NOTE** (orientation). Everything was re-disassembled with the native
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, GNU Binutils 2.34.20200201 / Xtensa Tools
14.09, FLIX/VLIW 32 B). Decoding the raw image as `-b binary -m xtensa` and forcing
`--start-address=` at each true function entry sidesteps the FLIX format-marker desync that
otherwise turns inter-function literal pools into spurious `.byte` runs.

---

## 1. Anatomy at a glance

The whole engine is three functions plus a poll routine, all SEQ-resident in the IRAM image:

| Function | IRAM @ | Role |
| --- | --- | --- |
| `start_fill_siram` | `0x6217` | Turn one cache index into one DMA transfer: compute `fetch_addr`, log, branch on backend byte, arm the transfer. |
| `DramRingDMA::enqueue` *(setup_enqueue_dispatch_descriptors)* | `0x6354` | SW-ring backend: validate length, allocate a ring slot, write the descriptor, commit. |
| `wait_for_dma_fill` | `0x61a8` | Busy-spin on the fill-done poll, then flip the line state `1 → 2` (VALID). |
| fill-done poll | `0x3b4c` | One probe: HW read-back of `glr[9]==0`, or SW ring-slot phase check. |
| ring reset / ctor | `0x3ba8` | Zero ring counters, seed the round-robin victim, init SW ring slots. |
| ring completion | `0x3c04` | SW phase-bit producer/consumer slot check. |

The transfer it carries is a **flat three-tuple**: a 64-bit source, a `u32` length, a 64-bit
destination. That is the entire descriptor — there is no dimension list, no cast, no
compute op, no second backend table. The DEBUG log binds the three fields in one string:

```
0x81578   "S: DMA enqueue 0x%llx[%u] -> 0x%llx"          // src[len] -> dst
```
`[HIGH/OBSERVED — string carved byte-exact from the DRAM blob @0x81578.]`

> **NOTE.** "One transfer = one cache line." Every iDMA call moves exactly `block_size` bytes
> (one line) from the instruction stream into the cache window. There is no scatter/gather
> and no multi-line batching at the descriptor level; batching is the *cache's* round-robin
> loop calling this once per victim. That flatness is the whole "legacy" story — see §6.

### String / file anchors (carved verbatim from the DRAM blob)

| DRAM VA | off | String (OBSERVED) |
| --- | --- | --- |
| `0x81215` | `1215` | `iram_dma.hpp` *(the `__FILE__` tag)* |
| `0x8150d` | `150d` | `S: wait_for_dma_fill: idx=0x%x` |
| `0x8152d` | `152d` | `S: start_fill_siram: fetch_addr=0x%llx` |
| `0x81555` | `1555` | `setup_enqueue_dispatch_descriptors` *(assert func-name)* |
| `0x81578` | `1578` | `S: DMA enqueue 0x%llx[%u] -> 0x%llx` *(the descriptor format)* |
| `0x8159d` | `159d` | `S: DramRingDMA::allocate(%zu) submit=%llu comp=%llu first_slot=%zu min_comp=%llu` |
| `0x815ef` | `15ef` | `S:   allocate done, returning first_slot=%zu count=%zu` |
| `0x81627` | `1627` | `S: drain_completions(%zu): comp=%llu submit=%llu drain_slot=%zu` |
| `0x81699` | `1699` | `S: submit_pending: first_slot=%zu count=%zu submit=%llu ring_id=%u` |

The C++ class is **`DramRingDMA`**; its public API — `allocate()` / `submit_pending()` /
`drain_completions()` — is a classic producer/consumer slot ring with a 64-bit monotonic
`submit` counter, a `comp(letion)` counter, a `ring_id`, and per-submit `ticket`s. All eight
strings are present in CAYMAN, MARIANA, and MARIANA_PLUS; **none** are present in SUNDA
(which carries `legacy_dma.hpp` instead) or in any Q7/POOL image. `[HIGH/OBSERVED — §6, §7.]`

---

## 2. The transfer descriptor — `src[len] → dst`

`start_fill_siram` @`0x6217` is the per-line entry that turns a cache index into one DMA
transfer. Instruction-exact (`ncore2gp`, this pass), the descriptor-build prologue:

```
6231: const16  a4, 8
6234: const16  a4, 0x5654         ; a4 = &g_block_size  ([0x85654])     [HIGH/OBSERVED]
6237: l32i.n   a4, a4, 0          ; a4 = block_size
6239: mull     a3, a3, a4         ; fetch_addr = idx * block_size       [HIGH/OBSERVED]
623c: s32i.n   a3, a1, 4          ; line[+4] = fetch_addr (the source offset)
6252: const16  a10, 0x152d        ; "S: start_fill_siram: fetch_addr=0x%llx"
6255: call8    0x18b84            ; printf
6260: const16  a3, 0x5658         ; a3 = &g_dma_backend  ([0x85658])    [HIGH/OBSERVED]
6263: l8ui     a3, a3, 0          ; backend byte (one byte)
6266: bbci     a3, 0, 0x62ac      ; bit0 clear -> HW path @0x62ac ; set -> SW ring
```

The three descriptor fields, named against the line descriptor (`a1` = the per-line struct
the cache manager handed in) and the `"0x%llx[%u]->0x%llx"` log:

| Field | Source | Meaning |
| --- | --- | --- |
| **src** | `line[+4]` = `fetch_addr` = `idx * block_size` | byte offset into the backing `'S:'` instruction stream; the SW path widens it to a 64-bit `base + offset` (§3b). |
| **len** | `block_size` (`g_block_size`, `[0x85654]`) | one cache line; logged as the `[%u]` field. |
| **dst** | `line[+16]` / `line[+20]` | the IRAM cache-window slot for this `idx`, supplied by the cache manager when it picked the victim line. |

`fetch_addr` is a pure linear map `idx → idx·block_size` — a `mull`, not a shift — so
`block_size` need not be a power of two at the iDMA level even though the cache initialises
it to one. `[src=fetch_addr HIGH/OBSERVED; len=block_size HIGH/OBSERVED; dst=line slot
HIGH/OBSERVED.]`

> **GOTCHA.** `block_size` is read **indirectly** through the global pointer slot
> `[0x85654]`, not from an immediate. A reimplementation that bakes a constant line size
> into the descriptor build will silently diverge from a firmware that re-derives it from
> CSR `glr[19]` (`0x1260`) at BEGIN time (see [`iram-cache.md`](../seq/iram-cache.md)). The
> `0x5654` pointer is referenced **20 times** across the IRAM image; the `0x5658` backend
> byte **4 times** (`rg -c` over the full disasm) — consistent with the cache page's counts.

---

## 3. The two backends — selected by one DRAM byte

A single byte at DRAM `[0x85658]` (`g_dma_backend`) chooses the engine: **bit 0 clear → (a)
HW iDMA**, **bit 0 set → (b) SW `DramRingDMA`**. The `bbci a3, 0, 0x62ac` at `0x6266` is the
fork. The two backends share the same descriptor (`src[len] → dst`) and the same poll
contract, but touch completely different surfaces.

### 3a. Backend (a) — the Xtensa core's own iDMA port (`glr[9..12]` CSR mailbox)

The HW arm @`0x62ac..0x62db`, byte-exact this pass:

```
62ac: l32i.n   a3, a1, 16        ; a3 = line[+16]  (dst word 0)
62b1: const16  a4, 0x1140        ; a4 = 0x1140 = glr[10]
62b4: s32i.n   a3, a4, 0         ; REG(0x1140) = line[+16]           desc word 0   [HIGH]
62b6: l32i.n   a3, a1, 20        ; a3 = line[+20]  (dst word 1)
62be: s32i.n   a3, a4, 32        ; REG(0x1140+32 = 0x1160) = line[+20]  word 1     [HIGH]
62c0: l32i.n   a3, a1, 4         ; a3 = line[+4] = fetch_addr  (the source offset)
62c5: const16  a4, 0x1180        ; a4 = 0x1180 = glr[12]
62c8: s32i.n   a3, a4, 0         ; REG(0x1180) = fetch_addr           desc word 2   [HIGH]
62cd: const16  a3, 0x1120        ; a3 = 0x1120 = glr[9]
62d0: movi.n   a4, 1
62d2: s32i.n   a4, a3, 0         ; REG(0x1120) = 1                    *** KICK ***   [HIGH]
62d7: l32i.n   a3, a1, 8         ; a3 = line[+8] = idx
62d9: s32i.n   a3, a2, 32        ; cache[+32] = idx  (pending-DMA slot)
62db: retw.n
```

As annotated C pseudocode (real symbols/addresses; consistent with `iram-cache.md`):

```c
// start_fill_siram, HW backend (g_dma_backend bit0 == 0)   @0x62ac
// glr[9..12] of tpb_xt_local_reg.general : base 0x1000, stride 0x20
#define GLR(i)   (*(volatile uint32_t *)(0x1000 + (i) * 0x20))
#define IDMA_KICK   GLR(9)    // 0x1120
#define IDMA_W0     GLR(10)   // 0x1140
#define IDMA_W1     GLR(11)   // 0x1160
#define IDMA_W2     GLR(12)   // 0x1180

void start_fill_siram_hw(line_t *line, cache_t *cache) {
    IDMA_W0 = line->desc0;          // line[+16]  (dst word 0)
    IDMA_W1 = line->desc1;          // line[+20]  (dst word 1)
    IDMA_W2 = line->fetch_addr;     // line[+4]   (= idx * block_size, source offset)
    IDMA_KICK = 1;                  // 0x1120 <- 1   arms the core iDMA port
    cache->pending_idx = line->idx; // cache[+32]   stash for wait_for_dma_fill
}
```

The four CSRs are the `tpb_xt_local_reg.general` block (base `0x1000`, ArraySize 60, stride
`0x20`): `glr[9]@0x1120`, `glr[10]@0x1140`, `glr[11]@0x1160`, `glr[12]@0x1180`. They are
**generic scratch local registers the SEQ firmware repurposes as the iDMA descriptor
mailbox** — the same trick the Setup-Halt path uses to stash a resume PC in `glr` slots. The
underlying engine is the `ncore2gp` `IDMAInterface = ACELite … target dataidma`, the Xtensa
core's *local* DMA port — **not** the SoC SDMA. `[glr identities HIGH/OBSERVED; the absolute
aperture-base alias carries the 0x0-vs-0x400000 ambiguity but does not change the register
identity.]`

> **QUIRK.** `glr[11]@0x1160` is written **without its own `const16`**: the code reuses
> `a4 = 0x1140` from the `glr[10]` write and stores at `+32` (`0x1140 + 0x20 = 0x1160`). So
> the descriptor-build path emits only **one** `const16 …,0x1140` for two writes. A
> reimplementation that counts CSR writes by counting `const16` immediates will undercount
> the `0x1160` write. The four CSR const16 counts over the whole image (`rg -c`): `0x1120`=3,
> `0x1140`=1, `0x1160`=1, `0x1180`=1. `[HIGH/OBSERVED.]`

### 3b. Backend (b) — the software `DramRingDMA`

The SW arm @`0x6287..0x629f` builds a **64-bit** source address (the HW path only ever sees
the low `fetch_addr` offset; the SW path adds the stream base with carry):

```
6287: l32i     a5, a1, 4         ; a5 = fetch_addr (lo)
628a: add      a12, a3, a5       ; base_lo + fetch_addr_lo
628d: saltu    a3, a12, a3       ; carry out of the low add
6290: add      a13, a4, <carry>  ; base_hi + carry      => 64-bit source address
6298: l32i     a14 = block_size  ; the length
629a: call8    0x6354            ; DramRingDMA::enqueue
629d/629f:  store returned slot ids into line[+24] / line[+28]
            -> cache[+48] / cache[+52]
```

The enqueue body @`0x6354` (`entry a1, 96` — large frame), byte-exact this pass:

```
6354: entry    a1, 96
6378: l16ui    a2, a1, 22        ; load the byte-count field
637b: beqz.n   a2, 0x6391        ; count == 0 ? -> assert path
6383: const16  a10, 0x1555       ; "setup_enqueue_dispatch_descriptors"
638e: call8    0xa304            ; assert handler (count must be non-zero)
63c0: const16  a10, 0x1578       ; "S: DMA enqueue 0x%llx[%u] -> 0x%llx"
63c3: call8    0x18b84           ; log the descriptor
```

After the assert and log it calls the slot allocator `0x6444` (the 64-bit ring-pointer
arithmetic with carry, `saltu @0x6464` — the monotonic `submit` counter), the per-slot
descriptor writers `0x6514` / `0x6538` (write `src-lo` / `src-hi` / `dst` / `len` into the
ring slot), and `0x655c` (commit), returning `(first_slot, count)` in `a1+24/a1+28`. As
pseudocode:

```c
// start_fill_siram, SW backend (g_dma_backend bit0 == 1)   @0x626c
ring_slot_t start_fill_siram_sw(line_t *line, cache_t *cache,
                                uint64_t stream_base) {
    uint64_t src = stream_base + line->fetch_addr;      // 64-bit, carry-propagated
    uint32_t len = g_block_size;                        // [0x85654]
    // DramRingDMA::enqueue == setup_enqueue_dispatch_descriptors @0x6354
    ring_slot_t s = DramRingDMA_enqueue(src, len, line->dst);  // asserts len != 0
    cache->slot_a = line->slot_lo;   // line[+24] -> cache[+48]
    cache->slot_b = line->slot_hi;   // line[+28] -> cache[+52]
    return s;
}
```

`[HIGH on the enqueue identity + assert/log/alloc structure; the exact per-slot byte layout
inside 0x6514/0x6538 is reachable but INFERRED-HIGH; the carried tuple
(src-lo/src-hi/dst/len) is HIGH from the "0x%llx[%u]->0x%llx" log + the §3b address build.]`

> **CORRECTION.** The companion cache page labelled the two recorded slot ids
> `cache[+48]`=submit / `cache[+52]`=comp. The byte-exact stores this pass are
> `line[+28]→cache[+52]` and `line[+24]→cache[+48]`. Both ids are recorded, and the poll
> (§4) reads **both** back, so the submit-vs-comp labelling does not affect the mechanism —
> it is the one residual ambiguity here. `[slots recorded HIGH; the +48/+52 labelling MED.]`

---

## 4. Completion — poll, never IRQ

`wait_for_dma_fill` @`0x61a8` is a **busy-spin**. There is no interrupt, no surprise handler,
and no notification-queue hop in the fill path: the SEQ core stalls its own fetch on the spin
until the line is resident. Byte-exact this pass:

```
61a8: entry    a1, 48
61b2: l32i.n   a11, a2, 32       ; idx = cache[+32]  (the pending-DMA slot)
61b7: const16  a10, 0x150d       ; "S: wait_for_dma_fill: idx=0x%x"
61ba: call8    0x18b84           ; printf
61c5: j        0x61c8            ; --- spin head ---
61c8: mov.n    a10, a2
61ca: call8    0x3b4c            ; r = fill_done_poll(cache)
61cd: bnez     a10, 0x61c5       ; while (busy) spin                            [HIGH]
...
61f0: ...                        ; --- on done (HW) ---
6201: l32i     a3, a2, 64        ; tag_base = cache[+64]
6204: l32i.n   a2, a2, 32        ; idx = cache[+32]
6206: slli     a2, a2, 4         ; idx << 4   (16-B per-line descriptors)
6209: add.n    a2, a3, a2        ; entry = tag_base + idx*16
620b: movi.n   a3, 2
620d: s32i.n   a3, a2, 0         ; entry[0] = 2  => line VALID / READY            [HIGH]
6215: retw.n
```

```c
// wait_for_dma_fill   @0x61a8
void wait_for_dma_fill(cache_t *cache) {
    uint32_t idx = cache->pending_idx;            // cache[+32]
    LOG("S: wait_for_dma_fill: idx=0x%x", idx);   // 0x8150d
    while (fill_done_poll(cache)) { }             // busy-spin, no IRQ           @0x3b4c
    line_t *e = (line_t *)(cache->tag_base + (idx << 4));  // cache[+64] + idx*16
    e->state = 2;                                 // 1 (in-flight) -> 2 (VALID)
}
```

The poll @`0x3b4c` branches on the **same** backend byte `[0x85658]`:

```
3b56: const16  a3, 0x5658
3b59: l8ui     a3, a3, 0
3b5c: bbci     a3, 0, 0x3b8e     ; bit0 clear -> HW probe @0x3b8e ; set -> SW ring
; --- SW ring (0x3b62) ---
3b62: l32i     a3, a2, 48        ; submit slot cache[+48] -> [fp+16]
3b68: l32i.n   a2, a2, 52        ; comp   slot cache[+52] -> [fp+20]
3b80: call8    0x3c04            ; ring completion check
3b85: xor      a2, a10, a2       ; done bit
; --- HW DMA (0x3b8e) ---
3b8e: const16  a2, 0x1120        ; glr[9]
3b94: l32i.n   a2, a2, 0         ; v = REG(0x1120)
3b96: movi.n   a3, 0
3b98: saltu    a2, a3, a2        ; busy = (0 < v) = (v != 0)                     [HIGH]
```

```c
// fill_done_poll   @0x3b4c   -- returns "busy" (1 = keep spinning, 0 = done)
int fill_done_poll(cache_t *cache) {
    if (g_dma_backend & 1) {                        // [0x85658] bit0
        // SW ring: phase-bit producer/consumer slot check
        return ring_completion(cache->slot_a, cache->slot_b) ^ 1;   // @0x3c04
    } else {
        // HW iDMA: the KICK wrote 1; the port clears glr[9] on completion
        return GLR(9) != 0;                         // DONE when 0x1120 reads back 0
    }
}
```

**The HW completion contract is a self-clearing register:** the KICK wrote `1` to
`glr[9]@0x1120`; the core iDMA port clears it to `0` on completion, and the poll's
`saltu a2, 0, v` reports busy iff `v != 0`. This is *identical* to the
[`iram-cache.md`](../seq/iram-cache.md) statement — **KICK = write 1 to `0x1120`, completion
= read-back 0** — and was re-confirmed here byte-exact. `[HIGH/OBSERVED.]`

The SW ring completion @`0x3c04` reads a ring-slot header, extracts a 3-bit and two 2-bit
fields (`extui @0x3c1d/0x3c29/0x3c3f`), compares a phase/seq field (`bne @0x3c48`), and sets
the completed flag — a textbook phase-bit ring poll. Ring reset @`0x3ba8` (the
`DramRingDMA` ctor/reset) zeroes `cache[+32]` and `cache[+56]`, seeds the round-robin victim
at `cache[+36] = num_blocks − 1` (so the first `+1` wraps to 0), and, in SW mode
(`descr[+60]` bit 0), walks the `num_blocks` ring slots (`cache[+64]`, stride 16) initialising
each. `[HIGH/OBSERVED.]`

> **NOTE.** Because completion is a spin and not an interrupt, the iDMA cannot overlap a fill
> with useful SEQ work: the core *is* the consumer and *is* the waiter. That single-threaded,
> stall-the-fetch model is fine for instruction refill (the core has nothing to run until the
> line lands) but is exactly what the DGE replaces for bulk tensor copy, where the engine must
> run *concurrently* with compute — see §6.

---

## 5. The cache-manager state and CSRs it touches

The iDMA reads two DRAM globals and writes a small slice of the cache control struct and the
four `glr` CSRs. The state map (offsets relative to the cache control base):

| Location | Field | Touched by |
| --- | --- | --- |
| `[0x85654]` | `block_size` (per-line transfer **length**; cached from CSR `glr[19]` `0x1260`) — **20** refs (`rg -c`) | `start_fill_siram` `0x6234` |
| `[0x85658]` | iDMA backend byte: bit0 `0`=HW iDMA, `1`=SW ring — **4** refs (`rg -c`) | `start_fill_siram` `0x6260`; poll `0x3b56` |
| `cache[+24]` | `num_blocks` (ring depth = # cache lines) | init |
| `cache[+32]` | pending-DMA idx/slot | `start_fill_siram` `0x62d9`; wait `0x61b2` |
| `cache[+36]` | round-robin victim (rotate; reset seed = `num_blocks − 1`) | rotate; reset `0x3ba8` |
| `cache[+48]` | DMA slot id A | `start_fill_siram` SW arm; poll `0x3b62` |
| `cache[+52]` | DMA slot id B | `start_fill_siram` SW arm; poll `0x3b68` |
| `cache[+60]` | HW(0)/SW(1) cache-mode flag (also gates ring-slot init in `0x3ba8`) | reset |
| `cache[+64]` | tag/valid array base (per-line 16-B descriptors; `state[0]` = `1`/`2`) | wait `0x6201` |

| CSR | `glr` | Field |
| --- | --- | --- |
| `0x1120` | `glr[9]` | **KICK** (write 1) / **DONE** (read 0) |
| `0x1140` | `glr[10]` | descriptor word 0 (`= line[+16]`, dst) |
| `0x1160` | `glr[11]` | descriptor word 1 (`= line[+20]`, dst) |
| `0x1180` | `glr[12]` | descriptor word 2 (`= line[+4] = fetch_addr`, src offset) |
| `0x1260` | `glr[19]` | `block_size` source config (→ `[0x85654]`) |

`[CSR identities and writes HIGH/OBSERVED; the glr[19]→[0x85654] plumbing is CARRIED from the
cache page.]`

---

## 6. Legacy vs DGE — the contrast that names this page

The iDMA and the **DGE** (Descriptor-Generation Engine, the rest of this cluster) are
**disjoint engines on disjoint register surfaces, in different firmware images, for different
jobs.** The iDMA is the SEQ core's *instruction-feed* DMA; the DGE is the custom-op *tensor*
copy/collective/gather engine. The iDMA is **not** a DGE backend, and the DGE selector cannot
reach it: they share no code, no CSRs, and no source file. Side by side:

| | **iDMA** (`iram_dma.hpp` / `legacy_dma.hpp`) | **DGE** (`dge_*`, this cluster) |
| --- | --- | --- |
| firmware image | SEQ / NX-engine POOL | Q7 + NX POOL |
| purpose | **instruction**-cache line refill | custom-op **tensor** copy / collective / gather |
| descriptor | flat `src(64b)[len:u32] → dst(64b)`, one cache line (`block_size` B) | N-D: src/dst base + up to 5+2 dims of `{num[u16], step[i32]}` + cast + compute-op + bounds regs |
| descriptor log | `0x81578` `S: DMA enqueue 0x%llx[%u] -> 0x%llx` | per-backend logs (Pool / RTL / SW) |
| emit primitive | **enqueue** (one tuple) | **GENERATE / DIMPUSH / REGWRITE** — a descriptor *program* |
| transfer engine | core iDMA `glr[9..12]` (`0x1120`/`40`/`60`/`80`), **or** SW `DramRing` | SoC SDMA UDMA M2S/S2M via a ring-descriptor manager |
| backend select | **1 byte** `[0x85658]` HW(0)/SW(1) | backend-availability table (Pool / RTL / software) |
| completion | poll `glr[9]==0`, or SW ring slots | semaphore + `dmacomplete`, ring tail-pointer writeback |

What makes the iDMA **legacy**, grounded:

1. **One transfer vs a descriptor program.** The iDMA's "emit" is a single `src[len]→dst`
   enqueue. The DGE *generates a program* of `GENERATE`/`DIMPUSH`/`REGWRITE` ops that build
   an N-dimensional reshape with cast and compute folded in. `[HIGH — the log strings and
   emit shapes are disjoint.]`
2. **Fixed registers vs a backend selector + reshape.** The iDMA hard-wires four scratch
   `glr` CSRs (or one SW ring); the DGE picks among Pool/RTL/software backends and a
   reshape. `[HIGH/OBSERVED.]`
3. **The literal name.** In SUNDA the source file is `legacy_dma.hpp`; in CAYMAN+ it was
   renamed/re-abstracted to `iram_dma.hpp` and wrapped in the `DramRingDMA` class (§7). That
   rename — not a deprecation — is where "legacy" comes from. The only `"legacy"` *string* in
   the CAYMAN image is the unrelated branch-hint `"…to preserve legacy behavior"` (`0x83d42`),
   which is about branch-NT handling, not DMA. `[rename HIGH/OBSERVED.]`

> **CORRECTION.** "Legacy" here does **not** mean dead or dropped. The path is the **active**
> I-cache fill engine in every generation that ships it, called from the live cache-miss path.
> The "SUNDA-only legacy that was later dropped" reading is wrong: the modern
> `iram_dma.hpp`/`DramRingDMA` form is a **CAYMAN-era addition**; the SUNDA-era form exists
> under the `legacy_dma.hpp` name. The mechanism is NX/SEQ-engine-only across **all**
> generations — never present in a Q7/POOL image. `[HIGH/OBSERVED — §7.]`

> **NOTE — forward links.** The DGE pages are still being authored. Once available, see
> [DGE Setup + Context Init](dge-setup.md) for the descriptor-context init that this page
> contrasts against (currently a stub). The SoC SDMA hardware the DGE drives — and which the
> iDMA pointedly does *not* — will be documented at
> [UDMA Hardware Engine](../../dma/udma-hw-engine.md) (Part 9, **not yet authored**).

---

## 7. Provenance, presence, and consumers

**The rename (per-gen `__FILE__` tags, `rg -a` over each carved DRAM `.rodata`):**

| Image (NX POOL DEBUG) | `iram_dma.hpp` | `DramRingDMA` | `DMA enqueue` | `legacy_dma.hpp` |
| --- | --- | --- | --- | --- |
| SUNDA_NX_POOL_DEBUG | 0 | 0 | 0 | **1** |
| CAYMAN_NX_POOL_DEBUG | **1** | **1** | **1** | 0 |
| MARIANA_NX_POOL_DEBUG | **1** | **1** | **1** | 0 |

SUNDA additionally ships the free-function names `ring_rollover`, `update_victim`,
`prepare_cache_line` (1 each) and the source-tree path `…/NeuronUcode/sunda/seq/…`
(1) — confirming this is the SEQ engine's source tree. The Q7/POOL images carry **zero** of
`iram_dma.hpp` / `DramRingDMA` / `legacy_dma.hpp` (checked on `CAYMAN_Q7_POOL_DEBUG`,
89344 B). The mechanism evolved as:

```
legacy_dma.hpp  (SUNDA: free fns prepare_cache_line / update_victim / ring_rollover)
      --renamed + re-abstracted-->
iram_dma.hpp    (CAYMAN+: C++ class DramRingDMA  allocate/submit_pending/drain_completions;
                 cache-side names also shift: prepare_cache_line -> wait_for_cache_line)
```

`[All counts HIGH/OBSERVED via rg -c -a over the carved DRAM blobs this pass.]`

**The consumer.** The sole proven client is the SEQ software I-cache documented in
[`iram-cache.md`](../seq/iram-cache.md). The live call chain:

```
fetch FSM
  └─ wait_for_cache_line(pc)                         [0x5cd0]
       └─ query / hit  -->  or MISS
            └─ replace(round-robin victim)           [0x6068]
                 └─ start_fill_siram(idx)            [0x6217] ── HW: glr[9..12] KICK
                                                              └─ SW: DramRingDMA::enqueue [0x6354]
            └─ wait_for_dma_fill                      [0x61a8] -> poll [0x3b4c] -> state 1->2 -> execute
```

The iDMA exists to keep the 64 KiB IRAM aperture fed from the ≈114 KiB instruction image:
the image is larger than the aperture, so the high vector body is paged in on demand. It is
**not** used by the external-lib loader (a host-side prelink + `kernel_info_table` install,
no DMA of its own), **not** by the host-resident segment loader, and **not** by the DGE
tensor copies (which use the SoC SDMA). `[positive cache linkage HIGH/OBSERVED; "sole
consumer" MED — an absence claim over the decoded SEQ span and the Q7 grep.]`

---

## 8. Honest gaps

- The iDMA core functions (`0x6217`, `0x61a8`, `0x6354`, `0x62e0`, `0x3b4c`, `0x3ba8`,
  `0x3c04`) decode cleanly as scalar windowed-ABI code; only short literal-pool `.byte` runs
  (FLIX format markers) sit *between* functions. Every descriptor→CSR write, the `1`/`2`
  state constants, the `mull`/`saltu` arithmetic, the backend-select `bbci`, and the poll
  loops are instruction-exact. `[HIGH/OBSERVED.]`
- The `cache[+48]`/`cache[+52]` submit-vs-comp **labelling** is the one residual ambiguity
  (§3b CORRECTION). Both ids are recorded and both are read back by the poll, so the
  labelling does not affect correctness. `[MED.]`
- The exact byte layout **inside** a `DramRingDMA` ring slot (`0x6514`/`0x6538` writers) is
  reachable but its field offsets are `INFERRED-HIGH`; the carried tuple
  (`src-lo`/`src-hi`/`dst`/`len`) is `HIGH` from the `"0x%llx[%u]->0x%llx"` log + the §3b
  address build.
- "Sole consumer = the I-cache" and "absent from Q7" are absence claims over the
  decoded/grepped spans; the positive cache linkage and the per-gen grep are `HIGH`, the
  exhaustive-absence is `MED`.

---

## 9. Cross-references

- [SEQ IRAM Instruction Cache / Overlay](../seq/iram-cache.md) — the **consumer**: the
  cache that calls `start_fill_siram` / `wait_for_dma_fill`; same KICK/poll contract on
  `0x1120/40/60/80`.
- [DGE Setup + Context Init](dge-setup.md) — the modern engine this page contrasts against
  *(stub — pending authoring)*.
- [UDMA Hardware Engine](../../dma/udma-hw-engine.md) — the SoC SDMA the DGE drives and the
  iDMA does **not** *(Part 9 — not yet authored)*.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the meaning of the
  `[HIGH/OBSERVED]`-style tags used throughout.
