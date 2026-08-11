# SEQ PC-Bounds Enforcement + Host API

This page is the **PC-range sandbox** of the GPSIMD **SEQ engine** — the device-side check that
decides whether a SEQ instruction-fetch address lies inside a host-armed legal `[lower, upper]`
SoC window, plus the host control API that stages that window into device DRAM. It is the
microcode-isolation surface for untrusted custom-op code: the mechanism a reimplementation must
reproduce to confine a custom op's reachable instruction stream to its own loaded library. It
decodes `is_pc_in_bounds @0x68d0` byte-exact, names its **exactly two** call sites, and pairs it
with the three exported host functions `nrtucode_core_enable_pc_bounds_check` /
`_disable_pc_bounds_check` / `_private_get_pc_bounds` that write the 64-bit `[lo,hi]` pair through
the per-core memhandle vtable into the device-DRAM control block at **+0x38 (lower)** and **+0x40
(upper)**.

This is the surface that closes the **NX-030 PC-bounds concern**. It sits at the front of the
same pipeline as its siblings: the surrounding poll/stop FSM is [SEQ Main FSM Loop](main-loop.md),
the demand-fetch / redirect machine is [SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md),
the branch/prefetch arming whose hint target this guard gates is
[SEQ Branch + Prefetch-Hint](branch-prefetch.md), the instruction-cache fill whose look-ahead line
this guard gates is [SEQ IRAM Instruction Cache / Overlay](iram-cache.md), and the hard
opcode-value fault (the *contrast* bound, §7) is [SEQ Error-Handler / Fault Reporting](error-handler.md).
The host side is mirrored, runtime-facing, in [The DGE Host-Private API](../../runtime/dge-host-api.md)
and [the nrtucode_core_t struct](../../runtime/nrtucode-core.md); the threat-model framing is
[Custom-Op Reachability / Isolation Model](../../control/security/reachability-isolation.md).

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte / string / config field / disassembler line read from a shipped artifact;
`INFERRED` = reasoned over OBSERVED facts (often across a FLIX/literal-pool
desync); `CARRIED` = consolidated from a cited cross-page anchor at its original confidence.
Crossed with `HIGH` / `MED` / `LOW`. Callouts: **QUIRK** (counter-intuitive but real),
**GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**
(orientation). The page default is `[HIGH/OBSERVED]`; claims that depart from it carry an
explicit tag.

> **NOTE — carve + host-lib provenance.** Every device fact below is grounded
> in a carve of the SEQ base firmware out of the static archive `libnrtucode.a`
> (gpsimd-customop package), members `img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}_contents.c.o`. The carve
> `objcopy -O binary --only-section=.rodata` yields **iram.bin = 116768 B (`0x1c820`), sha256
> `8e4412b9…ab9ed70a`** and **dram.bin = 28448 B (`0x6f20`), sha256 `7bdf6ed7…d6816ecd`**, with head
> bytes `06 76 00 00` = `j 0x1dc` (reset vector). Decode is the native `xtensa-elf-objdump` (GNU
> Binutils 2.34.20200201, Xtensa Tools 14.09) with `XTENSA_CORE=ncore2gp` — the **only** config that
> decodes SEQ FLIX correctly (the SEQ is Vision-Q7 FLIX/VLIW, `IsaMaxInstructionSize=32`, **not** the
> scalar-LX NCFW core; see [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md)).
> IRAM offset == device IRAM VA (reset vector at byte 0); **DRAM string offset =
> device DRAM VA − `0x80000`** (the DRAM image loads at VA `0x80000`). For the raw binary carve,
> `.text`/`.rodata` VMA == file offset by construction. The **host** facts are read from the shipped
> x86-64 host libraries with stock `objdump`/`nm`/`dd`: the runtime-lib `libnrtucode_extisa.so`
> (sha256 `dc00763d…c39159f`, the three PC-bounds funcs at `0xa860`/`0xa960`/`0xaa10`) and the
> gpsimd-customop `libnrtucode.so` (the same three at `0x3099a0`/`0x309aa0`/`0x309b50`). Host libs are
> identity-mapped (`.text`/`.rodata` VMA == file offset; **not** the `0x200000`-delta config-DLL kind);
> a vtable slot reached as `call *N(%rax)` is `vptr+N`. `[HIGH/OBSERVED]`

> **GOTCHA — DEBUG vs PERF offsets differ; this page anchors to DEBUG.** The control-flow algorithm is
> **identical** in the `POOL_PERF` build, but PERF strips every `call8 0x18b84` log call and re-lays-out
> the code, so **every interior device address below shifts in PERF**. The DEBUG image keeps the
> `'S:'` format strings as named xrefs, so every step has a string anchor. Do **not** look up a DEBUG
> address in a PERF image. `[HIGH/OBSERVED]`

---

## 0. The sandbox in one diagram

```
  HOST (x86-64, libnrtucode_extisa.so / libnrtucode.so)        DEVICE (Vision-Q7 SEQ, ncore2gp FLIX)
  ────────────────────────────────────────────────────        ─────────────────────────────────────
  nrtucode_core_enable_pc_bounds_check(core, lo, hi)
    if (lo >= hi)  -> NRTUCODE_ERR_INVALID (8) + log sev1       ┌─ per-core DEVICE-DRAM control block
    write_fn(ctx, dram_base + 0x38, 8, &lo) ───────────────────►│  +0x38  pc_bounds_soc_addr_lo (8B)
    write_fn(ctx, dram_base + 0x40, 8, &hi) ───────────────────►│  +0x40  pc_bounds_soc_addr_hi (8B)
    log sev4 "enabled PC bounds check for range [lo, hi]"       └─────────────┬──────────────────────
                                                                              │ surfaced on-device as
  nrtucode_core_disable_pc_bounds_check(core)                                  ▼ bounds global DRAM 0x82dc0
    write_fn(+0x38, &0x0) ; write_fn(+0x40, &0xFFFFFFFFFFFFFFFF)   GetSequenceBounds @0xd224 reads it
    => MAXIMAL window [0, 2^64)  (every PC in-bounds)              ──► stages {lo,hi} into caller frame

  nrtucode_core_private_get_pc_bounds(core, *lo, *hi)            is_pc_in_bounds @0x68d0 (bool):
    read_fn (slot 0)  +0x38 -> *lo ; +0x40 -> *hi                  return (pc >= lower && pc <= upper)
                                                                    │
  vtable:  ctx -> *ctx = RW vtable                                  ├─ caller 1: wait_for_cache_line @0x5d79
           slot +0x00 = READ   (get)                               │     next_addr = current_pc + block_size
           slot +0x08 = WRITE  (enable/disable)                    │     OOB -> log 0x8171c + SKIP fill   (SOFT)
                                                                    └─ caller 2: branch-prefetch hint @0x34de
                                                                          OOB -> log 0x81d51 + SKIP push   (SOFT)
```

