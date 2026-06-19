# SEQ IRAM Instruction Cache / Overlay

The SEQ engine — the Vision-Q7 NX "Cairo" sequencer core (`ncore2gp`) — **does not
execute its full program directly out of IRAM**. The shipped firmware *image* is 114 KiB
but the core's instruction-RAM aperture is only 64 KiB, so the firmware carries a
**software-managed IRAM overlay cache** (`cache.hpp`): a resident region holds boot + the
fetch FSM + the cache manager itself, and the larger instruction stream is paged into a
fixed **32 KiB IRAM cache window** on demand, line-by-line, by DMA. This page reconstructs
that cache — its geometry, the fully-associative tag scan, the round-robin victim policy,
the per-line state machine, the two DMA fill backends (the HW iram-DMA general-LR bundle
and a software DRAM ring), and the fetch-side glue that pre-stages the next line — entirely
from the disassembly of the carved device image.

The substrate is the carved `CAYMAN_NX_POOL` **DEBUG** firmware image. The DEBUG build keeps
every `'S:'` log format string in its DRAM data segment, and each cache function loads its
string with a two-halfword `const16` pair, so the strings are a precise oracle: a function's
identity, its arguments, and its assertion line numbers are all recoverable byte-exact from
the string it references. The **PERF** build strips these strings and re-lays-out the code,
so every IRAM offset on this page is DEBUG-specific (see the [DEBUG-vs-PERF](#15-debug-vs-perf)
note at the end). All addresses are IRAM-image offsets, which equal device IRAM virtual
addresses (`.rodata` VMA == file offset for this carve). DRAM string addresses are device
DRAM VAs; the file offset into the carved DRAM blob is `VA − 0x80000`.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte / string / instruction read or re-disassembled from the carved image
**this pass**; `INFERRED` = reasoned over those reads; `CARRIED` = taken from a cited sibling
report at its original confidence. Crossed with `HIGH` / `MED` / `LOW`. Callouts: **QUIRK**
(counter-intuitive but real), **GOTCHA** (a reimplementation trap), **CORRECTION** (overturns
a naive reading), **NOTE** (orientation). Everything below was re-disassembled with the native
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, Binutils 2.34.20200201 / Xtensa Tools 14.09)
against the four carved `img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM,SRAM,EXTRAM}_contents.c.o`
members of `libnrtucode.a`.

