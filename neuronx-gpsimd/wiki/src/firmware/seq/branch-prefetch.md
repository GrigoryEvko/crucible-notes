# SEQ Branch + Prefetch-Hint

The SEQ engine — the Vision-Q7 NX "Cairo" sequencer core (`ncore2gp`) — software-decodes a
**`BRANCH` opcode** out of its instruction (`'S:'`) stream and, on a *taken* branch, does two
things at once: it drives the [PC-redirect machine](fetch-pc-redirect.md) at `0x5750` (which
rewrites the architectural 64-bit PC in CSRs `0x1060`/`0x1080`), and — gated by a
**prefetch-enable flag** — it arms an **explicit branch-prefetch hint**. The hint is a
declarative record `{br_addr, br_target, decision}` that lets the fetch front-end **warm the
[software IRAM cache](iram-cache.md) ahead of the branch target**: when the fetch PC reaches
the hinted branch's cache block and the target falls in the *next* block, the front-end
**pushes** that target block into the cache (sets the `cache_idx_pushed` bitmask
`descr[+56]`), so the cache's normal DMA fill starts *early* and its latency overlaps with
continued execution of the in-flight block.

This page reconstructs that mechanism instruction-exact: the `BRANCH` handler at `0xc7fc`
(`branch.cpp`), its 11-way sub-opcode table, the software-evaluated taken/not-taken decision,
the taken-path redirect + arm tail, and the full `PrefetchHelper` state machine
(`branch_prefetch_hint.cpp`) — `arm` / `disarm` / `check+prefetch` / `invalidate-all` — plus
the cache-push predicate `0x15010` and the fetch-side consumer that turns a hint into a
`descr[+56]` push. It closes with the correctness guard that makes the prefetch *purely a
latency hint* (it can never cause incorrect execution) and the out-of-bounds / mispredict /
invalidation edge cases.

The substrate is the carved `CAYMAN_NX_POOL` **DEBUG** firmware image. The DEBUG build keeps
every `'S:'` log format string in its DRAM data segment, and each handler loads its string
with a two-halfword `const16` pair, so the strings are a precise oracle: a function's identity,
its arguments, and its assertion line numbers are recoverable byte-exact from the string it
references. The recovered C++ names below (`BRANCH`, `BRTAKEN`, `BranchPrefetchHint`,
`PrefetchHelper`, `resolve_hint_decision`, `branch.cpp`, `branch_prefetch_hint.cpp`) **are the
firmware's own DEBUG strings**, dereferenced from its DRAM image — they are binary-derived, not
imported. The **PERF** build strips these strings and re-lays-out the code, so every IRAM
offset on this page is DEBUG-specific (see [§16 DEBUG-vs-PERF](#16-debug-vs-perf)). All
addresses are IRAM-image offsets, which equal device IRAM virtual addresses (`.rodata` VMA ==
file offset for this carve). DRAM string addresses are device DRAM VAs; the file offset into
the carved DRAM blob is `VA − 0x80000` (the DRAM image loads at VA `0x80000`).

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte / string / instruction read or re-disassembled from the carved image;
`INFERRED` = reasoned over those reads; `CARRIED` = taken from a cited sibling report at
its original confidence. Crossed with `HIGH` / `MED` / `LOW`. Callouts: **QUIRK**
(counter-intuitive but real), **GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a
naive reading), **NOTE** (orientation). The page default is `[HIGH/OBSERVED]`; claims that
depart from it carry an explicit tag. Everything below was re-disassembled with the native
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, Binutils 2.34.20200201 / Xtensa Tools 14.09)
against the carved `img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}_contents.c.o` members of
`libnrtucode.a`.

> **GOTCHA — this is the Q7 FLIX datapath core, not the scalar NCFW core.** The carved image
> decodes under the *only* shipped Xtensa config, `ncore2gp` (Vision-Q7, FLIX-bearing,
> `IsaMaxInstructionSize = 32`). That is correct for the SEQ engine: its body genuinely
> contains FLIX/IVP vector bundles. Do **not** apply the scalar-LX caveat here — the SEQ core
> *is* FLIX, and several functions below (`PrefetchHelper::arm`, `check+prefetch`, the front-end
> `RTL_PC_check` body) carry **real** multi-slot IVP bundles that the *linear* disassembler
> mis-frames into `.byte`/`l32r`/`addmi` runs. Every quoted instruction below was re-verified by
> re-disassembling from a confirmed instruction boundary (an `entry`, a `retw`, or a `const16`
> log site), so the scalar skeleton, all state writes, all call/log anchors, and all constants
> are instruction-exact; the co-issued IVP vector slots are not control-flow relevant and are
> left undecoded. `[HIGH/OBSERVED on the skeleton]`

---

## 0. Carve provenance and the one-line proof

The firmware carve is hash-checked against the sibling
anchors, so every offset below is anchored to a reproducible binary:

```
archive : extracted/.../custom_op/c10/lib/libnrtucode.a
members : img_CAYMAN_NX_POOL_DEBUG_IRAM_contents.c.o  → .rodata = IRAM image @VA 0x0
          img_CAYMAN_NX_POOL_DEBUG_DRAM_contents.c.o  → .rodata = DRAM image @VA 0x80000
carve   : ar x <archive> <members> ; objcopy -O binary --only-section=.rodata <member> <bin>

  iram.bin  116768 B (0x1c820)  sha256 8e4412b99201f62d…ab9ed70a   ← matches the iram-cache anchor
  dram.bin   28448 B (0x6f20)   sha256 7bdf6ed7ccd27b37…d6816ecd
  iram head bytes  06 76 00 00  =  j 0x1dc   (the reset vector)
```

The IRAM image (114 KiB) exceeds the 64-KiB IRAM aperture, which is *why* the
[software IRAM cache](iram-cache.md) exists in the first place — and why warming it ahead of a
branch is worth doing. `[HIGH/OBSERVED — carve reproduced, hash matches the iram-cache page's
anchor exactly.]`

---

## 1. The branch + prefetch model in one diagram

```
  'S:' stream
      │  BRANCH opcode  (handler-object vtable[0] = branch.cpp execute() @0xc7fc)
      ▼
  ┌──────────────────────── branch.cpp @0xc7fc ────────────────────────┐
  │  sub-opcode = instr[+14]  →  jx via DRAM table 0x82b84 (11 variants)│
  │  evaluate CONDITION (operands + engine state)  →  BRTAKEN bool      │
  └────────────────────────────────┬───────────────────────────────────┘
                          taken?    │ not-taken → fall-through (no redirect, no prefetch)
                                    ▼
        ┌── prefetch-enable flag state[0x8564c] bit0 ? ──┐
        │ set                                       clear │ (legacy)
        ▼                                                 ▼
   0x8660 redirect helper                       0x5750 PC-REDIRECT machine
        │                                       (write CSR 0x1060/0x1080)  [fetch-pc-redirect]
        └────────────────────┬────────────────────────────┘
                             ▼  arm-the-hint tail (0xcb15)
              read armed bit  0x6ec4  (state[0x855e0+0] & 1)
              compute target  0x4c3c  →  decision bool (xor/or/saltu)
              DISARM consumed hint  0x151dc → 0x151e8  (state[0x855e0+0] = 0)

  ── branch_prefetch_hint.cpp / PrefetchHelper (the producer) ──
   BranchPrefetchHint @0xcfe8  (registered callback @0x1d70)
        └─ resolve_hint_decision 0xcf90  →  arm 0x15128 :
              decision==0 (TAKEN) → ARM  : hint[+0]=1, hint[+1]=decision
              decision!=0 (NT)    → DISARM: hint[+0]=0  ("preserve legacy behavior")

  ── fetch front-end (the consumer) — RTL_PC_check_delta ~0x326e ──
   PC lands in hinted branch's block  →  0x6ec4 armed? → 0x15010 "target in cache?" predicate
        target in NEXT block (PrefetchHelper::check 0x15208) AND in-bounds (0x68d0 @0x34de):
            PUSH: descr[+56] |= (1<<target_idx)   via 0x5b84    →   "Pushing cache idx=…"
                  └─ cache's replace→start_fill_siram→DMA fills the line EARLY (latency hidden)
        out-of-bounds → 0x3513 "WARNING: …prefetch_pc…out-of-bounds…skipping it"
```