One-line verdict: the host arms a validated half-open-on-the-host, inclusive-on-the-device 64-bit
`[lower, upper]` SoC window into device DRAM `+0x38`/`+0x40` via a memhandle write vtable; the device
`is_pc_in_bounds` tests `pc >= lower && pc <= upper` and is wired, in this shipped build, only to the
**two speculative look-ahead paths**, where an out-of-range result is a **soft warn-and-skip**, never a
hard fault — exactly the speculative-prefetch carve-out the shipped header promises.

> **QUIRK — the PC-bounds guard is a SOFT speculative guard, not a hard fetch fault (in this image).**
> The header (§6d) says an out-of-bounds PC "raises an error notification to runtime and goes in the
> error state," *with the one exception* that speculative-prefetch PCs do not interrupt execution. In
> the shipped SEQ firmware, `is_pc_in_bounds` is wired **only** to that exception — the two speculative
> paths — and both treat OOB as warn-and-skip. The strong "hard fault on a committed demand-fetch OOB
> PC" is **not present in this software image** (§8 residual); it is presumed HW-enforced or
> forward-looking. The hard fault that *does* exist on the fetch front-end is the **opcode-value**
> dispatch bound (a different bound; §7), not the PC-address bound. `[HIGH/OBSERVED]`

---

## 1. String anchors (the named xrefs this page hangs on)

Every step is pinned to a `'S:'`-prefixed DEBUG log string in the device DRAM image and to a host
format string. All offsets are byte-exact (`xxd`/`dd`).

### Device — DRAM (VA = DRAM file offset + `0x80000`)

| DRAM VA | file off | IRAM `const16` site | String (byte-exact) |
|---|---|---|---|
| `0x81864` | `0x1864` | `0x696d` | `S: is_pc_in_bounds: pc=0x%llx bound=[0x%llx, 0x%llx]` |
| `0x8189a` | `0x189a` | `0x69b1` | `S: is_pc_in_bounds: 0x%llx smaller than lower bound 0x%llx` |
| `0x818d6` | `0x18d6` | `0x69ff` | `S: is_pc_in_bounds: 0x%llx larger than higher bound 0x%llx` |
| `0x8171c` | `0x171c` | `0x5daf` | `S: WARNING: wait_for_cache_line's next_addr 0x%llx is out-of-bounds [0x%llx, 0x%llx], skipping it.` |
| `0x81d51` | `0x1d51` | `0x3513` | `WARNING: ok_to_evict's prefetch_pc 0x%llx is out-of-bounds [0x%llx, 0x%llx], skipping it.` |
| `0x82d90` | `0x2d90` | `0xd224` | `S: GetSequenceBounds` |
| `0x83b11` | `0x3b11` | `0x13f6a` | `S: ErrorHandler : Bad Opcode(0x%x)` (the **hard** dispatch fault, §7) |

A `strings dram.bin | grep -i bound` over the whole image shows **only** these PC-bounds strings plus
the unrelated DGE-DMA `S: DGE: Failed bounds check.` (`0x83105`, a different engine's source/dst
window — §7). There is **no** device string for a hard "fetch PC out of bounds → error state,"
consistent with both `is_pc_in_bounds` callers being the soft speculative paths. `[HIGH/OBSERVED]`

### Host — `libnrtucode_extisa.so` (x86-64, identity-mapped)

| File VA | String (byte-exact) |
|---|---|
| `0x920a86` | `nrtucode: invalid API usage in `%s`: %s: enable_pc_bounds_check : lower bound(0x`%lx`) must be smaller than upper bound(0x`%lx`)` |
| `0x920b07` | `%s: enabled PC bounds check for range [0x%lx, 0x%lx]` |
| `0x920b62` | `%s: disabled PC bounds check` |
| `0x920b7f` | `nrtucode_core_private_get_pc_bounds` |
| `0x920ba3` | `pc_bounds_soc_addr_lo` (then `pc_bounds_soc_addr_hi`) |
| `0x9201fb` | `nrtucode: invalid API usage in `%s`: `%s` is null` |
| `0x920c32` | `core` |

> **NOTE — `pc_bounds_soc_addr_lo`/`_hi` are the *parameter names*, decisively.** The two field names
> appear at `0x920ba3` as the `%s` argument the `get` path passes to its null-check assert (§6c),
> *and* verbatim in the shipped header (§6d, `nrtucode.h:563-564`). That double anchor is what fixes
> device `+0x38` = `pc_bounds_soc_addr_lo` (lower) and `+0x40` = `pc_bounds_soc_addr_hi` (upper).
> `[HIGH/OBSERVED]`

---

## 2. `is_pc_in_bounds @0x68d0` — the PC-range guard body

`entry a1,96`. The prologue `0x68d3..0x6967` co-issues FLIX/IVP slots and linear-sweeps with
`.byte` noise, but every **scalar** instruction from the entry-log (`0x696a`) to the return
(`0x6a1d`) is instruction-exact. The decoded skeleton:

```
68d0: entry  a1,96
696a: const16 a10,8 ; 696d const16 a10,0x1864 ; 6970 call8 0x18b84
      ;; LOG "S: is_pc_in_bounds: pc=0x%llx bound=[0x%llx, 0x%llx]"   (DRAM 0x81864)

--- LOWER-BOUND test --------------------------------------------------------------
6976: l32i.n a2,[a1+40] ; 6978 l32i.n a3,[a1+44]     ;; a2:a3 = pc  (lo:hi)
697a: l32i.n a4,[a1+24] ; 697c l32i.n a5,[a1+28]     ;; a4:a5 = LOWER bound (lo:hi)
6984: call0 0x57c18     ;; <cmp64>  3-way compare cmp(pc, lower) -> result @ [a1+20]
6987: l32i.n a2,[a1+20]
6989: bgez  a2,0x69c4    ;; cmp(pc,lower) >= 0  i.e. pc >= lower -> go check upper
                         ;; else fall through = pc < lower  (BELOW lower)
--- pc < lower (BELOW) ---
69b1: const16 a10,0x189a ; 69b4 call8 0x18b84
      ;; LOG "...0x%llx smaller than lower bound 0x%llx"   (DRAM 0x8189a)
69be: s8i a2,[a1+52]     ;; result byte = 0 (FALSE / out-of-bounds)
69c1: j 0x6a1a

--- pc >= lower: UPPER-BOUND test --------------------------------------------------
69c4: l32i.n a2,[a1+32] ; 69c6 l32i.n a3,[a1+36]     ;; a2:a3 = pc (lo:hi, re-staged)
69c8: l32i.n a4,[a1+24] ; 69ca l32i.n a5,[a1+28]     ;; a4:a5 = bound (lo:hi)
69d2: call0 0x47c64     ;; <cmp64'> 3-way compare cmp(pc, upper) -> result @ [a1+16]
69d5: l32i.n a2,[a1+16]
69d7: blti  a2,1,0x6a12  ;; cmp(pc,upper) < 1  i.e. <= 0  i.e. pc <= upper
                         ;; -> IN BOUNDS (true); else pc > upper (ABOVE upper)
--- pc > upper (ABOVE) ---
69ff: const16 a10,0x18d6 ; 6a02 call8 0x18b84
      ;; LOG "...0x%llx larger than higher bound 0x%llx"   (DRAM 0x818d6)
6a0c: s8i a2,[a1+52]     ;; result byte = 0 (FALSE)
6a0f: j 0x6a1a

--- IN BOUNDS ---
6a12: movi.n a2,1 ; 6a14 s8i a2,[a1+52]              ;; result byte = 1 (TRUE)

--- RETURN ---
6a1a: l8ui a2,[a1+52] ; 6a1d retw.n                  ;; return the 1-byte bool
```