> **GOTCHA — this is the Q7 datapath core, not the NCFW management core.** The carved image
> here decodes under the *only* shipped Xtensa config, `ncore2gp` (Vision-Q7, FLIX-bearing).
> That is correct for the SEQ engine — its body genuinely contains FLIX/IVP vector bundles.
> Do **not** confuse it with the scalar Xtensa-LX [NCFW management core](../../uarch/ncfw-lx-core.md),
> whose firmware is a different binary (`libncfw.so`, not in this tree) and which `ncore2gp`
> *mis*-decodes. The cache **manager** functions on this page are *scalar* windowed-ABI code
> and decode instruction-exact; the FLIX desync only bites in the fetch front-end body and the
> high vector-kernel body (see [§14](#14-desync-spans--honest-gaps)). `[HIGH/OBSERVED]`

---

## 0. Why a software I-cache exists at all — the image > aperture fact

The reason the cache exists is a one-line size mismatch, and it is the first thing to verify.

```
  carved IRAM .rodata  = 0x1c820 = 116768 B = 114 KiB      ← the firmware IMAGE
  ncore2gp InstRAM     = 0x10000 =  65536 B =  64 KiB @VA 0  ← the hardware APERTURE
                         ─────────────────────────────────
  114 KiB  >  64 KiB    ⇒  the image cannot be resident in IRAM all at once.
```

The `ncore2gp` config also declares `L1SCacheBytes = 0` and `L1VCacheBytes = 0` — **no
hardware L1 instruction cache is configured**. The IRAM is a flat 64-KiB local memory. So the
"I-cache" is a *pure software overlay* the firmware builds on top of that flat memory: a
**resident** half (boot, the FSM, the cache manager, the DGE/RDMA blocks — the low/mid image)
plus a **32-KiB cache window** into which lines of the larger instruction stream are DMA'd on
demand. `[image>aperture HIGH/OBSERVED; the resident-vs-cached byte boundary is computed at
runtime from `block_size`, not baked in, so the exact split is MED/INFERRED — see §14.]`

> **NOTE — there is no firmware-baked backing store in this build.** The carved
> `img_CAYMAN_NX_POOL_DEBUG_SRAM` and `_EXTRAM` `.rodata` sections are **size 0**. The backing
> instruction stream the cache fills *from* is supplied at runtime — host-staged into DRAM/HBM
> and referenced by a `fetch_addr` the cache computes — not carried as an SRAM/EXTRAM blob.
> `[empty-in-this-build HIGH/OBSERVED; physical home of the runtime backing buffer
> INFERRED DRAM/HBM, MED.]`

This image-over-aperture relationship is the contract the on-device prelinker
([External-Lib Prelink Validation](../pool/prelink-validation.md)) and the host forward-link /
staging path must respect: they lay out the instruction stream that this cache pages in, at the
block granularity the cache expects.

---

## 1. Cache geometry, at a glance

```
  REGION = 1<<15 = 32768 B (32 KiB)        ← fixed, half the 64-KiB IRAM
  block_size                                ← runtime, power-of-two (CSR glr[19] 0x1260)
  num_blocks = 32768 / block_size           ← floor; one cache LINE per block
  per-line descriptor = 16 bytes            ← in DRAM, tag/valid array, num_blocks entries
  associativity = FULLY ASSOCIATIVE (SW mode: linear tag scan over num_blocks entries)
  replacement   = ROUND-ROBIN / FIFO (single circular victim pointer, NOT LRU)
  line state    = {0 invalid, 1 fill-in-flight, 2 valid/ready}
  fill          = DMA, one block_size line at a time, from the backing 'S:' stream
                    backend A: HW iram-DMA via general-LR CSRs 0x1120/40/60/80
                    backend B: SW DramRingDMA ring (DRAM-resident)
  prefetch      = +1 sequential stride (fetch path) + branch-hint push (descr[+56] bitmask)
```

Every figure here is `[HIGH/OBSERVED]` and is re-derived instruction-by-instruction in the
sections below. The two structural choices worth flagging up front: the region is **fixed at
32 KiB** (a literal `1<<15`), while `block_size` is a **runtime** power-of-two read from a CSR
— so `num_blocks` is computed at init, not compiled in.

---

## 2. String anchors — the module + log oracle  `[HIGH/OBSERVED]`

The DEBUG DRAM image carries the source-file tags and every cache log string. These are the
proof of which modules implement the cache, and they pin each function's arguments and assert
lines. `VA = DRAM_file_off + 0x80000`. Source-file tags:

| DRAM VA | string |
|---|---|
| `0x81215` | `iram_dma.hpp` |
| `0x812d0` | `cache.hpp` |
| `0x81ac7` | `fetch.hpp` |
| `0x82c64` | `branch_prefetch_hint.cpp` |

Cache lookup / fill / evict strings, each with the IRAM `const16` low-half that loads it (the
high half is `0x0008` for all — i.e. DRAM page `0x80000`):

| DRAM VA | `const16` low | string |
|---|---|---|
| `0x812ab` | `0x12ab` | `S: IRAM cache init, block_size=0x%x` |
| `0x81407` | `0x1407` | `S: I$ Hit, iram_idx=0x%x` |
| `0x81421` | `0x1421` | `S: I$ Miss` |
| `0x8142d` | `0x142d` | `S: query addr 0x%llx, tag 0x%llx, vld=0x%x` |
| `0x81459` | `0x1459` | `S: query addr 0x%llx, tag 0x%llx` |
| `0x8147f` | `0x147f` | `S: replace: orig victim=0x%x` |
| `0x8149d` | `0x149d` | `S: replace: collision on cache idx=0x%x, updating again` |
| `0x814d6` | `0x14d6` | `S: replace: idx(updated victim_idx)=0x%x, addr=0x%llx` |
| `0x8150d` | `0x150d` | `S: wait_for_dma_fill: idx=0x%x` |
| `0x8152d` | `0x152d` | `S: start_fill_siram: fetch_addr=0x%llx` |
| `0x81578` | `0x1578` | `S: DMA enqueue 0x%llx[%u] -> 0x%llx` |
| `0x8159d` | — | `S: DramRingDMA::allocate(%zu) submit=%llu comp=%llu first_slot=%zu min_comp=%llu` |
| `0x81780` | `0x1780` | `S: wait_for_cache_line: pc=0x%llx, mask=0x%llx, blk_sz=%d, idx=0x%x, nxt_idx=0x%x` |
| `0x817e7` | `0x17e7` | `S: wait_for_valid: (HW cache) iram_idx=%x, state=0x%x` |
| `0x8182d` | `0x182d` | `S: wait_for_valid: (SW cache) iram_idx=%x, state=0x%x` |
| `0x81864` | `0x1864` | `S: is_pc_in_bounds: pc=0x%llx bound=[0x%llx, 0x%llx]` |
| `0x81ae9` | `0x326e` | `S: RTL_PC_check_delta: curr_dma_idx_vld=0x%x, tgt_cache_idx=0x%x, curr_pc=0x%llx` |
| `0x81c04` | `0x3387` | `S: Evict? next_dma_cache_idx=0x%x, ok_to_evict=0x%x, curr_dma_idx_vld=0x%x, ...` |
| `0x81c82` | `0x33ef` | `S: next_dma_cache_idx is the same with curr_cache_line_idx, rotate to next victim and retry` |
| `0x81dd6` | `0x3595` | `S: Push? next_vld=0x%x, next_cache_idx=0x%x, cache_idx_pushed=0x%x, idx_state=0x%x` |
| `0x82c93` | `0xcff4` | `S: BranchPrefetchHint` |
| `0x83c49` | `0x152f6` | `S: PrefetchHelper : Taken branch addr ... prefetching branch target 0x%llx` |

The log helper itself is `0x18b84` (**254** call sites across the image, `rg -c` this pass) —
called with `a10` = format-string pointer, `a11..` = varargs. The assertion helper is
`0xa304` (**43** call sites) — called with `a10` = message string, `a11` = `"cache.hpp"`
(`const16 0x12d0`), `a12` = source line number. This `a12`-carries-the-line convention is how
every "cache.hpp line N" claim below is grounded.

> **NOTE — `'S:'` is the DEBUG marker.** Every string here is `'S:'`-prefixed and resolved
> *locally* from the engine's own DRAM image (the firmware logs to its own ring, not to a host
> printf). In the PERF build these strings and their `const16` loads are gone, and the
> compiler re-schedules the code around the removed loads — so the IRAM offsets shift. The
> *algorithm* is identical; only the addresses move.

---

## 3. Cache init — geometry + tag-array allocation  `@0x2a84`  `[HIGH/OBSERVED]`

`BEGIN` (the boot path) calls the IRAM-cache init at `0x2a84`. It establishes the cache
control struct, computes the geometry, programs the HW block-size mask CSR, selects HW-vs-SW
mode, and (in HW mode) allocates and zeroes the DRAM tag/valid array. The control struct base
is `a2 = &state[0x855e0 + 24]` — i.e. the cache struct lives at offset `+24` inside the central
SEQ state struct at DRAM `[0x855e0]`.

```c
// IRAM-cache init  @0x2a84  ("entry a1, 64")
// a2_in = BEGIN arg byte (low bit selects HW/SW cache); cache = &state[0x855e0 + 24].
void iram_cache_init(uint8_t begin_arg /* a2 in */) {
    cache_t *cache = (cache_t *)&g_seq_state[0x855e0 + 24];   // 0x2a87-0x2a90: const16 0x55e0 ; addi 24
    g_init_arg_byte = begin_arg;                              // 0x2a92: s8i a2, a1, 28 (stack scratch)

    uint32_t block_size = g_block_size;                       // 0x2aa0: l32i [0x85654]
    LOG("S: IRAM cache init, block_size=0x%x", block_size);   // 0x2aa5: const16 0x12ab -> 0x18b84

    // (1) power-of-two check on block_size against the fixed 32-KiB region.
    if ((1u << 15) % block_size != 0)                         // 0x2abd slli 15 ; 0x2ac0 remu
        _Assert("...", "cache.hpp", 97);                      // 0x2ad4 movi a12,97 ; 0x2add call 0xa304

    // (2) num_blocks = floor(32768 / block_size).
    cache->num_blocks = (1u << 15) / block_size;              // 0x2aed slli 15 ; 0x2af0 quou ; 0x2af3 s32i +24

    // (3) block-index mask + log2(block_size) EXPONENT selector (used to program the HW CSR).
    cache->line_shift  = block_size >> 6;                     // 0x2bd1 srli 6  ; 0x2bd4 s32i +16
    cache->line_count  = some_scale * cache->line_shift;      // 0x2bda mull    ; 0x2bdd s32i +20
    int exp = log2_select(block_size >> 10);                  // 0x2b49..0x2ba1: 16->14,8->13,4->12,2->11,else 10
    if (exp == -1) _Assert("...", "cache.hpp", 143);          // 0x2bac bnei a3,-1 ; ... 0x2bc1 call 0xa304

    // (4) program the HW iram-ctrl block-size mask CSR (pairs with hw_decode.control).
    uint32_t cw = rsr_WindowBase();                           // 0x2be9 rsr.wb
    cw = (cw & 3) | (((1u << exp) - 1) << 2);                 // 0x2bf5 and 3 ; ssl/sll/-1 ; 0x2c04 slli 2 ; or
    *(volatile uint32_t *)IRAM_CTRL_BLOCK_SIZE_CSR = cw;      // 0x2c0e s32i a4, a3, 0  (CSR 0x4000[18:2])

    // (5) HW(0) / SW(1) cache-mode flag, from BEGIN arg bit0.
    cache->mode_sw = begin_arg & 1;                           // 0x2c10 l8ui+extui ; 0x2c16 s8i +60
    if (!cache->mode_sw) {                                    // 0x2c1c bbsi +60,0 -> skip HW alloc
        // (6) HW-cache: allocate + zero the DRAM tag/valid array, 16 bytes per line.
        cache->tag_base = alloc(cache->num_blocks << 4);      // 0x2c24 slli 4 ; 0x2c27 call 0x18a64 ; 0x2c2a s32i +64
        map_zero(cache->tag_base, cache->num_blocks << 4);    // 0x2c37 call 0x1a084
    }
    final_init(cache);                                        // 0x2c40 call 0x3ba8
}
```

> **QUIRK — the `(1<<15) % block_size` test is the power-of-two enforcement, indirectly.**
> Because the region is the fixed power-of-two `32768`, requiring `32768 % block_size == 0` is
> equivalent to requiring `block_size` to be a power-of-two divisor of 32768. The firmware then
> *also* derives the explicit `log2` exponent (`0x2b56..0x2ba1`) for the HW mask CSR, and
> asserts that exponent is valid (`cache.hpp:143`). Two redundant checks for one invariant —
> the second exists because the HW CSR needs the exponent, not the size. `[HIGH/OBSERVED]`

The geometry stores land at these struct offsets (full map in [§12](#12-cache-descriptor--global-state-map)):
`num_blocks → +24`, `line_shift(block_size>>6) → +16`, `line_count → +20`, `mode_sw → +60`,
`tag_base → +64`.

---

## 4. Tag lookup — the fully-associative scan  `query @0x5edc`  `[HIGH/OBSERVED]`

`fetch_cache_line @0x5a98` is the hit/miss entry point. It calls the query body and logs the
result; the query body branches on the HW/SW mode flag and, in SW mode, runs a **linear scan
over every tag entry** — a fully-associative search.

```c
// fetch_cache_line  @0x5a98  ("fetch_cache_line", asserts cache.hpp:189 @0x5b03 movi a12,189)
int fetch_cache_line(cache_t *cache, addr_t pc) {
    bool hit = query(cache, pc);                              // 0x5b26: call8 0x5edc -> a10 = hit
    if (!hit) {                                               // 0x5b29: beqz a10, 0x5b45
        LOG("S: I$ Miss");                                    // 0x5b4b: const16 0x1421
        return MISS;                                          //   -> replace() + fill()
    }
    LOG("S: I$ Hit, iram_idx=0x%x", cache_idx /* [a1+8] */);  // 0x5b32 l32i +8 ; 0x5b37 const16 0x1407
    return cache_idx;
}

// query body  @0x5edc  (logs "query addr/tag/vld" 0x5f68, "query addr/tag" 0x5fc9)
bool query(cache_t *cache, addr_t pc) {
    addr_t tag = block_align(pc);                             // block-aligned address = the tag
    if (cache->mode_sw) {                                     // 0x5ef6: bbsi +60 -> SW path
        // ---- SW fully-associative linear tag scan ----
        uint32_t idx = *cache->scan_index; /* [a1+20]->[0] */ // 0x5fdd l32i.n ; 0x5fdf l32i.n
        for (;;) {
            if (idx >= cache->num_blocks) {                   // 0x5fe1 l32i +24 ; 0x5fe3 bgeu -> MISS
                cache->result = 0;                            // 0x605d s8i 0, +44
                return false;
            }
            line_t *e = cache->tag_base + (idx << 4);         // 0x5fe9 l32i +64 ; 0x5ff0 slli 4 ; 0x5ff3 add
            uint64_t etag = LOAD64(e->tag);                   //   (two l32i halves)
            if (((etag_lo ^ tag_lo) | (etag_hi ^ tag_hi)) != 0) { // 0x600a xor ; 0x600d xor ; 0x6010 or
                idx = (*cache->scan_index += 1);              // 0x6050 l32i ; 0x6054 addi 1 ; 0x6056 s32i
                continue;                                     // 0x6058: j 0x5fdd  (next entry)
            }
            // match: confirm the line is non-zero (allocated), then mark HIT.
            assert(e->state != 0 /* cache.hpp:0x1b8 */);      //   (reload entry[0], assert valid)
            cache->result = 1;                                // 0x6047: s8i 1, +44
            return true;
        }
    } else {
        // ---- HW-cache path: the HW decoder owns the tag array; read its reported idx + vld.
        // (0x5f00-0x5f60 partially FLIX-desyncs; the descr[+60] split + the two-log structure
        //  ("query addr/tag/vld" vs "query addr/tag") are unambiguous.)  [SW HIGH, HW body MED]
        return hw_reported_hit(cache);
    }
}
```

The result is a single byte at `descr[+44]` (`0` = miss, `1` = hit), read back by the caller at
`0x6063: l8ui a2, a1, 44`.

> **QUIRK — fully associative, but a *persistent* scan cursor.** The scan starts from
> `*scan_index` (a running counter at `descr[+20]→[0]`), not from 0, and advances it on every
> mismatch — so the linear search is effectively a *rotating* probe over the tag array rather
> than a fresh 0..N scan each lookup. Combined with the round-robin victim ([§6](#6-replacement--eviction-round-robin)),
> the recently-filled lines tend to sit near the cursor. `[HIGH/OBSERVED on the cursor read +
> increment; the rotation-vs-LRU benefit is INFERRED.]`

---

## 5. The per-line state machine  `wait_for_valid @0x67f4`  `[HIGH/OBSERVED]`

One 16-byte descriptor layout serves both modes; `descr[+60]` selects which. `wait_for_valid`
blocks until a line's fill completes. The line-state word (`entry[0]`) takes exactly three
values, written and compared verbatim across query / replace / fill / wait.

```c
// wait_for_valid  @0x67f4
void wait_for_valid(cache_t *cache, uint32_t iram_idx) {
    if (!cache->mode_sw) {                                    // 0x6800: bbci +60,0 -> SW branch @0x6864
        // ---- HW cache ----
        uint32_t state = cache->hw_state; /* [a1+8] */
        LOG("S: wait_for_valid: (HW cache) iram_idx=%x, state=0x%x", iram_idx, state); // 0x681d const16 0x17e7
        if (state == 1)                                       // 0x6828: bnei a3,1 -> skip
            wait_for_dma_fill(cache);                         // 0x6830: call8 0x61a8
        assert(state == 2 /* cache.hpp:0x1cc */);             // 0x6841 beqi a2,2 ; 0x6853 movi a12,0x1cc ; call 0xa304
    } else {
        // ---- SW cache ----
        line_t *e = cache->tag_base + (iram_idx << 4);        // 0x686a l32i +64 ; 0x686d slli 4 ; add
        uint32_t state = e->state;                            // 0x6873 l32i.n a12, e, 0
        LOG("S: wait_for_valid: (SW cache) iram_idx=%x, state=0x%x", iram_idx, state); // 0x6878 const16 0x182d
        if (e->state == 1)                                    // 0x688d: bnei a3,1
            wait_for_dma_fill(cache);                         // 0x6895: call8 0x61a8
        assert(e->state == 2 /* cache.hpp:0x1d4 */);          // 0x68a9 beqi a2,2 ; 0x68bb movi a12,0x1d4 ; call 0xa304
    }
}
```

**Line state word `entry[0]`:**

| value | meaning | written where |
|---|---|---|
| `0` | unallocated / invalid (the zeroed init state) | `map_zero @0x1a084` (cold init) |
| `1` | FILL IN FLIGHT (DMA submitted, not complete) | `replace` SW-finalize `@0x5df4`; HW finalize |
| `2` | VALID / READY (line resident, executable) | `start_fill_siram` HW path `@0x620d`; fill-done |

The `1` and `2` constants are not abstractions — they are literal `movi.n`/`beqi` operands in
the disassembly, and the wait path *asserts* state `2` after the fill, which is how the page
proves the state machine is `0 → 1 → 2`. `[HIGH/OBSERVED]`

---

## 6. Replacement / eviction — round-robin  `replace @0x6068`  `[HIGH/OBSERVED]`

On a miss the firmware picks a victim line. The policy is a **single circular pointer that
advances by one (mod num_blocks)** — round-robin / FIFO, decisively **not** LRU.

```c
// replace  @0x6068  (logs orig-victim 0x147f, collision 0x149d, updated-victim 0x14d6)
void replace(cache_t *cache) {
    uint32_t orig = cache->victim; /* [a2+36] */              // 0x6080: l32i.n a11, a2, 36
    LOG("S: replace: orig victim=0x%x", orig);                // 0x6085: const16 0x147f
    uint32_t v = victim_rotate(cache);                        // 0x6092: call8 0x6194 -> 0x62e0
    wait_for_dma_fill(cache);                                 // 0x6097: call8 0x61a8  (kick the new fill)
    if (!cache->mode_sw) { /* HW finalize @0x60a3 */ }
    while (collides_with_live_line(v)) {                      // 0x60a9..0x60d3 collision guard
        LOG("S: replace: collision on cache idx=0x%x, updating again", v); // const16 0x149d
        v = victim_rotate(cache);                             // rotate again, retry
    }
    if (cache->mode_sw) {                                     // SW finalize @0x611a
        line_t *e = cache->tag_base + (v << 4);
        e->state = 1;                                         // mark FILL-IN-FLIGHT (invalidates prior occupant)
        LOG("S: replace: idx(updated victim_idx)=0x%x, addr=0x%llx", v, addr); // const16 0x14d6
    }
}

// victim_rotate  @0x6194 -> @0x62e0  (the policy primitive)
uint32_t victim_rotate(cache_t *cache) {
    uint32_t v = cache->victim + 1;                           // 0x62e7 l32i +36 ; 0x62e9 addi 1
    if (v == cache->num_blocks)                               // 0x62ef l32i +24 ; 0x62f1 bne
        v = 0;                                                // 0x62f7 movi 0   (WRAP)
    return v;                                                 // 0x6300 retw.n   (caller stores to +36)
}
```

> **CORRECTION — the rotate returns the new victim; the caller stores it.** A naive reading of
> the report text "victim-rotate ... `descr[+36] = victim`" suggests the *primitive* writes the
> victim back. The disassembly shows `victim_rotate @0x62e0` only **computes and returns**
> `v` in `a2` (`0x6300: retw.n`); the write-back to `descr[+36]` happens in the caller
> (`replace`, and the fetch-time driver at `0x5d14`). The distinction matters for a reimplementer:
> the primitive is pure-ish, the state lives one frame up. `[HIGH/OBSERVED this pass.]`

The collision guard layered on top — both inside `replace()` and in the fetch front-end's
"rotate to next victim and retry" ([§9](#9-fetch-front-end-interaction)) — is what keeps the
round-robin from ever choosing the line currently being executed from.

---

## 7. Fill / refill — DMA from the backing `'S:'` stream  `[HIGH/OBSERVED]`

A miss pulls one `block_size` line from the backing instruction stream by DMA. There are **two
backends**, selected by the global byte at DRAM `[0x85658]` (`0` = HW iram-DMA, `1` = SW DRAM
ring). The fill address is computed linearly: `fetch_addr = idx * block_size`.

```c
// start_fill_siram  @0x6217  (logs "start_fill_siram: fetch_addr=0x%llx" 0x152d)
void start_fill_siram(cache_t *cache, uint32_t idx) {
    uint64_t fetch_addr = (uint64_t)idx * g_block_size;       // 0x6231 const16 0x5654 ; 0x6239 mull
    LOG("S: start_fill_siram: fetch_addr=0x%llx", fetch_addr); // 0x6252: const16 0x152d

    if (g_dma_backend /* [0x85658] */) {                      // 0x6260 const16 0x5658 ; 0x6266 bbci 0 -> HW @0x62ac
        // ---- backend B: SW DRAM ring ----
        ring_slot_t s = DramRingDMA_enqueue(/*src=*/stream + fetch_addr,
                                            /*dst=*/iram_cache_line(idx),
                                            /*len=*/g_block_size);  // 0x629a: call8 0x6354
        cache->dma_submit = s.submit;                         // 0x62a7: s32i +48
        cache->dma_comp   = s.comp;                           // 0x62a3: s32i +52
    } else {
        // ---- backend A: HW iram-DMA via the "general" local-reg bundle (glr[9..12]) ----
        REG(0x1140) = cache->src_desc;  /* [a1+16] */         // 0x62b1 const16 0x1140 ; s32i +0   glr[10]
        REG(0x1160) = cache->src_hi;    /* [a1+20] */         // 0x62b8 (a4=0x1140, +0x20) s32i +32 glr[11]
        REG(0x1180) = cache->dst_params;/* [a1+4]  */         // 0x62c5 const16 0x1180 ; s32i +0   glr[12]
        REG(0x1120) = 1;                /* KICK */            // 0x62cd const16 0x1120 ; movi 1 ; s32i  glr[9]
    }
    cache->pending_dma = idx;                                 // 0x62d7: s32i +32
}
```

> **NOTE — the HW iram-DMA reuses the general local-register bundle as a descriptor.** CSRs
> `0x1120/0x1140/0x1160/0x1180` are `glr[9..12]` of the "general" local-register block (base
> `0x1000`, stride `0x20`; see the [tpb_xt_local_reg CSR map](../../uarch/lsu-memory.md)). The
> firmware repurposes four general LRs as the iram-DMA descriptor (src/desc, src-hi/count,
> dst/params, then a `1`-write KICK), exactly the way the Setup-Halt resume path repurposes
> `glr 0x1060/0x1080` for the resume PC. This is the **direct touchpoint** with the
> [iDMA / Legacy DMA](../dge/idma-legacy-dma.md) engine: the cache-fill HW path *is* an
> iram-DMA descriptor poke. `[HIGH/OBSERVED on the four CSR writes + KICK.]`

```c
// DramRingDMA enqueue  @0x6354  (logs "DMA enqueue 0x%llx[%u] -> 0x%llx" 0x1578,
//                                "DramRingDMA::allocate(...) submit/comp/first_slot/min_comp")
// Allocates a ring slot, pushes a descriptor moving block_size bytes src(stream)->dst(IRAM line).
// submit/comp slot ids recorded in descr[+48]/[+52] for the completion poll.   [enqueue HIGH;
// exact ring-descriptor field encoding is partial FLIX, MED]

// wait_for_dma_fill  @0x61a8  (logs "wait_for_dma_fill: idx=0x%x" 0x150d)
void wait_for_dma_fill(cache_t *cache) {
    uint32_t idx = cache->pending_dma; /* [a1+32] */          // 0x61b2: l32i +32
    LOG("S: wait_for_dma_fill: idx=0x%x", idx);               // 0x61b7: const16
    while (!fill_done(cache)) { /* call8 0x3b4c */ }           // 0x61c8: spin, bnez retry
    line_t *e = cache->tag_base + (idx << 4);                 // 0x6201 l32i +64 ; 0x6204 l32i +32 ; 0x6206 slli 4
    e->state = 2;                                             // 0x620b movi 2 ; 0x620d s32i e,0  (VALID)
}

// fill_done poll  @0x3b4c  (branches on the backend select [0x85658])
bool fill_done(cache_t *cache) {
    if (g_dma_backend) {                                      // 0x3b56/0x3b5c: const16 0x5658
        return ring_complete(cache->dma_submit, cache->dma_comp); // 0x3b62 -> call 0x3c04
    } else {
        return REG(0x1120) == 0;  /* busy != 0 */             // 0x3b8e/0x3b91: const16 0x1120, read-back
    }
}
```

> **CORRECTION — the VALID store is at `0x620d`, not `0x620b`.** The state-`2` write in the
> HW-fill completion is `movi.n a3, 2` at `0x620b` followed by `s32i.n a3, a2, 0` at `0x620d`.
> Cite the *store* address (`0x620d`) when anchoring "line becomes valid". `[HIGH/OBSERVED.]`

One DMA fill moves exactly **one `block_size` line**; completion is polled (no interrupt
rendezvous in the cache manager itself), and the line state flips `1 → 2`. The
[iDMA / Legacy DMA](../dge/idma-legacy-dma.md) page documents the descriptor format and
completion semantics of both backends in full.

---

## 8. Fetch-time line acquisition  `wait_for_cache_line @0x5cd0`  `[HIGH/OBSERVED]`

This is the per-fetch driver that turns a PC into a resident line **and pre-stages the next
sequential line** — a built-in `+1`-stride prefetch on top of the branch-hint prefetch
([§10](#10-prefetch--branch-hint-driven)).

```c
// wait_for_cache_line  @0x5cd0  (logs "wait_for_cache_line: pc, mask, blk_sz, idx, nxt_idx" 0x5e28,
//                                out-of-bounds warning 0x5daf)
void wait_for_cache_line(cache_t *cache, addr_t pc) {
    uint32_t idx     = (translate(pc) & iram_block_size_mask) / g_block_size;  // (pc & mask)/block_size
    uint32_t nxt_idx = idx + 1;

    wait_for_valid(cache, idx);                               // 0x5d0f: call8 0x67f4 (current line)
    cache->victim = idx;                                      // 0x5d14: s32i.n a3, a2, 36 (current idx)

    addr_t next_addr = pc + g_block_size;                     // 0x5d1d..: const16 0x5654, add
    if (is_pc_in_bounds(next_addr)) {                         // 0x696d: const16 0x1864 bounds check
        replace(cache /* for next line */);                  // 0x5dee: call8 0x6068
        cache->next_staged = 1;                              // 0x5df4 movi 1 ; 0x5df6 s8i a3,a2,40  (BYTE flag)
    } else {
        LOG("S: WARNING: ... next_addr out-of-bounds [lo,hi], skipping it."); // 0x5daf
    }
}
```

> **CORRECTION — `descr[+40]` ("next line staged") is a *byte* flag, not a word pointer.** The
> store is `s8i a3, a2, 40` (`0x5df6`) with `a3 = 1`, not a 32-bit pointer write. Treat
> `descr[+40]` as a boolean "next line has been staged" flag. `[HIGH/OBSERVED this pass.]`

The index derivation `(pc & iram_block_size_mask) / block_size` is read from the log's own
argument list (`mask`, `blk_sz`, `idx`, `nxt_idx`). The exact mask-arithmetic *word* sits in a
partially FLIX-desynced span (`0x5d6d-0x5e23`), so the precise shift constants are MED, but the
`(pc & mask)/block_size` model is HIGH from the log signature. The out-of-range guard
(`is_pc_in_bounds @0x696d`) prevents staging a next line past the program's high bound — see
[PC-Bounds Enforcement](pc-bounds.md).

---

## 9. Fetch front-end interaction  `fetch.hpp check_fetch / RTL_PC_check`  `[head HIGH / body MED]`

The fetch front-end that consumes this cache is the dual-fetch FSM (the HW-decode FSM `@0x31ac`
and the Sunda software-fetch FSM `@0x2d81`; see [HW-Decode vs Sunda Dual Fetch](dual-fetch.md)
and [Fetch + PC-Redirect](fetch-pc-redirect.md)). The cache↔fetch glue lives in `check_fetch`
(`fetch.hpp`, asserted `@0x33c8` line `0x189`), embedded in the `0x31ac` body. The surrounding
bundle scheduling is the FLIX/IVP desync span, but **the cache call sites and their log anchors
are instruction-exact**:

- **`RTL_PC_check_delta`** (`0x326e` log): tracks `curr_dma_idx_vld`, `tgt_cache_idx`,
  `curr_pc` — the coherence point between the **RTL fetch PC** (what the HW decoder is
  currently fetching) and the firmware's **target** SW cache index.
- **`Evict?`** (`0x3387` log): reads `ok_to_evict` / `curr_dma_idx_vld` for the proposed
  `next_dma_cache_idx` (via the state reader `0x6f24`). If `next_dma_cache_idx ==
  curr_cache_line_idx`, it logs "rotate to next victim and retry" (`0x33ef`), rotates the
  round-robin victim (`0x6194 → 0x62e0`), re-evaluates, and logs `Retry:` (`0x342b`). **The
  front-end refuses to evict the line it is currently executing from.**
- **`Push?` / `Pushing`** (`0x3595` / `0x35e8` logs): when `next_vld` is set and
  `next_cache_idx` is not yet in the `cache_idx_pushed` bitmask (`descr[+56]`), it **pushes**
  the idx (feeds the HW decoder the pre-resolved next line) and sets the bit via `0x5b84`:
  `descr[+56] |= (1 << idx)`.

```c
// cache_idx_pushed bitmask set  @0x5b84   (instruction-exact this pass)
void mark_pushed(cache_t *cache, uint32_t idx) {
    uint32_t bit = 1u << idx;            // 0x5b8f movi 1 ; 0x5b91 ssl a3 ; 0x5b94 sll a3,a4
    cache->pushed |= bit;               // 0x5b97 l32i +56 ; 0x5b99 or ; 0x5b9c s32i a3,a2,56
}
```

```
  cache <-> fetch dataflow:

  FSM fetch (PC) ─► wait_for_cache_line(pc)
                      ├─ query(tag) ── hit ─► wait_for_valid ─► execute line
                      │             ── miss ─► replace(round-robin victim)
                      │                          └─ start_fill_siram (DMA) ─┐
                      │                             wait_for_dma_fill ◄──────┘ poll, state 1→2
                      └─ stage next line (idx+1) + (branch-hint) Push? next idx
  check_fetch / RTL_PC_check_delta keeps the RTL decode PC and the SW cache index in
  agreement; Evict? guards against evicting the live line.
```

The Sunda redirect path (`sunda_fetch`, `@0x2de0` "redirecting to iram_pc=0x%x", `@0x2e2f`
"pending_redirect=%d") recomputes a software PC redirect into a cache-line-relative `iram_pc`
and drives the same query→miss→replace→fill chain. `[head + cache-call sites HIGH/OBSERVED;
the 0x31ac dispatch body / loop back-edge are the FLIX desync span — per-iteration ordering MED.]`

---

## 10. Prefetch — branch-hint driven  `branch_prefetch_hint.cpp`  `[HIGH]`  *(SX-FW-05 touchpoint)*

The prefetch subsystem is the **producer** that feeds the `Push?` path. Its full state machine
is the subject of [SEQ Branch + Prefetch-Hint](branch-prefetch.md); the cache-relevant
touchpoints are:

- **`BranchPrefetchHint @0xcfe8`** (`"S: BranchPrefetchHint"` `@0xcff4`): the
  `resolve_hint_decision` entry.
- **`PrefetchHelper @0x151e8`**:
  - "Arming branch_hint with {br_addr, br_target, decision}" (`0x3cf4`) — records a pending hint.
  - "Taken branch addr is in next cache block range [lo,hi], prefetching branch target
    0x%llx" (`0x3c49`) — when a taken branch's target falls in the **next** cache block, it
    calls `0x15010` (cache prefetch/push) to pre-stage that block.
  - "Branch is marked NT, invalidating branch_hint to preserve legacy behavior" (`0x3d42`) and
    "disarming branch_hint" (`0x3d90`) — hint teardown.
  - "PrefetchHelper : Invalidating all hints" (`0x3cc8`) — bulk hint invalidate.

So branch prefetch = compute the branch target's cache block, and if it is the next block,
**push that line into the cache ahead of the fetch** by setting the `cache_idx_pushed` bitmask
(`descr[+56]`). `[touchpoint + arm/prefetch/disarm/invalidate state machine HIGH/OBSERVED; the
exact `0x15010` push descriptor build is deferred to the branch-prefetch page.]`

---

## 11. Invalidation / coherence  `[HIGH unless noted]`

- **Cold init:** `map_zero @0x1a084` zeroes the entire `num_blocks × 16` tag/valid array — all
  line states → `0`. `[HIGH/OBSERVED]`
- **Per-line invalidate-on-refill:** `replace()` sets the new victim's `entry[0] = 1`
  (fill-in-flight) *before* the DMA, which invalidates the prior occupant of that slot (its tag
  is overwritten by the fill). `[HIGH/OBSERVED]`
- **Hint invalidation on divergence:** "Invalidating all hints" / "invalidating branch_hint to
  preserve legacy behavior" clear the prefetch hints when control flow leaves the hinted path.
  `[HIGH/OBSERVED]`
- **HW iram-ctrl flush:** `hw_decode.control.iram_ctrl_flush_en` (CSR `0x4000[20]`) is **set at
  boot in Sunda mode** (`BEGIN @0x241a`) — the HW-side IRAM-ctrl flush that pairs with the
  software cache when the HW decode path is bypassed. `[bit-set-in-Sunda HIGH/OBSERVED; precise
  RTL flush semantics INFERRED, MED.]`

> **QUIRK — the instruction cache is read-only; host updates need an eviction.** No `s32i` from
> the cache manager writes *back* to the backing stream (it is a read-only I-cache). A host
> update to the instruction stream becomes visible only after the affected line is evicted and
> re-filled — and round-robin guarantees eventual eviction. There is **no explicit
> "host-changed-the-stream, invalidate" doorbell inside `cache.hpp`**; the redirect / StartCtrl
> handshake ([Run-State Machine](run-state.md)) is where a fresh program is picked up. `[read-only
> HIGH/OBSERVED; the "no host-invalidate doorbell" is an ABSENCE claim over the recovered spans,
> partly movi-built — MED.]`

---

## 12. Cache descriptor / global-state map  `[HIGH unless noted]`

Cache control struct, base `a2 = &state[0x855e0 + 24]`:

| off | field | written / read at |
|---|---|---|
| `+16` | `block_size >> 6` (line-index shift) | init `0x2bd4` |
| `+20` | derived line count (also the SW-scan cursor anchor) | init `0x2bdd` / scan `0x5fdd` |
| `+24` | `num_blocks` | init `0x2af3` |
| `+32` | pending DMA index/slot | fill `0x62d7` / wait `0x61b2` |
| `+36` | round-robin victim pointer | victim `0x62e0` / store `0x5d14`,`0x6080` |
| `+40` | "next line staged" **byte** flag | `wait_for_cache_line` `0x5df6` (`s8i`) |
| `+44` | query hit/miss result **byte** | query `0x6047`/`0x605d` / read `0x6063` |
| `+48` | DMA submit slot (SW ring) | fill `0x62a7` |
| `+52` | DMA comp slot (SW ring) | fill `0x62a3` / poll `0x3b68` |
| `+56` | `cache_idx_pushed` bitmask (prefetch) | `0x5b84` (`\|= 1<<idx`) / `Push?` |
| `+60` | HW(0)/SW(1) cache-mode **byte** flag | init `0x2c16` ; switched everywhere |
| `+64` | tag/valid array base ptr (DRAM) | init `0x2c2a` |

Per-line descriptor (16 bytes, `tag_base + idx*16`):

| off | field | conf |
|---|---|---|
| `[0]` | state/valid word: `0`=invalid, `1`=fill-in-flight, `2`=valid/ready | `[HIGH/OBSERVED]` |
| `[4..15]` | tag (block-aligned addr, compared 64-bit) + aux | `[MED layout/INFERRED]` |

DRAM globals:

| addr | global | conf |
|---|---|---|
| `[0x85654]` | `block_size` (cached from CSR `0x1260` in BEGIN `@0x2814`) — **20** refs (`rg -c`) | `[HIGH/OBSERVED]` |
| `[0x85658]` | DMA-backend select: `0`=HW iram-DMA, `1`=SW DramRing — **4** refs | `[HIGH/OBSERVED]` |
| `[0x855e0]` | central SEQ state struct (`+24` = cache struct) — **61** refs | `[HIGH/OBSERVED]` |

CSR cross-reference (general local-register block, base `0x1000`, stride `0x20`):

| CSR | role | conf |
|---|---|---|
| `0x1120` glr[9] | HW iram-DMA KICK (write 1; read 0 = done) | `[HIGH/OBSERVED]` |
| `0x1140` glr[10] | HW iram-DMA descriptor word 0 (src/desc) | `[HIGH/OBSERVED]` |
| `0x1160` glr[11] | HW iram-DMA descriptor word 1 | `[HIGH/OBSERVED]` |
| `0x1180` glr[12] | HW iram-DMA descriptor word 2 (dst/params) | `[HIGH/OBSERVED]` |
| `0x1260` glr[19] | `block_size` source config CSR (read in BEGIN `@0x280c`) | `[HIGH/OBSERVED]` |
| `0x4000[18:2]` | `hw_decode.control.iram_block_size_mask` (reset `0x1FFF`) | `[HIGH/OBSERVED]` |
| `0x4000[20]` | `hw_decode.control.iram_ctrl_flush_en` (set in Sunda `@0x241a`) | `[HIGH/OBSERVED]` |

> **NOTE — CSR aperture-base alias.** The `glr` identities by low offset are HIGH; the absolute
> aperture base (`0x1000` vs a `0x401000` alias) carries the boot-page ambiguity flagged on the
> [Boot / Entry](boot.md) page and does not change the register identity. The `0x4000[*]`
> `hw_decode.control` fields likewise carry the `0x0`-vs-`0x400000` base ambiguity.

---

## 13. Confirmed call chain

```
  BEGIN(0x2378) ─► 0x2a84  IRAM cache init (geometry, alloc tag array, program mask CSR)
  FSM fetch     ─► 0x5cd0  wait_for_cache_line(pc)
                     ├─► 0x5a98 fetch_cache_line ─► 0x5edc query (tag scan) ─► hit/miss
                     ├─► 0x67f4 wait_for_valid (HW/SW) ─► (state 1) 0x61a8 wait_for_dma_fill
                     └─► 0x6068 replace (next line)
  replace(0x6068) ─► 0x6194 ─► 0x62e0 victim rotate (round-robin)
                   ─► 0x61a8 wait_for_dma_fill ─► fill kick
  fill            ─► 0x6217 start_fill_siram ─► 0x6354 DramRingDMA enqueue (SW)
                                             └─ CSR 0x1120/40/60/80 KICK (HW)
  fill-done poll  ─► 0x3b4c ─► 0x3c04 (ring)  |  CSR 0x1120 read-back (HW)
  front-end glue  ─► 0x31ac check_fetch / RTL_PC_check_delta / Evict? / Push?
  prefetch        ─► 0x151e8 PrefetchHelper ─► 0x15010 push next block ─► descr[+56]
```

`[HIGH/OBSERVED — every hop re-disassembled this pass.]`

---

## 14. Desync spans / honest gaps

- The cache **manager** functions (`0x2a84` init, `0x5a98`/`0x5edc` query, `0x6068` replace,
  `0x6194`/`0x62e0` victim, `0x61a8` `wait_for_dma_fill`, `0x6217` `start_fill_siram`, `0x6354`
  enqueue, `0x67f4` `wait_for_valid`, `0x3b4c` poll) **decode cleanly** in their core logic —
  scalar windowed-ABI code with only short literal-pool `.byte` runs between functions. All
  state writes, the `1`/`2` constants, the `num_blocks` / 16-byte-stride / round-robin
  arithmetic, the XOR tag compare, and the CSR writes are instruction-exact. `[HIGH/OBSERVED]`
- The fetch front-end body (`0x31ac check_fetch`) is the FLIX/IVP desync span. The cache **call
  sites** and their log anchors (`Evict?`/`Retry`/`Push?`/`RTL_PC_check`) are exact; the
  surrounding bundle scheduling and the loop back-edge are not. `[head HIGH, body MED]`
- The HW-cache branch of `query` (`0x5f00-0x5f60`) partially desyncs; the SW associative scan
  is the instruction-exact reference. The `descr[+60]` HW/SW split is unambiguous (two log lines
  + the `bbci` dispatch). `[split HIGH, HW-path body MED]`
- The 16-byte descriptor's interior field split beyond `word[0]=state` is INFERRED (the tag is
  read as opaque `l32i` halves in the scan). `[MED]`
- The resident-vs-cached BYTE BOUNDARY inside the 114-KiB image is computed at runtime from
  `block_size`, not baked in; "the high vector body is the cached stream" is INFERRED from the
  image>aperture fact + the fill machinery. `[MED]`
- "No host-invalidate doorbell in `cache.hpp`" is an ABSENCE claim over the recovered spans; the
  redirect path is partly `movi`-built / desynced. `[MED]`

---

## 15. DEBUG vs PERF

Everything on this page is offset-anchored to the **DEBUG** carve, because DEBUG keeps the
`'S:'` strings that name every function and argument. A reimplementer targeting the **PERF**
build must account for two differences:

1. **Strings gone.** The `const16` pairs that load the `'S:'` format strings, and the
   `LOG(0x18b84)` calls that consume them, are removed in PERF. This eliminates the
   string-oracle: a PERF disassembly cannot self-name its functions from log text.
2. **Code re-laid-out.** Removing the string loads changes instruction scheduling and shifts
   every IRAM offset. The function *boundaries* and the *algorithm* (32-KiB region, runtime
   power-of-two `block_size`, `num_blocks = 32768/block_size`, 16-byte descriptor, round-robin
   victim, state `1→2`, dual DMA backend) are invariant; the *addresses* are not. Match PERF
   functions by structure (the `(1<<15)` literal in init, the `glr[9]` KICK in fill, the
   `mod num_blocks` wrap in victim-rotate), not by offset.

> **GOTCHA — the assert line numbers are stable across builds, the offsets are not.** The
> `_Assert(..., "cache.hpp", N)` line numbers (`97`, `143`, `0x1b8`, `0x1cc`, `0x1d4`, `189`)
> come from the *source*, so they survive PERF stripping even though the strings around them do
> not (the line constant is a plain `movi a12, N`). Use them to re-anchor functions in a PERF
> build. `[HIGH/OBSERVED]`

---

## 16. Adversarial self-verification — the 5 strongest claims

Each of the five primary claims was re-challenged against the disassembly this pass; where a
naive reading would have failed, the failure and its fix are recorded.

| # | claim | challenge | binary arbiter / verdict |
|---|---|---|---|
| 1 | **IRAM image = 0x1c820 = 114 KiB > 64-KiB aperture** | Is the size the section size or a padded blob? Is the aperture really 64 KiB? | `objdump -h` on the carved IRAM object: `.rodata` size = `0x1c820`; `objcopy` to binary → `stat` = `116768` = `0x1c820`. `ncore2gp` `InstRAMInfo = [0x10000 0x0]` = 64 KiB. Head bytes `06 76 00 00` = `j 0x1dc` (reset vector). **VERDICT: STANDS — `[HIGH/OBSERVED]`. The 114-KiB-over-64-KiB mismatch is the cache's reason to exist.** |
| 2 | **Software overlay with round-robin victim** | Could `victim_rotate` actually be LRU/PLRU, or write its own state back? | `0x62e7 l32i +36 ; 0x62e9 addi 1 ; 0x62ef l32i +24 ; 0x62f1 bne ; 0x62f7 movi 0` — `victim+1`, compare-to-`num_blocks`, wrap-to-0. No timestamp, no tree. **CORRECTION applied:** the primitive *returns* `v` in `a2` (`0x6300 retw.n`); the caller stores it. **VERDICT: STANDS (corrected) — round-robin, caller-stored — `[HIGH/OBSERVED]`.** |
| 3 | **Cache-fill triggered on miss, one block_size line by DMA** | Is the fill really one line, and is state really `1→2`? | `start_fill_siram @0x6231`: `fetch_addr = idx * block_size` (`mull`). `wait_for_dma_fill @0x6201-0x620d`: on completion `movi 2 ; s32i e,0`. `replace` SW-finalize `@0x5df4`: `movi 1 ; s8i e,0`. **CORRECTION applied:** VALID store is at `0x620d` (movi at `0x620b`). **VERDICT: STANDS — one-line DMA fill, state `1→2`, addresses pinned — `[HIGH/OBSERVED]`.** |
| 4 | **iDMA relationship: HW path pokes general-LR CSRs 0x1120/40/60/80** | Are these really the iram-DMA CSRs and not something else? Is `0x1120` the KICK? | `0x62b1 const16 0x1140 ; 0x62c5 const16 0x1180 ; 0x62cd const16 0x1120 ; movi 1 ; s32i` (KICK = write 1 to `glr[9]`). `fill_done` reads `0x1120` and tests for `0`. The `0x1140 + 0x20` step matches the `glr` stride `0x20`. **VERDICT: STANDS — the HW fill path is an iram-DMA descriptor poke + KICK; read-back-0 completion — `[HIGH/OBSERVED]`.** |
| 5 | **Code-range bookkeeping: 16-byte descriptor, num_blocks, descr offsets** | Is the per-line stride really 16 B, and does init really store `num_blocks` at `+24` and `tag_base` at `+64`? | init: `0x2af0 quou (1<<15)/block_size ; 0x2af3 s32i +24` (num_blocks); `0x2c24 slli a10,a3,4` (×16) `; 0x2c27 call alloc ; 0x2c2a s32i +64` (tag_base). Query/wait/replace all address entries as `tag_base + (idx<<4)`. **VERDICT: STANDS — 16-byte stride, `num_blocks→+24`, `tag_base→+64`, all instruction-exact — `[HIGH/OBSERVED]`.** |

All five stand. Two in-place corrections were folded into the body (victim-rotate return-vs-store
in [§6](#6-replacement--eviction-round-robin); VALID-store address + the `s8i` byte stores for
`descr[+40]`/`descr[+44]`/`descr[+60]` in [§7](#7-fill--refill--dma-from-the-backing-s-stream)
and [§8](#8-fetch-time-line-acquisition-wait_for_cache_line-0x5cd0)). No claim was overturned.

---

## See also

- [iDMA / Legacy DMA (IRAM cache-fill)](../dge/idma-legacy-dma.md) — the descriptor format and
  completion semantics of both fill backends ([§7](#7-fill--refill--dma-from-the-backing-s-stream)).
- [SEQ Branch + Prefetch-Hint](branch-prefetch.md) — the prefetch producer that feeds the
  `Push?` path / `descr[+56]` bitmask ([§9](#9-fetch-front-end-interaction),
  [§10](#10-prefetch--branch-hint-driven)).
- [SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md) and
  [HW-Decode vs Sunda Dual Fetch](dual-fetch.md) — the fetch FSMs that consume the cache.
- [External-Lib Prelink Validation + NUM_POOL_CORES](../pool/prelink-validation.md) — the
  on-device prelinker / forward-link path that lays out the instruction stream this cache pages in.
- [SEQ Boot / Entry Path](boot.md) — `BEGIN`, where `block_size` is read and the cache is init'd.
- [SEQ PC-Bounds Enforcement + Host API](pc-bounds.md) — `is_pc_in_bounds`, the next-line guard.