The whole thing is a **declarative scheduling of a DMA fill**: the prefetch never issues a DMA
itself and never bypasses the cache's per-line validity gate, so a mispredicted or
not-yet-filled line can never be executed (see
[§9 Correctness guard](#9-correctness-guard--why-prefetch-can-never-corrupt-execution--high)).

---

## 2. String anchors — the module + log oracle

Source-file / symbol tags in the DEBUG DRAM image (`VA = DRAM_file_off + 0x80000`):

| DRAM VA | string |
|---|---|
| `0x82bb0` | `/opt/workspace/NeuronUcode/src/decode/branch.cpp:75 0`  *(assert site)* |
| `0x82c64` | `branch_prefetch_hint.cpp` |
| `0x82c7d` | `resolve_hint_decision` |

Branch / prefetch log strings, each with the IRAM `const16` low-half that loads it (the high
half is `const16 …,8` → DRAM page `0x80000` for all of them):

| DRAM VA | `const16` low | loaded at | string |
|---|---|---|---|
| `0x82be6` | `0x2be6` | `0xc930` | `S: BRANCH` |
| `0x82c13` | `0x2c13` | `0xca61` | `S: BRTAKEN: %d` |
| `0x82c93` | `0x2c93` | `0xcff4` | `S: BranchPrefetchHint` |
| `0x82d90` | `0x2d90` | `0xd224` | `S: GetSequenceBounds` |
| `0x83cf4` | `0x3cf4` | `0x151b4` | `S: Arming branch_hint with {br_addr=0x%llx, br_target=0x%llx, decision=0x%x}` |
| `0x83d42` | `0x3d42` | `0x151cb` | `S: Branch is marked NT, invalidating branch_hint to preserve legacy behavior` |
| `0x83d90` | `0x3d90` | `0x151f7` | `S: disarming branch_hint` |
| `0x83c49` | `0x3c49` | `0x152f6` | `S: PrefetchHelper : Taken branch addr(0x%llx) is in next cache block range [0x%llx, 0x%llx], prefetching branch target 0x%llx` |
| `0x83cc8` | `0x3cc8` | `0x15353`\* | `S: PrefetchHelper : Invalidating all hints` |
| `0x81b3b` | `0x1b3b` | `0x332e` | `S: Current PC(0x%llx) is in same block as hinted branch(0x%llx), checking if target(0x%llx) is in cache` |
| `0x81ba4` | `0x1ba4` | `0x3345` | `S: Updated values from previous RTL_PC_check_delta : curr_dma_idx_vld=0x%x, tgt_cache_idx=0x%x` |
| `0x81e2a` | `0x1e2a` | `0x35e8` | `S: Pushing cache idx=0x%x, pushed=0x%x` |
| `0x81dd6` | `0x1dd6` | (front-end) | `S: Push? next_vld=0x%x, next_cache_idx=0x%x, cache_idx_pushed=0x%x, idx_state=0x%x` |
| `0x81c04` | `0x1c04` | (front-end) | `S: Evict? next_dma_cache_idx=0x%x, ok_to_evict=0x%x, curr_dma_idx_vld=0x%x, curr_idx=0x%x, cache_idx_pushed=0x%x` |
| `0x81d51` | `0x1d51` | `0x3513` | `WARNING: ok_to_evict's prefetch_pc 0x%llx is out-of-bounds [0x%llx, 0x%llx], skipping it.` |
| `0x81efb` | `0x1efb` | `0x85fd` | `S: INS_FL` *(instruction-flush handler → invalidate-all)* |
| `0x8224e` | `0x224e` | `0xa348` | `S: Halt` *(halt handler → invalidate-all)* |

\* The `const16` low for "Invalidating all hints" is `0x3cc8` (loaded inside the invalidate-all
emitter); `0x15353` is the IRAM offset of the log emission. All `const16` sites were confirmed
present in the disassembly. Every log call goes through the printf-style emitter **`0x18b84`**
(verify: `c933`, `ca64`, `cff7`, `151b7`, `151ce`, `151fa`, `152f9`, `3339`, `3348`, `35ed` all
`call8 0x18b84`). `[HIGH/OBSERVED — every string read byte-exact from `dram.bin`; every
`const16` low-half confirmed in `iram.asm`.]`

> **NOTE — the `S:` prefix is the engine's log channel tag.** Most strings begin `S: …`; the
> two without it (`resolve_hint_decision`, the `branch.cpp:75` path) are **assert arguments**
> (symbol name + file:line), not log lines, which is exactly how the recovered C++ identities
> are anchored.

---

## 3. The BRANCH opcode handler  `branch.cpp @0xc7fc`  `[HIGH skeleton / MED FLIX body]`

`BRANCH` is dispatched as a C++ handler object: the SEQ dispatch map (opcode table →
trampoline → `Handler`-vtable `execute()`, detailed in the
[fetch front-end's §5 dispatch handoff](fetch-pc-redirect.md#5-the-dispatch-handoff--table--trampoline--handler-vtable))
resolves the `BRANCH` opcode byte to the handler object whose `vtable[0]` is `execute() @0xc7fc`.

> **CORRECTION — the exact `'S:'` opcode *byte* for `BRANCH` is not resolved here.** The
> handler-object *identity* is `[HIGH/OBSERVED]` (it loads `"S: BRANCH"` and `"S: BRTAKEN: %d"`),
> but the dispatch-map entry that maps a specific opcode byte to `vtable[0]=0xc7fc` is
> dispatch-hub territory and was not present in the recovered map. `0xc7fc` is reached as a
> virtual `execute()` — and recursively for its own sub-opcodes (§3.1) — consistent with the
> handler-object model. `[identity HIGH/OBSERVED; opcode byte NOT RESOLVED.]`

### 3.1 Sub-opcode dispatch — an 11-way jump table

The handler reads a **14th instruction byte** (`instr[+14]`) as a *sub*-opcode and jumps
through an 11-entry table:

```c
// branch.cpp execute()  @0xc7fc  (instruction-exact skeleton)
void BRANCH_execute(handler_ctx *a1) {        // c7fc: entry a1,64
    instr_t *ins = a1->decoded_instr;         // c801: l32i.n a2,[a1+4]
    uint8_t sub = ins->byte[14];              // c803: l8ui  a2,[a2+14]
    // c811: const16 a3,8 ; c814: const16 a3,0x2b84   →  &jumptab = 0x82b84  (HI:LO const16 pair)
    void *tgt = ((void**)0x82b84)[sub];       // c817: addx4 a2,a2,a3 ; c81a: l32i.n a2,[a2+0]
    goto *tgt;                                 // c81c: jx a2
}
```

> **CORRECTION — the table address is built by a `const16` *pair*, not a single op.** A naive
> reader renders `c811` as one `const16 a3,0x82b84`. The real disassembly is **two** const16:
> `c811: const16 a3,8` (high halfword → `0x8_____`) then `c814: const16 a3,0x2b84` (low halfword)
> — the standard Xtensa idiom for a 32-bit immediate, assembling `0x00082b84`. Every `0x8____`
> DRAM address on this page is built this way; the high half is always `const16 …,8`.
> `[HIGH/OBSERVED.]`

The 11-entry table at DRAM `0x82b84` was decoded byte-exact (little-endian 32-bit words). It is
immediately followed by the `branch.cpp:75` assert string, which is why the table is exactly 11
entries long:

| entry | VA | target | entry | VA | target |
|---|---|---|---|---|---|
| 0 | `0x82b84` | `0xc821` | 6 | `0x82b9c` | `0xcba2` |
| 1 | `0x82b88` | `0xc83b` | 7 | `0x82ba0` | `0xcbaa` |
| 2 | `0x82b8c` | `0xc869` | 8 | `0x82ba4` | `0xcbb2` |
| 3 | `0x82b90` | `0xc899` | 9 | `0x82ba8` | `0xcbba` |
| 4 | `0x82b94` | `0xc8cb` | 10 | `0x82bac` | `0xcbc2` |
| 5 | `0x82b98` | `0xcb9a` | — | `0x82bb0` | *(`branch.cpp:75` string starts here)* |

The first cluster (`0xc821`…`0xc8cb`, sub-opcodes 0–4) reads operand bytes `instr[+34]`/`[+35]`
and calls **`0x4c3c`** — the 64-bit target-PC getter (the same getter the
[PC-redirect path](fetch-pc-redirect.md#4-the-pc-redirect-execution-machine-0x5750) uses) — to
materialise the branch target. The second cluster (`0xcb9a`…`0xcbc2`, sub-opcodes 5–10) are
the simpler variants. An out-of-range sub-opcode hits the **assert** path:
`c905`/`c908` → `LOG branch.cpp:75` (string `0x82bb0`) → `call8 0x18c94` (abort).
`[HIGH/OBSERVED — table bytes, the `0x4c3c` operand-read cluster, and the assert site.]`

### 3.2 The decision — `BRANCH` + `BRTAKEN` log + the 3-way type switch  `[HIGH anchors / MED predicate]`

```c
    // c930: const16 a10,0x82be6 ; c933: call8 0x18b84    →  LOG "S: BRANCH"
    // c93c: const16 a2,8 ; c93f: const16 a2,0x20c8       →  a2 = &g_central_state_ptr (0x820c8)
    //        (g_central_state_ptr → 0x000855a0, the SEQ central state struct)
    int32_t k = a1->field28;                  // c982: l32i.n a2,[a1+28]
    if (k == 0) goto one_result;              // c984: beqz.n a2, 0xc991
    // 3-WAY branch-type switch:
    //   c989: addi.n a2,a2,-1 ; c98b: bltui a2,2,0xc999
    if ((uint32_t)(k - 1) < 2) goto resolve_substate;   // k ∈ {1,2}  → 0xc999
    else goto default_path;                   // c98e: j 0xca50  (other)
  one_result:                                 // c991:
    a1->taken = 1;                            // c993: s8i a2(=1),[a1+52]
    goto eval_done;                           // c996: j 0xca54
  resolve_substate:                           // c999:
    a10 = a1->byte56;                         // c999: l8ui a10,[a1+56]
    a10 = call8(0xcb78, a10);                 // c99c: call8 0xcb78
    a1->byte60 = a10;                         // c99f: s8i a10,[a1+60]
    // c9a2..ca2c: evaluate the branch CONDITION
    //   reads operand bytes instr[+32]/[+33] and the predicate at instr[+13],
    //   calls 0x6eec (poll-helpers) and 0xbd28 → producing the taken bool into [a1+52]
    // ca61: const16 a10,0x82c13 ; ca64: call8 0x18b84  →  LOG "S: BRTAKEN: %d"  (resolved bool)
```

So the branch **condition is software-evaluated** from the decoded operand fields plus engine
state, producing a taken/not-taken boolean that is then logged via `"S: BRTAKEN: %d"`.

> **NOTE — `[a1+52]` is the canonical "taken" byte.** It is set to `1` in the one-result path
> (`c993`) and computed in the condition path; the taken-branch tail (§3.3) keys off it. The
> exact predicate arithmetic at `c9c3`/`ca20` co-issues with FLIX/IVP slots and is left as MED;
> the *structure* (software-eval → `[a1+52]` taken bool → `BRTAKEN` log) is `[HIGH/OBSERVED]`.
> The 3-way switch (`addi.n -1; bltui ,2`) is exact: raw `field28` value `0` → one-result,
> `{1,2}` → resolve-substate, anything else → default. `[switch HIGH/OBSERVED; predicate MED/INFERRED.]`

### 3.3 The taken-branch tail — redirect + arm hint  `0xca7d..0xcb74`

This is the join point with the [PC-redirect machine](fetch-pc-redirect.md) and the prefetch
producer. It first reads the **prefetch-enable flag**, then redirects, then (if a hint is
armed) computes the target, derives a decision bool, and **disarms the consumed hint**:

```c
    // ── prefetch-enable flag test ──
    // ca7d: const16 a2,8 ; ca80: const16 a2,0x564c  →  &PREFETCH_ENABLE = 0x8564c
    uint8_t pf_en = *(uint8_t*)0x8564c;       // ca83: l8ui a2,[a2+0]
    if ((pf_en & 1) == 0) goto legacy;        // ca86: bbci a2,0,0xcac8   (bit0 clear → legacy)

  prefetch_enabled:                           // ca89
    call8(0xc7fc, …);                         // ca8f: re-enter sub-dispatch / recompute
    // cabe: const16 a10,0x5068 (redirect-state ptr) ; cac1: call8 0x8660  (redirect helper)
    goto arm_tail;                            // cac4: j 0xcaf0 → 0xcb15

  legacy:                                     // cac8
    call8(0x3cdc, …);                         // cad2
    call8(0xc7fc, …);                         // cad8
    // cae5: const16 a10,0x5068 ; caea: call8 0x5750   ←── PC-REDIRECT MACHINE
    //        (fetch-pc-redirect §4: writes the new 64-bit PC → CSR 0x1060/0x1080)
    goto arm_tail;                            // caed: j 0xcaf0 → 0xcb15

  arm_tail:                                   // cb15..cb74  (common to both paths)
    // cb18: const16 a10,0x55e0 ; cb1b: call8 0x6ec4    →  read hint-armed bit
    int armed = (*(uint8_t*)0x855e0) & 1;     //   0x6ec4 = l8ui [a2+0]; extui ,0,1
    if (!armed) goto done;                    // cb20: beqz a10,0xcb5c

    // cb2f: call8 0x6f14 (hint getter) ; cb38: call8 0x4c3c  → COMPUTE 64-bit branch TARGET PC
    a1->tgt_lo = a10; a1->tgt_hi = a11;       // cb3d: s32i.n a10,[a1+40] ; cb3b: s32i.n a11,[a1+44]
    call8(0x1250c, …); call8(0x5b74, …);      // cb43/cb46: redirect-state helpers
    // cb49: xor a3,a3,a11 ; cb4c: xor a2,a2,a10 ; cb4f: or a2,a2,a3 ; cb52: movi a3,1 ; cb54: saltu a2,a2,a3
    int decision = (target == fall_through);  //   ←── 64-bit equality → 0/1 DECISION bool  (INFERRED)

    // cb68: const16 a10,0x55e0 ; cb6b: call8 0x151dc   ←── PrefetchHelper DISARM (consumes the hint)
  done:                                       // cb5c
    return;                                   // cb74: retw.n
```

The `cb49`–`cb54` sequence is exact in its ops: `xor a3,a3,a11` and `xor a2,a2,a10` zero the
two halves of `target XOR fall_through` (64-bit), `or a2,a2,a3` folds them, and
`saltu a2,a2,1` yields `1` if the OR is zero — i.e. a **64-bit equality** of the two PCs giving
a `0`/`1` decision class. The precise "target vs fall-through" interpretation is `[MED/INFERRED]`
across the FLIX-desynced operand loads; the arithmetic itself is `[HIGH/OBSERVED]`.

> **QUIRK — the taken path re-enters `0xc7fc` (the handler) before redirecting.** Both the
> enabled (`ca8f`) and legacy (`cad8`) branches `call8 0xc7fc` *again*. This is the handler
> recursing into its own sub-dispatch to recompute / commit the resolved sub-state before the
> PC is rewritten — not a bug. The actual PC write is the *single* `call8 0x5750` (legacy) or
> `call8 0x8660` (enabled redirect helper). `[HIGH/OBSERVED — both call sites and the single
> redirect call.]`

> **GOTCHA — the consumed hint is disarmed *here*, not in `PrefetchHelper`.** `cb6b: call8
> 0x151dc` is the disarm of the hint that *drove this branch*: once the redirect commits, the
> hint is spent and its armed bit (`state[0x855e0+0]`) is cleared. A reimplementation that arms
> a hint but never disarms it on redirect-commit will leak a stale armed bit into the next
> branch. `[HIGH/OBSERVED.]`

---

## 4. The prefetch-hint producer  `branch_prefetch_hint.cpp`

### 4.1 The hint record + the hint table

The single *active* branch hint lives in the **central state struct** (base `0x855e0`):

```
  state[0x855e0 + 0]  = hint-ARMED byte   (1 = armed, 0 = disarmed)   ← read by 0x6ec4
  state[0x855e0 + 1]  = decision byte     (the resolved NT/T class)   ← written by arm (§4.4)
  state[0x855e0 +24]  = the CACHE DESCRIPTOR base  (iram-cache page; see GOTCHA below)
```

In addition there is a per-cache-block hint **table** at DRAM `0x85f88`:

```
  0x85f88 + 0            : 32-byte header
  0x85f88 + 32 + k*56    : entry k   (k = 0..15),  56 bytes (0x38) each,  16 entries
  entry[+48]             : per-entry "valid/pushed" flag  (cleared by invalidate-all, §4.7)
```

The `1`/`0` armed byte at `state[+0]`, the `16 × 56-byte` table from `0x85f88+32`, and the
`[+48]` clear are all instruction-exact (§4.4, §4.7). The interior of the 56-byte entry beyond
`[+48]` is not field-named (`[MED/INFERRED]`).

> **GOTCHA — the cache descriptor base is `central_state + 24`, *not* the central state base.**
> The `descr[+56]` *pushed* bitmask that the prefetch sets lives at `0x855e0 + 24 + 56`. Verify:
> the read primitive `0x6f24` does `const16 a2,0x855e0 ; addi a2,a2,24 ; l32i.n a2,[a2+56]`, and
> the clear `0x6f38` does the same `addi a2,a2,24` before touching `+56`. The armed *byte* is
> at `state[+0]` (offset 0); the *pushed bitmask* is at `descr[+56]` = `state[+24+56]`. Confusing
> the two bases is a real reimplementation trap. `[HIGH/OBSERVED — `addi …,24` in both 0x6f24
> and 0x6f38.]`

### 4.2 `resolve_hint_decision @0xcf90`

A 3-way classifier that maps a decision *class* `∈ {0,1}` to a resolved decision byte; any other
value is a **fatal assert** (`branch_prefetch_hint.cpp:45`):

```c
uint8_t resolve_hint_decision(uint8_t cls/*[a1+28]*/, uint8_t b/*[a1+24]*/, uint8_t c/*[a1+20]*/) {
    // cf93: s8i cls,[a1+28] ; cf96: s8i b,[a1+24] ; cf99: s8i c,[a1+20]
    uint8_t k = cls;                          // cf9c: l8ui a2,[a1+28]
    if (k == 0) { /* pick path A */ }          // cf9f: beqz.n a2,0xcfac
    else if (k == 1) { /* pick path B */ }     // cfa4: beqi a2,1,0xcfb5
    else {                                     // cfa7: j 0xcfc5
        // cfcc: const16 a10,0x82c7d ("resolve_hint_decision")
        // cfd2: const16 a11,0x82c64 ("branch_prefetch_hint.cpp")
        // cfd5: movi a12,45 ;  cfdd: call8 0xa304     ←── FATAL ASSERT  (file:line 45)
    }
    return a1->resolved/*[a1+16]*/;            // cfe0: l8ui a2,[a1+16] ; cfe3: retw
}
```

The two valid cases pick which of the two source bytes (`[a1+24]`/`[a1+20]`) becomes the
resolved decision at `[a1+16]`. The 3-way structure and the **line-45 assert** are
`[HIGH/OBSERVED]` (the `movi a12,45` is literal); the exact source-byte pick is FLIX-desynced
at `0xcfaa`–`0xcfc4` (`[MED/INFERRED]`). `[HIGH on assert; MED on pick.]`

### 4.3 `BranchPrefetchHint @0xcfe8` — the registered callback  `[HIGH head / MED arm-edge]`

`BranchPrefetchHint` is **registered as a function pointer** rather than directly called: the
registration site `0x1d70` does `const16 a2,0xcfe8 ; s32i a2,[a1+12] ; call8 0x85c4`
(registration thunk) — so it is a **hook** invoked by the branch-decode path.

```c
void BranchPrefetchHint(/* hint inputs in a1 frame */) {   // cfe8: entry a1,112
    // cff4: const16 a10,0x82c93 ; cff7: call8 0x18b84  →  LOG "S: BranchPrefetchHint"
    a1->b44 = …; a1->b40 = …; a1->b36 = …;     // d007/d011/d01b: stage the 3 decision-class bytes
    uint8_t resolved = resolve_hint_decision(a1->b44, a1->b40, a1->b36);   // d027: call8 0xcf90
    a1->b32 = resolved;                        // d02a: s8i a10,[a1+32]
    call8(0xd0f4, …);                          // d037: setter  (record br_addr)
    call8(0xd114, …);                          // d066: setter  (record br_target)
    // … through the resolved decision, ARMS the hint via PrefetchHelper::arm (§4.4)
}
```

The head (log + `resolve_hint_decision` call + the two setters) is `[HIGH/OBSERVED]`. The
**call edge from `BranchPrefetchHint` to `arm @0x15128`** is across a FLIX/`call0` desync span
(the `0x150ec`/`0x15128` thunks have no aligned data-pool xref and are reached via the hook +
mis-framed bundle slots). The `arm` *function* is decoded exactly (§4.4); but the precise caller
instruction is `[MED/INFERRED]` — the data-flow proof is that **the armed byte `arm` writes is
exactly the one `BRANCH`'s tail reads via `0x6ec4`** (`state[0x855e0+0]`).

### 4.4 `PrefetchHelper::arm @0x15128`  (thunk `0x150ec`)

The arm is **decision-gated**: a *taken* branch (decision byte `0`) arms; a *not-taken* branch
(decision `!= 0`) **disarms** to "preserve legacy behavior" (fall back to plain PC-redirect, no
prefetch):

```c
void PrefetchHelper_arm(hint_t *a1) {          // 15128: entry a1,64
    hint_rec_t *rec = a1->hint_ptr;            // 1512b: s32i.n a2,[a1+8]  (… 1514c: l32i.n a2,[a1+8])
    uint8_t decision = a1->decision_byte;      // 1512d: s8i a3,[a1+12]    (… 1514e: l8ui a3,[a1+12])
    if (decision != 0) goto mark_NT;           // 15151: bnez a3, 0x151c0

    // ── decision == 0  (TAKEN → ARM) ──
    rec->armed = 1;                            // 15158: movi.n a3,1 ; 1515a: s8i a3,[rec+0]
    rec->decision = a1->decision_byte;         // 1515d: l8ui a3,[a1+12] ; 15160: s8i a3,[rec+1]
    // 151b4: const16 a10,0x83cf4 ; 151b7: call8 0x18b84
    //        LOG "Arming branch_hint with {br_addr, br_target, decision=0x%x}"
    return;

  mark_NT:                                     // 151c0  (decision != 0 → DISARM)
    rec->armed = 0;                            // 151c0: movi.n a3,0 ; 151c2: s8i a3,[rec+0]
    // 151cb: const16 a10,0x83d42 ; 151ce: call8 0x18b84
    //        LOG "Branch is marked NT, invalidating branch_hint to preserve legacy behavior"
}
```

The `s8i 1/0` to `rec[+0]`, the `s8i decision` to `rec[+1]`, and the `bnez`-on-decision split
are instruction-exact. `[HIGH/OBSERVED.]`

> **QUIRK — "decision byte `0` = TAKEN = ARM".** The polarity is the opposite of what a naive
> reader expects: a *zero* decision arms the prefetch. This is because the decision byte encodes
> the *class* resolved by `resolve_hint_decision` (class `0` is the taken/prefetchable case);
> the `BRTAKEN` boolean at `[a1+52]` (§3.2) is a *separate* representation. `[HIGH/OBSERVED.]`

> **NOTE — a real IVP bundle sits at `0x151a9`.** Re-disassembled from an aligned boundary it is
> `{ addmi a11,a12,0x900 ; nop ; nop ; ivp_dselnx16t v6,v0,v4,v18,v5,vb4 }` — a 4-slot FLIX
> bundle the linear pass mis-frames. It is *not* control-flow relevant (it co-issues with the
> log-string setup); the scalar `const16`/`call8` log emission is exact. This is the FLIX desync
> the §0 GOTCHA warns about, caught in the act. `[HIGH/OBSERVED on the scalar slots.]`

### 4.5 `PrefetchHelper::disarm @0x151e8`  (thunk `0x151dc`)

Single-hint teardown — clears the armed bit. This is what the `BRANCH` taken-tail calls
(`cb6b`) to consume the hint after its redirect commits:

```c
void PrefetchHelper_disarm(hint_rec_t *rec/*[a1+12]*/) {  // 151e8: entry a1,48
    // 151f7: const16 a10,0x83d90 ; 151fa: call8 0x18b84   →  LOG "S: disarming branch_hint"
    rec->armed = 0;                            // 15200: movi.n a3,0 ; 15202: s8i a3,[rec+0]
    return;                                    // 15205: retw.n
}
// thunk 0x151dc: stores a2→[a1+12], reloads, then call8 0x151e8  (the windowed-ABI wrapper)
```

`[HIGH/OBSERVED — the `s8i 0,[rec+0]` and the thunk.]`

### 4.6 `PrefetchHelper::check + prefetch_target @0x15208`  `[HIGH skeleton / MED FLIX body]`

When the hint is armed **and** the taken-branch address falls in the *next* cache-block range,
this calls the cache-push predicate `0x15010` (§5) to pre-stage the branch-target block:

```c
void PrefetchHelper_check(/* tuple in a1 frame */) {   // 15208: entry a1,96
    uint64_t br_addr   = (a1->b108, a1->b104);  // 1520b: l32i a3,[a1+108] ; 1520e: l32i a8,[a1+104]
    uint64_t blk_range = (a1->b100, a1->b96);   // 15211: l32i a10,[a1+100]; 15214: l32i a9,[a1+96]
    cache_t *cache = a1->b20;                   // 15217: s32i.n a2,[a1+20]
    call8(0x1abe8, …); call8(0x1ac10, …);       // 1521d/15247: 64-bit range/compare helpers

    uint8_t armed = cache->armed_byte;          // 15268: l8ui a3,[a2+0]
    a10 = 0;                                     // 1526b: movi.n a10,0
    if ((armed & 1) == 0) goto skip;            // 1526d: bbci a3,0,0x15298   ←── armed? else skip

    bool in_cache = cache_push_predicate(cache);// 15292: call8 0x15010       ←── CACHE-PUSH (§5)

    // 152d8: addi a5,a3,-64 ; 152db: saltu a3,a5,a3 ; 152de: addi.n a4,a4,-1 ; 152e0: add.n a3,a4,a3
    uint64_t prefetch_target = …;               //   64-bit target-PC arithmetic (−64 = one block)
    // 152f6: const16 a10,0x83c49 ; 152f9: call8 0x18b84
    //        LOG "Taken branch addr is in next cache block range [lo,hi], prefetching branch target 0x%llx"
  skip: ;
}
```

> **NOTE — `0x15298` is a 5-slot IVP bundle.** Re-disassembled from boundary it is
> `{ bbci.w15 a3,23,… ; extui a14,a13,6,6 ; ivp_mul4t2n8xr8 wv0,… ; ivp_dextrprn_2x32 pr0,… ;
> ivp_sel2nx8i_s4 v0,… }` — heavy FLIX/IVP co-issue. The `armed?`-test → `0x15010`-push and the
> "prefetching branch target" log are exact; the interior 64-bit range/target arithmetic is
> `[MED/INFERRED]` across the IVP slots. The `addi a5,a3,-64` (subtract one 64-byte block in the
> low half) is `[HIGH/OBSERVED]`. `[skeleton + 0x15010 call + log HIGH; range/target math MED.]`

### 4.7 `PrefetchHelper::invalidate_all_hints @0x1536c`  (thunk `0x15344`)

Bulk-clears the per-entry `[+48]` valid/pushed flag across all 16 hint-table entries:

```c
void PrefetchHelper_invalidate_all(void) {     // 1536c: entry a1,48
    // 1536f: const16 a2,0x85f88 ; 15379: addi a2,a2,32   →  first entry (skip 32-byte header)
    uint8_t *e   = (uint8_t*)(0x85f88 + 32);
    uint8_t *end = e + 0x380;                   // 1537c: movi a3,0x380 ; 1537f: add a3,a2,a3  (0x380 = 16*0x38)
    do {                                        // 15384:
        clear_entry(e);                         //   15386: call8 0x15394 → 0x153a0:  s8i a4(=0),[e+48]
        e += 56;                                //   15389: addi a2,a2,56   (stride 0x38)
    } while (e != end);                         //   1538c: bne a2,a3,0x15384   (16 iterations)
}
// per-entry  0x153a0:  e->b48 = 0;   // 153a3: s32i.n a2,[a1+12] ; 153a7: movi a3,… ; 153b3: s8i a4(=0),[a2+48]
```

The companion single-hint variant (thunk `0x15344` → `0x151e8`) clears the central armed byte;
**together** = "invalidate ALL hints". The base `0x85f88`, the `+32` header skip, the
`16 × 56-byte` stride, and the `[+48]=0` store are all instruction-exact. `[HIGH/OBSERVED.]`

---

## 5. The cache-push predicate  `0x15010`  ("is the target block in / pushable to cache?")  `[HIGH skeleton]`

This is the single primitive both consumers (the front-end and `PrefetchHelper::check`) call to
decide whether the branch-target block can be pushed. **It returns a bool and issues no DMA
itself.**

```c
bool cache_push_predicate(cache_t *a1) {       // 15010: entry a1,80
    a1->save = a2;                             // 15013: s32i a2,[a1+4]   (a2 = target PC / cache ptr)
    call8(0x1a9e4, …); call8(0x1a9f0, …);      // 15019/15027: 16-bit field extract + block-align getters
    // … call0 0x7859c / 0x785cc : 64-bit address compare helpers (compiler-rt-style, off-image)
    // 1509f: l32i a6,[a0+0x110] ; s32i a6,[a0+0x10c]   (state-window read/write)
    int a2 = 0; … ; return movnez(a2, a3, a6); // 150a6/150dd: movnez a2,a3,a6  →  BOOL in a2
}
```

> **CORRECTION — `0x15010` issues NO cache fill. Verified by exhaustive grep.** A naive reading
> assumes "push" = "fill". It is **not**. Across the whole prefetch span `0x15010..0x15420`,
> there is **zero** call to any fill-chain function — `replace` (`0x6068`), `start_fill_siram`
> (`0x6217`), `DramRingDMA` enqueue (`0x6354`), `wait_for_cache_line` (`0x5cd0`), or
> `fetch_cache_line` (`0x5a98`). The push primitive is a *pure predicate*: "is the target block
> already in / pushable to the cache?" returning a bool. The actual fill is issued **later** by
> the fetch front-end's `Push?` path (§6) and the cache's own DMA chain. `[HIGH/OBSERVED — the
> absence-of-fill-calls is a verified grep over the entire prefetch span; the predicate's
> interior `0x7859c`/`0x785cc` arithmetic is across off-image compiler-rt helpers, MED.]`

> **NOTE — the `call0 0x28140` / `0x7859c` / `0x785cc` targets are *beyond* the 0x1c820 image.**
> They are not in-image functions; they are the FLIX-mis-framed `call0` *slots* whose decoded
> targets land past the 114-KiB IRAM (the 64-bit compare/div helpers are linked outside the
> carved blob). They confirm — rather than contradict — the desync note: the *scalar* skeleton
> (entry, the two `call8` getters, the `movnez` return) is exact. `[HIGH/OBSERVED on skeleton.]`

---

## 6. Prefetch ↔ cache interplay — the `RTL_PC_check` / `Push?` consumer  `[HIGH anchors / MED FLIX body]`

The *consumer* of the hint lives in the fetch front-end (the `check_fetch` /
`RTL_PC_check_delta` body, a FLIX-desync span shared with the
[IRAM-cache page's §9 fetch front-end interaction](iram-cache.md#9-fetch-front-end-interaction--fetchhpp-check_fetch--rtl_pc_check--head-high--body-med)).
Recovered exactly at the call/log anchors:

```c
// ~0x326e RTL_PC_check_delta : tracks curr_dma_idx_vld / tgt_cache_idx / curr_pc
    int armed = call8(0x6ec4);                 // 329d: call8 0x6ec4 (read hint-armed bit)
    if (!armed) goto no_hint;                  // 32a0: beqz.n a10,0x32cd
    // 32bf: const16 a10,0x855e0 ; 32c5: call8 0x15010   ←── CACHE-PUSH predicate (§5)
    int target_in_cache = a3 = a10;            // 32c8: mov.n a3,a10
    if (target_in_cache & 1) goto staged;      // 32cd: bbsi a3,0,0x32d3   (target IN cache? bit0)
    // 332e: const16 a10,0x81b3b ; 3339: call8 0x18b84
    //        LOG "Current PC is in same block as hinted branch, checking if target is in cache"
    // 3345: const16 a10,0x81ba4 ; 3348: call8 0x18b84
    //        LOG "Updated values from previous RTL_PC_check_delta : curr_dma_idx_vld, tgt_cache_idx"
```

The **`Push?` / `Evict?`** staging (the front-end actually marking the pushed line) — its
bitmask ops, decoded exactly:

```c
// 35c1: const16 a4,0x85654 ; 35c4: l32i.n a4,[a4+0] ; 35c6: srli a4,a4,6   →  line index = block_size>>6
// 35d8: addi a10,a2,24                                          →  descr base = central_state + 24
    mark_pushed(descr, idx);                   // 35db: call8 0x5b84   →  descr[+56] |= (1<<idx)
    uint32_t pushed = read_pushed(descr);      // 35e0: call8 0x6f24   →  READ descr[+56]
    // 35e8: const16 a10,0x81e2a ; 35ed: call8 0x18b84  →  LOG "Pushing cache idx=0x%x, pushed=0x%x"
```

The three `descr[+56]` bitmask primitives (instruction-exact):

```c
// SET   @0x5b84 :  descr[+56] |= (1 << idx)
void mark_pushed(cache_desc_t *desc/*a2*/, uint32_t idx/*a3*/) {
    uint32_t bit = (1u << idx);                // 5b8f: movi.n a4,1 ; 5b91: ssl a3 ; 5b94: sll a3,a4
    desc->pushed |= bit;                       // 5b97: l32i.n a4,[a2+56] ; 5b99: or ; 5b9c: s32i.n a3,[a2+56]
}
// READ  @0x6f24 :  return descr[+56]   (descr = 0x855e0 + 24)
// 6f2a: const16 a2,0x855e0 ; 6f2d: addi a2,a2,24 ; 6f34: l32i.n a2,[a2+56]
// CLR   @0x6f38 :  descr[+56] &= ~(1 << idx)
// 6f41: addi a3,a3,24 ; 6f4c: movi.n a4,-2 ; 6f4e: ssl a3 ; 6f51: src a3,a4,a4 ; 6f56: and ; 6f59: s32i.n [a2+56]
```

**The latency-hiding model, end-to-end (`[OBSERVED]`):**

1. A **taken** branch arms a hint `{br_addr, br_target, decision}` (§4.4); armed bit at
   `state[0x855e0+0]`.
2. As the fetch PC advances and lands in the **same** cache block as the hinted branch
   (`RTL_PC_check_delta`, log `0x332e`), it checks whether the branch *target*'s block is in
   cache (`call 0x15010`, §5).
3. If the target is in the **next** cache block (`PrefetchHelper::check 0x15208`, range check,
   log `0x152f6`) and not already pushed, the front-end **pushes** that block:
   `descr[+56] |= 1<<target_idx` (`0x5b84`) — marking it as a line the cache must eagerly bring
   resident.
4. The cache's normal `replace → start_fill_siram → DMA` chain (the
   [IRAM-cache fill path](iram-cache.md#7-fill--refill--dma-from-the-backing-s-stream--highobserved)) then
   fills that pushed line **by DMA while the SEQ engine keeps executing the current block** —
   the DMA latency is overlapped with execution of the in-flight block, *before* the taken
   branch actually redirects the PC into it.

`[HIGH/OBSERVED on the call/log/bitmask anchors and the data-flow; the precise per-iteration
bundle ordering in the `0x326e` body is the shared FLIX-desync span, MED.]`

> **NOTE — there are exactly two `0x15010` callers and two `0x6ec4` callers.** Grounded by
> count on the binary (`rg -c 'call8\t0x15010' = 2`, `'call8\t0x6ec4' = 2`): the push-predicate
> is called from `0x32c5` (front-end `RTL_PC_check`) and `0x15292` (`PrefetchHelper::check`); the
> armed-bit read from `0xcb1b` (the `BRANCH` tail) and `0x329d` (front-end). No other consumers
> exist. `[HIGH/OBSERVED.]`

---

## 7. The prefetch-enable flag and central-state globals  `[HIGH addrs / MED setter]`

| DRAM VA | role | how reached |
|---|---|---|
| `0x8564c` | **PREFETCH-ENABLE** flag (bit0) — gates the taken-path redirect-helper vs legacy split | `l8ui` @`0xca7d`, `0xcaf4` |
| `0x820c8` | ptr to central engine state — holds value **`0x000855a0`** (read from `dram.bin`) | `const16` @`0xc93c` |
| `0x855e0` | **central SEQ state struct** — `+0` armed byte, `+1` decision, `+24` = cache descriptor | `const16` @`0xcb18`, `0x6ec4`, … |
| `0x85f88` | branch-hint **table** — 32-byte header + 16 × 56-byte entries (`[+48]` valid/pushed) | `const16` @`0x1536c` |
| `0x85654` | `block_size` word — `>>6` gives the line index | `l32i.n` @`0x35c1` |
| `0x82b84` | branch sub-opcode jump table (11 entries) | `addx4`+`jx` @`0xc817` |

All these `0x8____` DRAM globals are zeroed in the carved image (`.bss`-style; runtime-populated)
**except** `0x820c8`, which carries the static initializer `0x000855a0` — confirming the
central-state base. `[addresses HIGH/OBSERVED.]`

> **CORRECTION — the `0x8564c` PREFETCH-ENABLE *setter* is not in IRAM.** The flag is **read** in
> the recovered spans (`0xca7d`, `0xcaf4`) but no `const16 …,0x564c` *store* exists anywhere in
> the IRAM image — it is set via a pointer alias or a host/CSR config write outside the carved
> firmware. Its **role** (gate the branch-hint redirect path) is `[HIGH/OBSERVED]`; its writer is
> `[MED/INFERRED]` (host/config). `[role HIGH; setter NOT RESOLVED.]`

> **NOTE — `GetSequenceBounds @0xd224` is a table-neighbor, not part of the branch core.** Its
> handler reads the sequence-bounds global at DRAM **`0x82dc0`** (built at `0xd25b` by
> `const16 a2,8 ; const16 a2,0x2dc0`, then dereferenced by the 8-word `l32i.n` struct copy at
> `0xd25e`). It shares the decode module but is *not* on the branch/prefetch control path.
> `[HIGH/OBSERVED — the `0xd224` head and the `0x82dc0` bounds-global anchor; the bounds-struct
> fields are runtime, MED.]`
>
> **CORRECTION — the bounds global is `0x82dc0`, not `0x82d80` (this page's earlier value was
> wrong).** An earlier draft of this NOTE (and the §summary table) "corrected" the bounds global
> to `0x82d80`, contradicting [pc-bounds.md](pc-bounds.md). Re-disassembling
> `GetSequenceBounds@0xd224` byte-exact settles it: the *only* `const16`/`l32i`
> that builds and **dereferences** a bounds pointer is `0xd25b: const16 a2,0x2dc0` →
> `0xd25e: l32i.n a3,a2,12` (= load `[0x82dc0+12]`, first of eight). The value `0x2d80`
> appears only once, at `0xd233`, on a `bnez.n a5` log/assert branch into the print helper
> `0x18b84` — it is **never** an `l32i` base. So **`0x82dc0` is byte-true**; pc-bounds.md was
> right all along and this page's `0x82d80` claim is retracted. `[HIGH/OBSERVED — byte-exact
> disasm of `0xd224`.]`

---

## 8. Shared PC-bounds enforcement — the prefetch OOB guard

The same `branch.cpp` decode logic that enforces PC bounds also gates every **prefetch target**:
a branch-target PC is checked by `is_pc_in_bounds @0x68d0` (the address-level guard detailed in
[the fetch front-end's §6](fetch-pc-redirect.md#6-the-pc-range-guard-is_pc_in_bounds-0x68d0-the-address-level-guard) and
the [PC-Bounds page](pc-bounds.md)) **before** any fill is scheduled:

```c
    int in_bounds = is_pc_in_bounds(prefetch_pc);   // 34de: call8 0x68d0
    if (in_bounds) goto proceed_push;               // 34e1: bnez.n a10,0x351e
    // ── OUT-OF-BOUNDS → warn + skip (NOT a hard fault) ──
    // 3513: const16 a11,0x81d51 ; 3516: movi.n a10,4 ; 3518: call8 0x1405c
    //        WARN "WARNING: ok_to_evict's prefetch_pc 0x%llx is out-of-bounds [0x%llx, 0x%llx], skipping it."
```

An out-of-range branch-target prefetch is **skipped with a warning**, never executed. This is the
prefetch-side mirror of the `wait_for_cache_line` OOB skip on the fetch path. The
`call8 0x68d0 @0x34de`, the `bnez`-skip, and the OOB warn at `0x3513` are instruction-exact.
`[HIGH/OBSERVED.]`

---

## 9. Correctness guard — why prefetch can never corrupt execution  `[HIGH]`

The prefetch is **speculative and decoupled from correctness**: pushing a line only *schedules*
its fill; it never lets the SEQ core execute an un-filled line. The guard is the
[IRAM-cache state machine](iram-cache.md#5-the-per-line-state-machine--wait_for_valid-0x67f4--highobserved),
which the prefetch never touches:

- Per-line **state word** `entry[0]` ∈ `{0 invalid, 1 fill-in-flight, 2 valid/ready}`.
  `replace()` sets the pushed/victim line to `1` **before** the DMA.
- `wait_for_valid @0x67f4` blocks the fetch until `entry[0] == 2`, calling
  `wait_for_dma_fill @0x61a8` (polls the DMA to completion) when `state == 1`, and **asserts**
  `state == 2` afterward (`cache.hpp`).

Therefore, even if the prefetch pushed the **wrong** block, or the fill has **not** completed,
the fetch **cannot** proceed into a line whose `entry[0] != 2` — it spins in `wait_for_dma_fill`.
**The prefetch sets only the `descr[+56]` pushed bitmask; the executable-validity gate is the
independent `entry[0]` state word + `wait_for_valid`, which the prefetch never bypasses.**
Prefetch *only* hides latency (it gets the fill started early); it can never cause incorrect
execution. `[HIGH/OBSERVED.]`

---

## 10. Failure / edge cases

**10a. Mispredicted branch (wrong block prefetched).** If actual control flow diverges from the
hinted path, the wrongly-pushed line is simply a wasted DMA fill. The pushed bit is cleared by
`0x6f38` (`descr[+56] &= ~(1<<idx)`) and/or the round-robin victim pointer eventually re-uses the
slot. No correctness impact (§9). A **not-taken** branch never arms (§4.4: decision `!= 0` →
clears armed bit "to preserve legacy behavior"), so a NT branch produces **no** prefetch.

**10b. Hint target out of bounds.** Gated by `is_pc_in_bounds @0x68d0` *before* any fill is
scheduled; OOB → warn + skip (§8). Not a hard fault.

**10c. Resolve-decision assert.** `resolve_hint_decision @0xcf90` **fatally asserts**
(`branch_prefetch_hint.cpp:45`) if the decision-class is not in `{0,1}` — a corrupt/unexpected
branch decision is a hard error, not silently prefetched (§4.2).

`[HIGH/OBSERVED — the `0x6f38` clear, the NT-no-arm path, the `0x68d0`+`0x3513` OOB skip, and the
line-45 assert.]`

---

## 11. Hint-invalidation triggers — when prefetch state is torn down

| trigger | mechanism | anchor |
|---|---|---|
| **(a) taken-branch redirect commit** | the `BRANCH` tail disarms the consumed hint (`state[0x855e0+0]=0`) | `0xcb6b → 0x151dc → 0x151e8` |
| **(b) not-taken branch** | `arm()` itself clears the armed bit ("marked NT … preserve legacy behavior") | `0x151c0` (§4.4) |
| **(c) instruction-flush / library change** | the `INS_FL` handler invalidates all hints before it may redirect | `INS_FL @0x85f4` → `call8 0x15344 @0x8616` |
| **(d) halt** | the `Halt` handler invalidates all hints — engine stop clears all hints | `Halt @0xa33c` → `call8 0x15344 @0xa38b` |
| **(e) miss / eviction** | the pushed bit for a line is cleared (`0x6f38`) when the cache evicts/consumes it; round-robin guarantees eventual reclamation | `descr[+56] &= ~(1<<idx)` |

Triggers (c) and (d) were located by their **call8 `0x15344`** sites: `0x8616` inside the
`INS_FL` handler (which logs `"S: INS_FL"`) and `0xa38b` inside the `Halt` handler (which logs
`"S: Halt"`). So reloading the `'S:'` stream / switching library, or halting the engine,
invalidates **all** hints. `[HIGH/OBSERVED — both `call8 0x15344` sites and the handler
strings.]`

---

## 12. Confirmed call / state map

```
 BRANCH execute  (branch.cpp 0xc7fc)
   ├─ sub-opcode jx via DRAM 0x82b84 (11 variants) → 0x4c3c (64-bit target getter)
   ├─ evaluate condition → BRTAKEN bool  (log 0xca61 "S: BRTAKEN: %d")
   └─ TAKEN (0xca7d): test PREFETCH-ENABLE flag  state[0x8564c] bit0
        ├─ enabled  → 0x8660 redirect helper
        ├─ legacy   → 0x5750 PC-REDIRECT (write CSR 0x1060/0x1080)   [fetch-pc-redirect]
        └─ tail (0xcb15): 0x6ec4 read armed-bit → if armed: 0x4c3c target, decision bool,
                          0x151dc DISARM hint
 PrefetchHelper  (branch_prefetch_hint.cpp):
   arm     0x150ec→0x15128 : rec[+0]=1, rec[+1]=decision (TAKEN, dec==0) / rec[+0]=0 (NT, dec!=0)
   disarm  0x151dc→0x151e8 : rec[+0]=0
   check   0x15208         : if armed & target in next-block-range → 0x15010 push
   push    0x15010         : "target in cache?" bool  (issues NO DMA fill)
   inval   0x15344/0x1536c : single-hint clear + 16×56B table [+48]=0
 Fetch front-end consumer  (0x326e..0x3600 RTL_PC_check / Push?):
   0x6ec4 armed-bit ; 0x15010 push-test ; is_pc_in_bounds 0x68d0 @0x34de ;
   0x5b84 SET descr[+56]|=1<<idx ; 0x6f24 READ descr[+56] ; 0x6f38 CLR bit ;
   OOB → 0x3513 warn+skip
 Cache correctness gate  (iram-cache, untouched by prefetch):
   replace 0x6068 → start_fill_siram 0x6217 (DMA) ; wait_for_valid 0x67f4 →
   wait_for_dma_fill 0x61a8 ; entry[0] state {0,1,2}, execute only when ==2.
 Invalidation hooks:  INS_FL 0x85f4 (call8 0x15344 @0x8616), Halt 0xa33c (@0xa38b),
                      taken-redirect 0xcb6b.
```

---

## 13. Reimplementation notes  `[INFERRED unless noted]`

A Vision-Q7-compatible SEQ engine that wants this latency-hiding behavior must replicate the
following contract:

- **Two PC-update paths.** A `BRANCH` handler that software-evaluates the condition into a taken
  bool, and on taken either rewrites the architectural PC (the redirect machine, CSR
  `0x1060`/`0x1080`) or, if a prefetch-enable flag is set, routes through a redirect helper. The
  *same* handler enforces PC bounds (`is_pc_in_bounds`). `[HIGH/OBSERVED contract.]`
- **An explicit, decision-gated hint — not a heuristic.** Arm only on a *taken* branch (decision
  class `0`); a not-taken branch must *disarm* (legacy fall-back). Store `{armed, decision}` in a
  single central record plus a 16-entry per-block table. `[HIGH/OBSERVED.]`
- **Push = predicate, not fill.** The "push" primitive must be a pure "is the target block in /
  pushable to cache?" predicate that *sets a bitmask bit* (`descr[+56]`), and must **not** issue
  the DMA itself. The fill is the cache's job, gated by the per-line `{0,1,2}` state word. This
  decoupling is what makes the prefetch correctness-neutral. `[HIGH/OBSERVED — the no-fill grep.]`
- **Invalidate on the four triggers** (redirect-commit, NT, INS_FL/library-change, halt) plus
  eviction. Forgetting any of these leaks stale armed bits or wastes DMAs. `[HIGH/OBSERVED.]`
- **OOB targets skip, not fault.** An out-of-bounds prefetch target must warn-and-skip; only the
  *executable* fetch faults on a genuinely bad PC. `[HIGH/OBSERVED.]`

The firmware *also* does an unconditional `+1`-stride sequential prefetch in the fetch path
(documented on the [IRAM-cache page](iram-cache.md)); the branch-hint mechanism here is the
*additional*, *explicit*, target-directed warm on top of that. `[CARRIED from the iram-cache
page.]`

---

## 14. Desync spans / honest gaps

**HIGH/OBSERVED (direct disassembly, re-verified by aligned re-disassembly):**

- Carve reproduced; `iram.bin` sha256 matches the iram-cache anchor exactly; `dram.bin` matches.
- `BRANCH` handler `0xc7fc`: the sub-opcode table `0x82b84` (11 entries, decoded byte-exact), the
  `"BRANCH"`/`"BRTAKEN"` logs, the 3-way branch-type switch, the taken-path flag-gated redirect
  (`0x5750`/`0x8660`) + armed-bit read (`0x6ec4`) + target getter (`0x4c3c`) + DISARM (`0x151dc`).
- `PrefetchHelper` `arm` (`0x15128`: `rec[+0]=1/0`, `rec[+1]=decision`, `bnez`-on-decision),
  `disarm` (`0x151e8`: `rec[+0]=0`), `invalidate-all` (`0x1536c`: 16×56B from `0x85f88+32`,
  `[+48]=0`), `check+prefetch-target` (`0x15208 → 0x15010`), cache-push (`0x15010` returns a bool,
  issues NO fill — verified absence of all five fill-chain calls in the whole prefetch span).
- pushed-bitmask `descr[+56]`: SET `0x5b84`, CLR `0x6f38`, READ `0x6f24` (all exact); descriptor
  base = `central_state(0x855e0) + 24`.
- `is_pc_in_bounds` gate `@0x34de` + OOB warn/skip `@0x3513` (string `0x81d51`).
- Invalidation triggers: INS_FL (`call8 0x15344 @0x8616`), Halt (`@0xa38b`), taken-redirect
  (`0xcb6b`), NT-branch (arm path).
- Correctness guard = the iram-cache `entry[0]` state `{0,1,2}` + `wait_for_valid`, which the
  prefetch never bypasses.
- Counts grounded on the binary: `call8 0x15010` = **2** sites, `call8 0x6ec4` = **2** sites.

**MED/INFERRED:**

- The branch CONDITION arithmetic (`0xc9a2..0xca2c`) and the `cb49` decision bool are exact in
  their ops (`xor`/`or`/`saltu`) but their precise predicate meaning is inferred across
  FLIX-desynced bundles.
- `BranchPrefetchHint(0xcfe8) → arm(0x15128)` **call edge**: the `arm` *function* is exact, but
  the linkage from `BranchPrefetchHint` is across a FLIX/`call0` desync span (the `0x150ec`/`0x15128`
  thunks have no aligned data-pool xref). The arm **is** invoked by the branch-hint flow — the
  central armed byte it writes is exactly the one `BRANCH`'s `0x6ec4` reads — but the precise
  caller instruction is inferred.
- `resolve_hint_decision` source-byte pick (`0xcfaa-0xcfc4` desync).
- The 64-bit range/target arithmetic in `0x15208` / `0x15010` (FLIX/IVP slots, e.g. the real
  bundles at `0x151a9`, `0x15298`, `0xcfbc`).
- The `RTL_PC_check` / `Evict?` / `Push?` per-iteration ordering (shared FLIX span with the
  iram-cache page); the cache CALL sites + bitmask ops are exact.

**NOT RESOLVED (deferred):**

- The exact `'S:'` **opcode byte** whose handler-object `vtable[0]=0xc7fc` (`BRANCH`) — dispatch-hub
  territory; handler identity is HIGH.
- The exact runtime **writer** of the `0x8564c` PREFETCH-ENABLE flag (read-only in IRAM; no
  `const16 …,0x564c` store found → set via pointer alias or host/CSR config). Its role is HIGH.

---

## 15. Cross-page divergences reconciled

| claim | naive reading | this page (verified) |
|---|---|---|
| sub-opcode table load | `c811: const16 a3,0x82b84` (single op) | **two** const16: `c811: const16 a3,8` + `c814: const16 a3,0x2b84` (HI:LO pair) |
| `descr[+56]` base | "`descr[+56]`" (base unstated) | descriptor base = `central_state(0x855e0) + 24`; `0x6f24`/`0x6f38` both `addi …,24` first |
| `GetSequenceBounds` data | bounds @`0x82dc0` ([pc-bounds.md](pc-bounds.md)) | **CONFIRMED `0x82dc0`** — handler `@0xd224` builds the pointer at `0xd25b` (`const16 a2,8 ; const16 a2,0x2dc0`) and dereferences it at `0xd25e` (`l32i.n a3,a2,12`). The `0x2d80` const16 at `0xd233` is on a log/assert branch only (never an `l32i` base). This page's earlier `0x82d80` claim is **retracted** — see the §7 CORRECTION |
| BRANCH/BRTAKEN log emitter | per-site thunks | all logs go through the single printf emitter `0x18b84` |

These are refinements, not contradictions: the prior-report offsets and semantics hold; this
page tightens the `const16`-pair rendering and pins the descriptor base. The
`GetSequenceBounds` data anchor is **`0x82dc0`** (byte-confirmed; an earlier draft
of this page mis-stated it as `0x82d80` and has been retracted — see the §7 CORRECTION).
`[HIGH/OBSERVED.]`

---

## 16. DEBUG vs PERF

Every IRAM offset and DRAM string VA on this page is from the **DEBUG** image (`CAYMAN_NX_POOL_DEBUG`).

- **DEBUG** keeps all `'S:'` strings in DRAM; each handler loads its string with a `const16` pair,
  so the log oracle that names `BRANCH` / `PrefetchHelper` / `branch_prefetch_hint.cpp` and the
  assert line `45` is *present*.
- **PERF** strips those strings and re-lays-out the code, so the **offsets differ** and the string
  oracle is gone. The *algorithm* (decision-gated arm, push-as-predicate, descr[+56] bitmask,
  the four invalidation triggers, the `{0,1,2}` correctness gate) is invariant across builds, but
  do **not** expect `0xc7fc`, `0x15128`, `0x855e0`, `0x82b84`, etc. to hold in PERF — re-derive
  them per-build.

`[HIGH/OBSERVED — the DEBUG-build string presence; the PERF re-layout is CARRIED from the
iram-cache and fetch-pc-redirect pages.]`

---

## 17. Adversarial self-verification — the 5 strongest claims

Each claim's disassembly evidence; verdicts:

1. **The `BRANCH`/`BRTAKEN` opcodes + 11-way sub-table.** `c803: l8ui a2,[a2+14]` (sub-opcode),
   `c811/c814: const16` pair → `0x82b84`, `addx4; l32i.n; jx`. Table at DRAM `0x2b84` decoded to
   exactly **11** little-endian words `0xc821…0xcbc2`, then the `branch.cpp:75` string — matching
   byte-for-byte. `"S: BRANCH" @0xc930`, `"S: BRTAKEN: %d" @0xca61`. **VERDICT: VERIFIED** (with
   the const16-pair refinement, §15).
2. **The taken-path PC redirect.** `ca86: bbci a2,0,0xcac8` (flag `0x8564c` bit0 split) →
   legacy `caea: call8 0x5750` / enabled `cac1: call8 0x8660`; tail `cb1b: call8 0x6ec4`
   (armed), `cb38: call8 0x4c3c` (target), `cb6b: call8 0x151dc` (disarm), `cb74: retw.n`.
   **VERDICT: VERIFIED.**
3. **The `PrefetchHelper::arm` warm mechanism.** `15151: bnez a3,0x151c0` on decision byte →
   ARM (`15158: movi.n a3,1; 1515a: s8i a3,[rec+0]`; `15160: s8i decision,[rec+1]`; log `0x3cf4`)
   / DISARM (`151c0: movi.n a3,0; 151c2: s8i a3,[rec+0]`; log `0x3d42` "marked NT"). **VERDICT:
   VERIFIED.**
4. **The IRAM-cache interaction.** `0x15010` issues **no** fill — rigorous grep over
   `0x15010..0x15420` for `replace`/`start_fill_siram`/`DramRingDMA`/`wait_for_cache_line`/
   `fetch_cache_line` returned **NONE**. Front-end `35db: call8 0x5b84` SET `descr[+56]|=1<<idx`;
   `5b97: l32i.n [a2+56]; 5b99: or; 5b9c: s32i.n [a2+56]` exact; descriptor = `state(0x855e0)+24`.
   **VERDICT: VERIFIED.**
5. **Shared PC-bounds enforcement.** `34de: call8 0x68d0` (`is_pc_in_bounds`),
   `34e1: bnez.n a10,0x351e` (in-bounds → proceed), else `3513: const16 a11,0x1d51`
   ("WARNING…out-of-bounds…skipping it") `+ call8 0x1405c`. `0x68d0` confirmed (`entry a1,96`,
   2 callers). **VERDICT: VERIFIED.**

All five pass against fresh disassembly of the hash-matched carve.

---

## See also

- [SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md) — the `0x5750` redirect machine
  (CSR `0x1060`/`0x1080`), the `0x4c3c` 64-bit target getter, the `is_pc_in_bounds @0x68d0`
  guard, and the dispatch handoff that reaches `BRANCH`'s `execute() @0xc7fc`.
- [SEQ IRAM Instruction Cache / Overlay](iram-cache.md) — the cache this prefetch warms: the
  `Push?` path / `descr[+56]` bitmask, the per-line `{0,1,2}` state machine + `wait_for_valid`
  correctness gate, and the `replace → start_fill_siram → DMA` fill chain the push schedules.
- [SEQ PC-Bounds Enforcement + Host API](pc-bounds.md) — the same `branch.cpp` decode logic that
  enforces PC bounds; the shared `is_pc_in_bounds` guard.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the `OBSERVED` /
  `INFERRED` / `CARRIED` × `HIGH` / `MED` / `LOW` tagging used throughout.