### C pseudocode — the device check (names = real frame slots / addresses)

```c
/* is_pc_in_bounds @0x68d0
 * Caller stages the {pc, lower, upper} triple into THIS frame before call8 0x68d0:
 *   [a1+40]/[a1+44]  = pc    lo:hi    (used by the lower-bound test)
 *   [a1+32]/[a1+36]  = pc    lo:hi    (re-staged copy used by the upper-bound test)
 *   [a1+24]/[a1+28]  = bound lo:hi    (comparand of BOTH compares — see GOTCHA)
 *   [a1+52]          = 1-byte result  (1 = in-bounds, 0 = out-of-bounds)
 * Returns a 1-byte bool in a2.
 *
 * cmp64(x, y) is a call0 compiler-rt-style 3-way ("spaceship") helper:
 *   <0 if x<y, 0 if x==y, >0 if x>y.  (0x57c18 / 0x47c64; see COMPARE PRIMITIVE.)
 */
bool is_pc_in_bounds(uint64_t pc, uint64_t lower, uint64_t upper)   /* @0x68d0 */
{
    log("S: is_pc_in_bounds: pc=0x%llx bound=[0x%llx, 0x%llx]", pc, lower, upper); /* 0x81864 */

    /* LOWER test: bgez on cmp(pc,lower) >= 0  =>  pc >= lower */
    if (cmp64(pc, lower) < 0) {                 /* 0x6989 bgez a2,... (else-arm) */
        log("S: is_pc_in_bounds: 0x%llx smaller than lower bound 0x%llx", pc, lower); /* 0x8189a */
        return false;                           /* [a1+52] = 0  (0x69be) */
    }

    /* UPPER test: blti(...,1) on cmp(pc,upper) <= 0  =>  pc <= upper */
    if (cmp64(pc, upper) > 0) {                  /* 0x69d7 blti a2,1,... (else-arm) */
        log("S: is_pc_in_bounds: 0x%llx larger than higher bound 0x%llx", pc, upper); /* 0x818d6 */
        return false;                           /* [a1+52] = 0  (0x6a0c) */
    }

    return true;                                /* [a1+52] = 1  (0x6a12) */
}
```

**Return contract** `[HIGH/OBSERVED]`: a 1-byte bool (1 = in-bounds, 0 = out). The result byte is
materialised at frame slot `[a1+52]` in all three exit arms and reloaded with `l8ui` before
`retw.n`.

**Legal region** `[HIGH/OBSERVED — strings + tests]`: the **inclusive** interval `[lower, upper]`
of 64-bit SoC addresses — `pc >= lower && pc <= upper`. The two log strings name `"lower bound"` and
`"higher bound"`; the tests are `bgez` (`>= 0`) and `blti ...,1` (`<= 0`).

> **GOTCHA — both compares load the comparand from the SAME slot `[a1+24]/[a1+28]`.** The lower test
> and the upper test each `l32i.n a4,[a1+24]; a5,[a1+28]`. The branch+log ground truth fixes the
> **semantics** (`result1 >= 0 ⇒ pc >= lower`, so cmp1's comparand is the LOWER limit;
> `result2 <= 0 ⇒ pc <= upper`, so cmp2's comparand is the UPPER limit). Because both compares
> re-read `[a1+24]`, **either** the caller re-stages that slot (lower then upper) between the two
> compares **or** the two `call0` helpers take their operands in a swapped order. The operand-staging
> bundle (`addmi a1,a3,0xffffb800` co-issue at `0x697e`/`0x69cc`) is FLIX-desynced, so the exact
> slot→limit wiring for the *second* compare is **INFERRED**; the `pc>=lower`/`pc<=upper` semantics
> are HIGH. A reimplementation must not assume `[a1+24]` is statically the lower limit for both
> compares. `[HIGH sem / MED slot wiring]`

> **NOTE — COMPARE PRIMITIVE: 3-way is HIGH, signedness is MED.** The 64-bit compare is a `call0`
> 3-way helper forced by the `bgez`(`>=0`) and `blti...,1`(`<=0`) reads of its result. The two
> `call0` targets print **out-of-image** (`0x57c18`/`0x47c64`) because they are FLIX-bundle slots the
> linear sweep mis-frames, and the two compares use *different* helpers (encoded bytes `052951` vs
> `052941`). Signed-vs-unsigned is therefore MED across that desync — but the host enable() requires
> `lower < upper` **unsigned** (§6a) and all real SoC instruction addresses are positive, so the
> signedness choice changes **no** in-range decision for legitimate inputs. `[HIGH 3-way / MED signedness]`

---

## 3. Where the limits live on the device — the bounds global `DRAM 0x82dc0`

The host does not write `is_pc_in_bounds`'s frame directly; it writes a device-DRAM control block,
which the firmware surfaces through a small getter, `GetSequenceBounds @0xd224`, that copies the
4-word `{lower64, upper64}` bounds global at **DRAM `0x82dc0`** into the exact slot layout
`is_pc_in_bounds` consumes:

```
d224: const16 a10,0x2d90 ; call8 0x18b84      ;; LOG "S: GetSequenceBounds"   (DRAM 0x82d90)
d25b: const16 a2,0x2dc0                        ;; a2 = DRAM 0x82dc0 (bounds global)
d25e: l32i.n a3,[a2+12] ; s32i.n a3,[a1+44]    ;\
d262: l32i.n a3,[a2+8]  ; s32i.n a3,[a1+40]    ; \  copy {lo,hi} pair into [a1+44/40/36/32]
d266: l32i.n a3,[a2+4]  ; s32i.n a3,[a1+36]    ; /  (the pc-side slots is_pc_in_bounds reads)
d26a: l32i.n a3,[a2+0]  ; s32i.n a3,[a1+32]    ;/
d26e: l32i.n a3,[a2+12] ; s32i.n a3,[a1+28]    ;\
d272: l32i.n a3,[a2+8]  ; s32i.n a3,[a1+24]    ; \  AND into [a1+28/24/20/16]
d276: l32i.n a3,[a2+4]  ; s32i.n a3,[a1+20]    ; /  (the bound-side slots)
d27a: l32i.n a2,[a2+0]  ; s32i.n a2,[a1+16]    ;/
```

So `0x82dc0[+0..+7]` is the lower 64-bit limit and `[+8..+15]` the upper.

> **NOTE — `0x2dc0` has exactly ONE IRAM reference: the reader.** A whole-image grep finds the single
> `const16 a2,0x2dc0` at `0xd25b` (the `GetSequenceBounds` reader) and **no** `const16 0x2dc0` STORE
> anywhere in IRAM. The runtime population of `0x82dc0` is therefore the host's `enable()`/`disable()`
> write to device-accessor field `+0x38`/`+0x40` (§6) — the precise `+0x38`/`+0x40 ↔ 0x82dc0`
> aliasing is the device register-window / CSR mapping, **INFERRED** from the host write edge + the
> device read edge (no in-IRAM store closes it). `[HIGH read / MED host↔global wiring]`

**Static-image default** `[HIGH/OBSERVED]`: `0x82dc0[+0..+7]` (the lower limit) is **all-zero** in
the static carve. The upper-half tail bytes in the static image are ordinary image padding, **not**
relocations — the DRAM `.c.o` carries only 2 x86 relocations, neither into this region
(`readelf -r` verified). The real values arrive at runtime from the host writes.

---

## 4. The two enforcement points — both speculative, both soft

A whole-image grep for `call8 0x68d0` (plus an indirect/`jx`/function-pointer-table check) finds
**exactly two** callers: `rg -c 'call8\s+0x68d0' iram.asm` ⇒ **2**.
`[HIGH/OBSERVED]`

### 4a. Caller 1 — `wait_for_cache_line @0x5cd0`, call `@0x5d79` (the next-line prefetch)

```
5d20: const16 a6,0x5654 ; 5d23 l32i.n a7,[a6]   ;; block_size  (DRAM 0x85654)
5d25: add.n a7,a3,a7 ; 5d27 saltu a3,a7,a3 ; 5d2a add.n a3,a4,a3
      ;; next_addr = current_pc + block_size  (64-bit add with carry-propagate via saltu)
5d61: call8 0x15208            ;; PrefetchHelper::check  (FW-05 / branch-prefetch.md)
5d79: call8 0x68d0             ;; is_pc_in_bounds(next_addr, [lo,hi])
5d7c: bnez.n a10,0x5dbb        ;; a10 != 0  => IN-BOUNDS -> 0x5dbb continue the cache-line fill
      ;; else (a10 == 0, OUT-OF-BOUNDS) fall through:
5daf: const16 a10,0x171c ; 5db2 call8 0x18b84
      ;; LOG "S: WARNING: wait_for_cache_line's next_addr 0x%llx is out-of-bounds [..], skipping it." (0x8171c)
5db8: j 0x5e5b                 ;; *** SKIP the speculative fill (SOFT) ***
```

The look-ahead line is **not fetched** when out of range; the demand fetch of the current PC is
unaffected. Soft skip-with-warning. `[HIGH/OBSERVED]`

### 4b. Caller 2 — branch-prefetch hint path, call `@0x34de` (the hint target)

```
34de: call8 0x68d0             ;; is_pc_in_bounds(prefetch_pc, [lo,hi])
34e1: bnez.n a10,0x351e        ;; a10 != 0  => IN-BOUNDS -> 0x351e proceed with the push
      ;; else (OUT-OF-BOUNDS) fall through:
3513: const16 a11,0x1d51 ; 3516 movi.n a10,4   ;; log level = 4 (WARNING)
3518: call8 0x1405c            ;; warn/skip logger (NOT the error-notification dispatch)
351b: j 0x3556                 ;; *** SKIP the speculative prefetch (SOFT) ***
      ;; LOG "WARNING: ok_to_evict's prefetch_pc 0x%llx is out-of-bounds [..], skipping it." (0x81d51)
```

An out-of-range branch-prefetch target is **skipped** with a warning. `[HIGH/OBSERVED]`

> **NOTE — the warn helper `0x1405c` is purely soft.** `0x1405c` begins
> `entry a1,176` (a clean function boundary — the `call8 0x13e00` at `0x14059` belongs to the
> *preceding* function). Its body calls only format/emit helpers (`0x14128`/`0x14138`/`0x14b44`/
> `0x14164`/…); it **never** calls the error-notification dispatch `0x13e00`. So the prefetch-OOB
> path cannot escalate to a fault. Contrast the hard dispatch path (§7), which *does* reach `0x13e00`
> via `ErrorHandler 0x13f58`. `[HIGH/OBSERVED]`

### 4c. The correctness backstop (why a skipped speculative line is never executed)

The per-cache-line state word (`{0=invalid, 1=fill-in-flight, 2=valid}`) and the `wait_for_valid`
gate ([SEQ IRAM Instruction Cache / Overlay](iram-cache.md)) are **independent** of the prefetch:
the SEQ can only **execute** a line whose `state == 2`. A mispredicted or skipped speculative line
can never be executed prematurely — the bounds guard only decides whether to **start** a speculative
fill, never whether an already-fetched instruction commits. `[HIGH/OBSERVED — CARRIED from iram-cache]`

---

## 5. The host control API — staging `[lo,hi]` into device DRAM `+0x38`/`+0x40`

Three exported functions implement the device-staging contract. They are byte-identical in structure
across both shipped host builds (the runtime-lib `libnrtucode_extisa.so` and the gpsimd-customop
`libnrtucode.so`); addresses for both are tabulated in §6. The common call shape:

```
core (rdi)                      ;; nrtucode_core_t*
*(core+0x00) = ctx              ;; mov rdi,[rdi]   -> the nrtucode_context_t*
*ctx         = rw_vtable        ;; mov rax,[rdi]   -> the platform RW vtable
dram_base    = *(core+0x20)     ;; mov rsi,[rbx+0x20] -> device-memory base for this core
target       = dram_base + OFF  ;; add rsi,0x38 (lower) / add rsi,0x40 (upper)
len          = 8                ;; mov edx,8
buf          = &local           ;; lea rcx,[rsp+..]
WRITE: call *0x8(%rax)          ;; vtable slot +0x08
READ : call *(%rax)             ;; vtable slot +0x00
```

> **NOTE — two different `+0x38`s; don't conflate them.** The **host** `nrtucode_core_t` struct field
> at `+0x38` is the destructor userdata (set to 0 at create `0x9a45`, freed in destroy). The **device**
> field `+0x38` is `pc_bounds_soc_addr_lo`, reached as `*(core+0x20) /*dram_base*/ + 0x38` through the
> memhandle vtable — a *device-memory* address, not a host-struct offset. This page's `+0x38`/`+0x40`
> are always the **device** ones. `[HIGH/OBSERVED]`

### 5a. `nrtucode_core_enable_pc_bounds_check(core, lo, hi)` — validate + write both limits

```
;; extisa @0xa860 ; customop @0x3099a0 ; args (SysV): rdi=core, rsi=lo, rdx=hi
a86f: test rdi,rdi ; je a926       ;; null-core -> fprintf "...`core` is null" + abort()
a87e: mov rdi,[rdi]                 ;; rdi = ctx
a881: cmp rsi,rdx ; jae a8a8       ;; if lo >= hi (UNSIGNED) -> ERROR arm:
        ;;   a8b0: sev=1 log "enable_pc_bounds_check : lower bound(0x%lx) must be smaller than
        ;;         upper bound(0x%lx)"  (0x920a86) ;  a8cd: return 8 = NRTUCODE_ERR_INVALID
a886: mov rsi,[rbx+0x20] ; a88a mov rax,[rdi] ; a88d add rsi,0x38   ;; target = dram_base + 0x38
a891: lea rcx,[rsp+0x18] (=&lo) ; a896 mov edx,8 ; a89b call *0x8(%rax)
        ;; *** WRITE lower (8B) -> device field +0x38 (pc_bounds_soc_addr_lo) ***
a89e: test eax,eax ; jne a8a2 -> return that error
a8d8: ... add rsi,0x40 ; lea rcx,[rsp+0x10] (=&hi) ; mov edx,8 ; a8f0 call *0x8(%rax)
        ;; *** WRITE upper (8B) -> device field +0x40 (pc_bounds_soc_addr_hi) ***
a8f3: test eax,eax ; jne -> return error
a908: sev=4 log "%s: enabled PC bounds check for range [0x%lx, 0x%lx]"  (0x920b07)
a91e: return 0 (success)
```

```c
/* nrtucode_core_enable_pc_bounds_check  (extisa @0xa860 / customop @0x3099a0) */
nrtucode_result_t nrtucode_core_enable_pc_bounds_check(
        nrtucode_core_t *core, uint64_t lo, uint64_t hi)
{
    if (core == NULL) { fprintf(stderr, "...`core` is null"); abort(); }   /* a86f */

    nrtucode_context_t *ctx = core->context;          /* *(core+0x00)  (a87e) */
    rw_vtable_t        *vt  = *(rw_vtable_t **)ctx;    /* *ctx          (a88a) */

    if (!(lo < hi)) {                                  /* a881 cmp/jae: UNSIGNED lo>=hi  */
        log(ctx, /*sev=*/1,                            /* a8b0          */
            "enable_pc_bounds_check : lower bound(0x%lx) must be smaller than upper bound(0x%lx)",
            lo, hi);                                   /* string 0x920a86 */
        return NRTUCODE_ERR_INVALID;                   /* = 8           (a8cd) */
    }

    uint64_t dram_base = *(uint64_t *)(core + 0x20);   /* a886          */
    int r = vt->write(ctx, dram_base + 0x38, 8, &lo);  /* slot +0x8     (a89b) */
    if (r != 0) return r;                              /* a89e          */
    r = vt->write(ctx, dram_base + 0x40, 8, &hi);      /* slot +0x8     (a8f0) */
    if (r != 0) return r;                              /* a8f3          */

    log(ctx, /*sev=*/4, "%s: enabled PC bounds check for range [0x%lx, 0x%lx]", lo, hi); /* 0x920b07 */
    return NRTUCODE_SUCCESS;                            /* 0  (a91e)     */
}
```

> **GOTCHA — `lower < upper` is required (UNSIGNED), and it is half-open on the host.** The host
> rejects `lo >= hi` with `cmp rsi,rdx ; jae` (unsigned) and returns `NRTUCODE_ERR_INVALID` (= 8 — the
> 9th value of the `nrtucode_result_t` enum, `nrtucode.h:101-109`). An **empty** window (`lo == hi`)
> is rejected too. But note the *device* check is **inclusive** (`pc <= upper`, §2), so the host's
> half-open `[lo, hi)` validation and the device's inclusive `[lo, upper]` test differ by one ULP at
> the top. For a window armed to bracket a library's IRAM extent this is immaterial; a reimplementation
> that round-trips a window through enable→get must remember the device stores exactly what the host
> wrote (no `hi-1` normalisation happens). `[HIGH/OBSERVED]`

### 5b. `nrtucode_core_disable_pc_bounds_check(core)` — program the maximal window `[0, 2^64)`

```
;; extisa @0xa960 ; customop @0x309aa0
a965: test rdi,rdi ; je a9e5       ;; null-core -> fprintf + abort
a96d: mov QWORD [rsp+0x8], 0x0                 ;; slot @rsp+8 := 0
a976: mov QWORD [rsp],    0xffffffffffffffff   ;; slot @rsp+0 := 0xFFFFFFFFFFFFFFFF
a988: add rsi,0x38 ; a98c lea rcx,[rsp+0x8] ; a996 call *0x8(%rax)
        ;; *** WRITE +0x38 (lower) <- *(rsp+8) = 0x0 ***
a9ad: add rsi,0x40 ; a9b1 mov rcx,rsp       ; a9b9 call *0x8(%rax)
        ;; *** WRITE +0x40 (upper) <- *(rsp+0) = 0xFFFFFFFFFFFFFFFF ***
a9c7: sev=4 log "%s: disabled PC bounds check"  (0x920b62)
a9dd: return 0
```

```c
/* nrtucode_core_disable_pc_bounds_check  (extisa @0xa960 / customop @0x309aa0) */
nrtucode_result_t nrtucode_core_disable_pc_bounds_check(nrtucode_core_t *core)
{
    if (core == NULL) { fprintf(stderr, "...`core` is null"); abort(); }   /* a965 */

    uint64_t local_hi = 0x0;                  /* [rsp+0x8]  (a96d) */
    uint64_t local_lo = 0xFFFFFFFFFFFFFFFFull; /* [rsp]      (a976) */
    /* The pointer math is the subtle part: lower-write reads &[rsp+8]=0,
     * upper-write reads &[rsp]=~0.  So: */
    uint64_t dram_base = *(uint64_t *)(core + 0x20);

    int r = vt->write(ctx, dram_base + 0x38, 8, &local_hi); /* +0x38 (lower) <- 0x0          (a996) */
    if (r != 0) return r;
    r = vt->write(ctx, dram_base + 0x40, 8, &local_lo);     /* +0x40 (upper) <- 0xFFFF...FF   (a9b9) */
    if (r != 0) return r;

    log(ctx, /*sev=*/4, "%s: disabled PC bounds check");    /* 0x920b62 */
    return NRTUCODE_SUCCESS;                                 /* a9dd     */
}
```

> **CORRECTION — disable arms the MAXIMAL window `[0, 2^64)`, not an inverted/empty one.** A naive
> reading (and the earlier FW-06 §6b reading) maps disable to `lower=0xFFFFFFFFFFFFFFFF, upper=0`
> (inverted/empty), concluding "disable ⇒ every prefetch is skipped ⇒ no speculation." **This is
> wrong.** Trace the pointers exactly: `a96d` puts `0x0` at `[rsp+0x8]` and `a976` puts `~0` at
> `[rsp]`. The lower write (`+0x38`) reads `&[rsp+0x8]` ⇒ writes **`0x0`**; the upper write (`+0x40`)
> reads `&[rsp]` ⇒ writes **`0xFFFFFFFFFFFFFFFF`**. So disable programs **lower = `0x0`, upper =
> `0xFFFFFFFFFFFFFFFF`** = the maximal open window `[0, 2^64)`. Verified identically in *both* host
> builds (`extisa @0xa988`/`0xa9ad`; customop `@0x309ac8`/`0x309aed`). The device consequence flips
> too: with `[0, 2^64)`, `is_pc_in_bounds` returns **true for every PC**, so **every** speculative
> prefetch is **in-bounds and proceeds** — disable = "no PC restriction; full speculation," the exact
> opposite of "skip everything." This is the correct device semantics. `[HIGH/OBSERVED — re-traced
> against both host binaries]`

### 5c. `nrtucode_core_private_get_pc_bounds(core, *lo, *hi)` — read both limits back

```
;; extisa @0xaa10 ; customop @0x309b50
aa19: test rdi,rdi ; je aa8e        ;; null core -> fprintf("`core` is null") + abort
aa21: test rsi,rsi ; je aab9        ;; null out_lo -> fprintf arg "pc_bounds_soc_addr_lo" + abort
aa2d: test rdx,rdx ; je aae4        ;; null out_hi -> fprintf arg "pc_bounds_soc_addr_hi" + abort
aa39: mov rdi,[rdi] ; aa3c mov rsi,[r15+0x20] ; aa40 mov rax,[rdi] ; aa43 add rsi,0x38
aa47: lea rcx,[rsp+0x8] ; aa4c mov edx,8 ; aa51 call *(%rax)
        ;; *** READ +0x38 (lower) -> [rsp+0x8]  via vtable slot 0 ***
aa5e: ... add rsi,0x40 ; aa65 mov rcx,rsp ; aa68 mov edx,8 ; aa6d call *(%rax)
        ;; *** READ +0x40 (upper) -> [rsp]     via vtable slot 0 ***
aa73: mov rax,[rsp+0x8] ; aa78 mov [r14],rax      ;; *out_lo = lower
aa7b: mov rax,[rsp]     ; aa7f mov [rbx],rax      ;; *out_hi = upper
aa82: xor eax,eax (return 0)
```

```c
/* nrtucode_core_private_get_pc_bounds  (extisa @0xaa10 / customop @0x309b50) */
nrtucode_result_t nrtucode_core_private_get_pc_bounds(
        nrtucode_core_t *core, uint64_t *out_lo, uint64_t *out_hi)
{
    if (core   == NULL) { fprintf(stderr, "`core` is null");                  abort(); } /* aa19 */
    if (out_lo == NULL) { fprintf(stderr, "`pc_bounds_soc_addr_lo` is null"); abort(); } /* aa21 */
    if (out_hi == NULL) { fprintf(stderr, "`pc_bounds_soc_addr_hi` is null"); abort(); } /* aa2d */

    uint64_t dram_base = *(uint64_t *)(core + 0x20);
    uint64_t lo, hi;
    int r = vt->read(ctx, dram_base + 0x38, 8, &lo);  /* vtable slot 0  (aa51) */
    if (r != 0) return r;
    r = vt->read(ctx, dram_base + 0x40, 8, &hi);      /* vtable slot 0  (aa6d) */
    if (r != 0) return r;

    *out_lo = lo;   /* aa78 */
    *out_hi = hi;   /* aa7f */
    return NRTUCODE_SUCCESS;
}
```

> **NOTE — READ is vtable slot 0, WRITE is vtable slot `+0x8`.** `get` reads with `call *(%rax)`
> (slot 0); `enable`/`disable` write with `call *0x8(%rax)` (slot `+0x8`). The vtable is the platform
> memhandle RW pair installed by the host runtime at context-create (default `.data.rel.ro` stub that
> returns errors; overridden by libnrt's real memhandle-DMA impl). A reimplementation's accessor
> object must expose exactly `{read@+0, write@+8}` with signature `int fn(ctx, soc_addr, len, buf)`.
> `[HIGH/OBSERVED]`

### 5d. The header contract (`nrtucode.h:556-577`, `nrtucode_private.h:67-68`)

The shipped header states the design intent verbatim:

> *"When enabled, the core will check if the PC is within the bounds of the SoC when fetching. If the
> PC is out of bounds, the core will raise an error notification to runtime and goes in the error
> state. One exception is that when PC is generated by speculative prefetching, then normal execution
> flow won't be interrupted."*
>
> Params: `pc_bounds_soc_addr_lo` = *"Lower bound of the SoC address"*; `pc_bounds_soc_addr_hi` =
> *"Upper bound of the SoC address."*

The two soft callers (§4) realise exactly the carved-out **speculative-prefetch exception** — the
only `is_pc_in_bounds` wiring present in this shipped firmware build.

---

## 6. API surface — addresses across both host builds

| Function | extisa | customop | internal | device field | len | gate |
|---|---|---|---|---|---|---|
| `nrtucode_core_enable_pc_bounds_check` | `0xa860` | `0x3099a0` | `0x9b14d0` | `+0x38`, `+0x40` | 8, 8 | NULL only |
| `nrtucode_core_disable_pc_bounds_check` | `0xa960` | `0x309aa0` | `0x9b15d0` | `+0x38`, `+0x40` | 8, 8 | NULL only |
| `nrtucode_core_private_get_pc_bounds` | `0xaa10` | `0x309b50` | `0x9b1680` | `+0x38`, `+0x40` | 8, 8 | NULL ×3 |

> **NOTE — no claim/boot gate on the PC-bounds API.** Unlike the DGE priority-class / mailbox setters
> (which gate on `boot_state == 1` *and* the NX_POOL core-kind bitmask — see
> [The DGE Host-Private API](../../runtime/dge-host-api.md)), the three PC-bounds functions gate only
> on NULL pointers. The host can therefore **arm/adjust the sandbox window before the Q7 image is
> claimed** — consistent with arming the legal window before releasing a custom op to run.
> `[HIGH/OBSERVED]`

> **GOTCHA — `extisa` and `customop`/`internal` are different binaries with different addresses;
> a third (`internal`) lives at the `0x9b1xxx` band.** Do not look up an extisa address in the
> customop lib or vice-versa. The host control-flow is byte-identical across all three; only the link
> addresses and the logger symbol (`customop` uses an unnamed `sub_308A20`; `internal`/`extisa` use a
> named `nrtucode_context_log`) differ. `[HIGH/OBSERVED]`

---

## 7. The SEQ engine's other bounds — what `is_pc_in_bounds` is NOT

Four structurally distinct "bounds" coexist in the SEQ/POOL firmware. Confusing them is the easiest
way to mis-model the isolation surface, so they are tabulated explicitly. Only **#1** is this page.

| # | Bound | Guards | Limit storage | Comparison | Violation | Hard? |
|---|---|---|---|---|---|---|
| 1 | **PC-RANGE** (`is_pc_in_bounds @0x68d0`) | the FETCH **address** — but only the SPECULATIVE next-line / prefetch targets (2 sites) | device global `DRAM 0x82dc0`; host `+0x38`/`+0x40` | `pc >= lower && pc <= upper` (inclusive, 3-way 64-bit) | log + SKIP the fill (demand fetch unaffected) | **SOFT** |
| 2 | **DISPATCH** (`bgeu 177`) | the OPCODE **value** (byte `−0x41`) selecting the 178-entry handler table | literal `177` in IRAM; table `@DRAM 0x80814` | `bgeu 177, index` (unsigned) `@0x2e65` | `j 0x3198` → `ErrorHandler 0x13f58` "Bad Opcode" → `0x13e00` notify | **HARD** |
| 3 | **STACK-LIMIT** (ISL SR) | the data STACK pointer (custom-op HBM stack floor) | Xtensa ISL special reg (per-core SR) | HW store-below-ISL check (per store) | ISL stack-limit exception | **HARD** |
| 4 | **DGE-DMA bounds** (separate engine) | a DMA-gather source/dst address window | per-descriptor fields | per-descriptor compare | log `S: DGE: Failed bounds check` (`0x83105`) | soft/log |

Key contrasts `[HIGH/OBSERVED]`:

- **#1 vs #2.** #1 guards an **address** and is **soft** (speculative skip); #2 guards an **opcode
  value** and is **hard** (`ErrorHandler`). They are orthogonal — #2 catches a corrupt opcode byte
  even when the PC is perfectly in range. The hard dispatch bound, byte-exact:
  ```
  2e5f: addi a2,a2,-65 ; 2e62 movi a3,177 ; 2e65 bgeu a3,a2,0x2e6b
  2e68: j 0x3198            ;; out-of-range opcode -> default ->
  3198: ... call8 0x13f58   ;; ErrorHandler  -> LOG "S: ErrorHandler : Bad Opcode(0x%x)" (0x83b11)
                            ;; -> call8 0x13e00  *** error-notification dispatch (HARD) ***
  ```
- **#1 vs #3.** Both are address-range limits but on **different pointers**: #1 on the instruction PC
  (i-stream fetch), #3 on the data SP (the stack, see the ABI-09 stack switch). #3 is
  **hardware-enforced per store** via the Xtensa ISL SR and raises a hard exception; #1 is software
  on speculative look-ahead only and merely skips. They never interact.
- **#4** is a wholly separate DMA-gather engine's window check, named here only to disambiguate the
  `"bounds check"` string namespace (`0x83105` is **not** a PC bound).

---

## 8. NX-030 closure — what this isolates, and what it does not

**What protects the PC (precise):** `[HIGH host / MED device write-edge]`

- The legal instruction region is the 64-bit SoC interval `[lower, upper]` **defined and staged by
  the host** via `nrtucode_core_enable_pc_bounds_check(core, lo, hi)`, written to device-accessor
  fields `+0x38` (lower) / `+0x40` (upper) and surfaced on-device as the bounds global `DRAM 0x82dc0`
  (read by `GetSequenceBounds @0xd224`). The host knows the loaded ucode library's code extent; the
  **firmware does not self-derive the limits**. A reimplementation must drive the window from the
  loader, not from the firmware.
- `is_pc_in_bounds @0x68d0` enforces `pc >= lower && pc <= upper` (inclusive, 3-way 64-bit) and
  returns a bool. It is consulted at **exactly two** sites, **both speculative** look-ahead: the
  next-cache-line prefetch (`@0x5d79`, `next_addr = current_pc + block_size`) and the branch-prefetch
  hint (`@0x34de`). On a violation both paths **log a warning and skip** the speculative fill — they
  never fetch or execute an out-of-range instruction, and never hard-fault. This precisely matches
  the header's speculative-prefetch exception clause.
- The independent cache-validity gate (#4c) prevents any skipped/mispredicted speculative line from
  executing, so the soft guard's "skip" can never let stale code commit.
- **DISABLE programs the maximal window `[0, 2^64)`** (§5b CORRECTION), so disable cannot **arm** a
  window — it makes every speculative prefetch in-bounds (full speculation, no PC restriction). The
  host enable() enforces `lower < upper`, so a degenerate window cannot be armed by accident.

**The residual gap (stated honestly):** `[HIGH that the demand path does NOT call 0x68d0; INFERRED on the HW backstop]`

- In **this** shipped firmware build, `is_pc_in_bounds` is wired **only** to the two speculative
  paths. There is **no** firmware call of `is_pc_in_bounds` on the committed demand-fetch / PC-redirect
  path — the redirect machine (`0x5750`) writes the new 64-bit PC to CSR `0x1060`/`0x1080` **without**
  calling `0x68d0` ([SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md)). And there is **no**
  device string for a hard "fetch PC out of bounds → error state."
- The header's stronger clause (a non-speculative OOB PC "raises an error notification … goes in the
  error state") is therefore **either** realised in **hardware** (the NX-decode fetch path raising a HW
  fetch fault outside this software front-end) **or** a forward-looking contract not yet wired into
  this DEBUG build's software fetch loop. The hard software fault that **does** exist on the fetch
  front-end is the **opcode-value** dispatch bound (#2), which catches a corrupt instruction even with
  an in-range PC.

**Net:** NX-030 is **closed to the extent the software SEQ front-end can be**: speculative i-cache
prefetch and branch-prefetch are both gated by `is_pc_in_bounds` and skip out-of-range targets with a
warning (never fetch/execute them); the host enable/disable/get API stages a validated `[lower, upper]`
legal SoC window into the device; and the independent cache-validity gate prevents any
skipped/mispredicted line from executing. The one residual — the committed demand-fetch
hard-fault-on-OOB-PC promised by the header — is not present in this software image and is presumed
HW-enforced. That is the only piece not observable from the shipped device image.

> **NOTE — what this sandbox does NOT isolate.** PC-bounds confines only the **instruction-fetch
> address** of the loaded custom op. It does **not** bound data loads/stores (that is the ISL stack
> limit #3 plus the DMA window #4), does **not** validate opcode legality (that is the hard dispatch
> bound #2), and does **not**, in this build, hard-stop a *committed* out-of-range fetch (the residual
> above). A complete isolation model must compose all four. See
> [Custom-Op Reachability / Isolation Model](../../control/security/reachability-isolation.md).

---

## Honesty ledger

**HIGH / OBSERVED** (direct disassembly re-verified by aligned re-disasm; header text; ELF/dynsym;
byte-exact strings):

- Carve reproduced: `iram.bin` sha256 `8e4412b9…` and `dram.bin` sha256 `7bdf6ed7…`; host
  `libnrtucode_extisa.so` sha256 `dc00763d…`.
- `is_pc_in_bounds @0x68d0` full scalar body: `entry a1,96`; the pc/lower/upper loads; the two 3-way
  64-bit compares; `bgez`(`>=lower`) / `blti...,1`(`<=upper`); the three log arms
  `0x81864`/`0x8189a`/`0x818d6`; result byte `[a1+52]`; `retw.n`. Inclusive `[lower, upper]`; bool
  return.
- **Exactly two** callers of `0x68d0` (`rg -c` ⇒ 2; no `jx`/pointer-table ref): `wait_for_cache_line
  @0x5d79` (`next_addr = current_pc + block_size`, soft skip+warn `0x8171c` → `j 0x5e5b`) and the
  branch-prefetch path `@0x34de` (soft skip+warn `0x81d51` → `0x1405c` → `j 0x3556`). Both speculative;
  both soft. The `0x1405c` warn helper (`entry` boundary; calls only `0x14128`/`0x14138`/`0x14b44`,
  never `0x13e00`) confirms the prefetch OOB is purely soft.
- Bounds global `DRAM 0x82dc0` read by `GetSequenceBounds @0xd224`; `0x2dc0` has **exactly one** IRAM
  reference (the reader); static default of the lower half is zero.
- Host API: `enable` (`lower<upper` unsigned `jae` guard; write `lo@+0x38`, `hi@+0x40`; sev-1 error +
  sev-4 success logs; return `NRTUCODE_ERR_INVALID = 8` on bad range), `disable` (**maximal window
  `[0, 2^64)`** — lower `0x0@+0x38`, upper `0xFFFFFFFFFFFFFFFF@+0x40`; re-traced in **both** host
  builds), `get` (read `lo`/`hi` back; vtable slot 0 = read, slot `+0x8` = write; three NULL asserts
  naming `core`/`pc_bounds_soc_addr_lo`/`pc_bounds_soc_addr_hi`). Header contract verbatim
  (`nrtucode.h:556-577`).
- Three-bounds contrast: PC-range **soft** / dispatch-opcode **hard** (`bgeu 177 @0x2e65` →
  `ErrorHandler 0x13f58` → `0x13e00`) / ISL stack **hard**.

**MED** (strong inference, usually across a FLIX/IVP-bundle desync):

- The exact slot→limit wiring of the **second** (upper) compare's operands (the `addmi`/IVP co-issue at
  `0x697e`/`0x69cc`); `pc<=upper` semantics are HIGH from the `blti`+log, the `[a1+24]`-reuse mechanics
  are inferred.
- Signed-vs-unsigned form of the two `call0` 64-bit compare helpers (`0x57c18`/`0x47c64` print
  out-of-image = FLIX mis-frame); the 3-way result semantics are HIGH; for positive SoC addresses the
  choice changes no decision.
- The host `+0x38`/`+0x40 ↔ device global DRAM 0x82dc0` aliasing (no `const16-0x2dc0` STORE in IRAM;
  the write edge is the device-accessor / CSR window, inferred from the host write + the device read).
- `wait_for_cache_line`'s exact bound-arg register staging at `0x5d65-0x5d79` (FLIX desync); the
  `next_addr` computation and the call+skip are HIGH.

**LOW / INFERRED:**

- The non-speculative demand-fetch "hard error state on OOB PC" promised by the header is **not** in
  this software image (the redirect path `0x5750` does not call `0x68d0`); presumed HW-enforced or
  forward-looking (§8 residual).

**OUT OF SCOPE (noted, not decoded here):** the DGE-DMA `S: DGE: Failed bounds check` (`0x83105`) is a
separate engine's address-window check; the IRAM-cache internals are
[SEQ IRAM Instruction Cache / Overlay](iram-cache.md); the branch-hint state machine is
[SEQ Branch + Prefetch-Hint](branch-prefetch.md); the DGE priority/mailbox host API is
[The DGE Host-Private API](../../runtime/dge-host-api.md).

---

## See also

- [SEQ Branch + Prefetch-Hint](branch-prefetch.md) — the prefetch-arming path whose hint target
  caller 2 (`@0x34de`) gates.
- [SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md) — the demand-fetch / redirect machine
  that, in this build, does **not** call `is_pc_in_bounds` (the §8 residual).
- [SEQ IRAM Instruction Cache / Overlay](iram-cache.md) — the next-line fill caller 1 (`@0x5d79`)
  gates, and the `state==2` validity backstop that makes skips safe.
- [SEQ Main FSM Loop](main-loop.md) — the poll/stop FSM this guard sits inside.
- [SEQ Error-Handler / Fault Reporting](error-handler.md) — the **hard** opcode-value dispatch fault
  (the contrast bound #2).
- [The DGE Host-Private API](../../runtime/dge-host-api.md) — the runtime-facing host surface
  (priority/mailbox/PC-bounds), with the claim+kind gates the PC-bounds calls deliberately skip.
- [nrtucode_core_t Struct + Introspection/Boot](../../runtime/nrtucode-core.md) — the host struct,
  `dram_base @core+0x20`, the boot/claim handshake.
- [Custom-Op Reachability / Isolation Model](../../control/security/reachability-isolation.md) — how
  PC-bounds composes with the stack/DMA/opcode bounds in the overall threat model.
- [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md) — why `ncore2gp` is the only
  config that decodes SEQ FLIX correctly.
