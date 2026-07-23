# SEQ Fetch + PC-Redirect Front-End

This page is the **instruction-fetch front-end** of the GPSIMD **SEQ engine** — the
device-side sequencer that runs a per-NeuronCore `'S:'`-stream of opcodes on the Vision-Q7
NX "Cairo" `ncore2gp` datapath core. It is the `fetch.hpp` logic decoded byte-exact: how the
fetch unit reads the next instruction word, how the architectural program counter **advances
in-stream** by one fetch step versus how it is **redirected** on a branch/jump, and how the
fetched opcode is **handed off** to the dispatch hub. It is the front of a pipeline whose
other stages are split across siblings: the surrounding poll/stop FSM is
[SEQ Main FSM Loop](main-loop.md), the redirect/prefetch arming is
[SEQ Branch + Prefetch-Hint](branch-prefetch.md), the address-range guard is
[SEQ PC-Bounds Enforcement + Host API](pc-bounds.md), the instruction-cache fill the fetch
rides on is [SEQ IRAM Instruction Cache / Overlay](iram-cache.md), and the Handler-object
table the dispatch lands in is [SEQ Decode / Dispatch Hub](dispatch-hub.md).

The reason this page exists as its own surface — rather than a paragraph inside the main loop
— is that the **redirect block is where prior passes lost FLIX sync**. The SEQ runs on a
config (`IsaMaxInstructionSize = 32`, Vision-Q7 FLIX) whose variable-length bundles defeat a
linear-sweep disassembler at exactly the spans where the redirect arms and clears its pending
flag. This page re-decodes those spans with the
correct alignment and pins the full chain: *poll → pending-redirect → advance → fetch `[a4]`
→ `opcode − 0x41` → `bgeu 177` bound → table `@DRAM 0x80814` → `jx` → back-edge*.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte / string / config field / disassembler line read from a shipped artifact;
`INFERRED` = reasoned over OBSERVED facts (often across a FLIX/literal-pool
desync); `CARRIED` = consolidated from a cited cross-page anchor at its original confidence.
Crossed with `HIGH` / `MED` / `LOW`. Callouts: **QUIRK** (counter-intuitive but real),
**GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**
(orientation). The page default is `[HIGH/OBSERVED]`; claims that depart from it carry an
explicit tag.

> **NOTE — carve provenance + addressing.** The whole page is grounded in a carve of the SEQ
> base firmware out of the static archive `libnrtucode.a`, member
> `img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}_contents.c.o`. The carve `objcopy -O binary
> --only-section=.rodata` yields **iram.bin = 116768 B (0x1c820), sha256
> `8e4412b9…ab9ed70a`** and **dram.bin = 28448 B (0x6f20), sha256 `7bdf6ed7…d6816ecd`**, with
> head bytes `06 76 00 00 86 77 00 00` = `j 0x1dc` (reset vector). Decode is the native
> `xtensa-elf-objdump` (GNU Binutils 2.34.20200201, Xtensa Tools 14.09) with
> `XTENSA_CORE=ncore2gp` — the **only** config that decodes SEQ FLIX correctly; the scalar-LX
> NCFW core mis-decodes (see [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md)).
> All IRAM offsets equal device IRAM VA (reset vector at byte 0); DRAM
> string offset = device DRAM VA − `0x80000` (the DRAM image loads at VA `0x80000`). For the
> raw binary carve, `.text`/`.rodata` VMA == file offset by construction.

> **GOTCHA — DEBUG vs PERF offsets differ; this page anchors to DEBUG.** The control-flow
> algorithm is **identical** in the `POOL_PERF` build, but PERF strips every `call8 0x18b84`
> log call and re-lays-out the code, so **every interior address below shifts in PERF**. The
> DEBUG image keeps the `'S:'` format strings as named xrefs, so every step here has a string
> anchor. **Do not** look up a DEBUG address in a PERF image. `[HIGH/OBSERVED]`

---

## 0. The fetch front-end in one diagram

```
   enter_run (0x2c64): read 64-bit PC from CSR 0x1060 (PC_lo) / 0x1080 (PC_hi)
                        -> stack[a1+24]/[a1+28]
        │
        ▼
   ┌────────────────────────  SUNDA SOFTWARE-FETCH FSM  (back-edge 0x31a3 -> 0x2d81)  ───────────────────────┐
   │                                                                                                          │
   │  STEP 1  poll-surprises  0x6af4  -> state[0x855e0+100].bit0 (running?)  -- empty -> retw EXIT            │
   │  STEP 2  sunda_check_surprises 0x6b0c  (surprise check — gates the redirect; see CORRECTION)            │
   │            ├─ HANDLED -> 0x4c3c compute 64-bit target PC                                                 │
   │            │         0x6b70 translate PC -> iram_pc / cache index                                        │
   │            │         LOG "redirecting to pc=…/iram_pc=…"                                                 │
   │            │         0x6bb0 commit redirect ; s8i 0,[a2] CLEAR pending                                   │
   │            │         (heavy machine 0x5750: WRITE PC -> CSR 0x1060/0x1080;                               │
   │            │          push {phase,iram_addr,num_instr}; SW/HW cache fill)                                │
   │            └─ NO  -> 0x5e98 advance-poll accumulate                                                      │
   │  STEP 3  stop?  l8ui [a1+40] == 0 -> retw EXIT                                                           │
   │  STEP 4  advance  0x1c64 -> 0x3a78 (drain notify queue) ; a2 = next word                                │
   │  STEP 5  FETCH    s32i a2,[a4]  (advance cursor) ; l32i a2,[a4]  (read opcode word)                      │
   │                   LOG "Dispatch opcode=0x%x"                                                             │
   │  STEP 6  DECODE   a2 = opcode - 0x41 ; bgeu 177,a2 ? no -> 0x3198 default -> ErrorHandler(BadOpcode)     │
   │                   addx4 a2,a2,DRAM 0x80814 ; l32i a2,[a2] ; jx a2  -> thin trampoline                    │
   │  STEP 7  trampoline 'call8 <impl>; j 0x31a3' -> impl: a2 = Handler obj -> thunk 0x96d4                   │
   │                   l32i a2,[obj+0] (vtable) ; callx8 a2 = vtable[0] = Handler::execute()                  │
   │                   handler ends -> j 0x31a3 -> 0x2d81  (BACK-EDGE)                                        │
   │                                                                                                          │
   │  [parallel guard]  is_pc_in_bounds 0x68d0 gates every fetch-address on the                              │
   │                    wait_for_cache_line (0x5d79) and prefetch (0x34de) paths ->                          │
   │                    out-of-range = SKIP + WARN (not a fault)                                              │
   └──────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

One-line verdict: a **software fetch loop** whose cursor `a4` reads a **32-bit word**, takes
only its **low byte** as the opcode, advances the cursor in-stream by writing the
notify-consume result back to `[a4]`, and **redirects** by recomputing a 64-bit PC and
rewriting it into the core CSR pair `0x1060`/`0x1080` — gated, on the cache-fill side, by an
address-range guard.

> **QUIRK — the SEQ has TWO fetch front-ends; this page decodes the software one.** Mirroring
> the boot mode pick ([SEQ Boot / Entry Path](boot.md), [HW-Decode vs Sunda Dual
> Fetch](dual-fetch.md)), the SEQ ships a **Sunda software-fetch** front-end (instruction-exact
> here) and a **HW-decode** front-end (head + exits only; its body `@0x31ac` is a flagged FLIX
> desync). The `fetch.hpp` logic this page owns is the **Sunda software-fetch** path. The
> HW-decode body is out of scope and is noted where the two paths fork (the
> `state[0x85070]` HW-decode flag, §4). `[HIGH/OBSERVED]`

---

## 1. The PC-stream model — where the architectural PC lives

The SEQ's architectural program counter is a **64-bit value** held in the core's
GENERAL-LR CSR pair, **not** in a GPR. `enter_run` materialises it onto the stack frame before
entering the FSM, and the redirect machine writes it back. The two halves are one CSR-bundle
register and its `+0x20`-stride sibling (`CSR-01` general bundle, base `0x1000`, stride
`0x20`; `0x1080 = 0x1060 + 0x20`).

All rows `[HIGH/OBSERVED]`:

| Field | Holder | Read site | Write site |
|---|---|---|---|
| PC_lo | CSR `0x1060` (general LR) | enter_run `0x2d33`/`0x2d36` | redirect `0x5790` |
| PC_hi | CSR `0x1080` (= `0x1060`+`0x20`) | enter_run `0x2d3d`/`0x2d40` | redirect `0x579c` |
| PC snapshot lo/hi | stack `[a1+24]`/`[a1+28]` | written `0x2d38`/`0x2d42` | — |
| Fetch cursor | reg `a4` | `l32i.n [a4]` `0x2e52`/`0x2e5d` | `s32i.n a2,[a4]` `0x2e50` |
| Opcode width at boundary | **32-bit word**, low byte = opcode | `l32i.n` `0x2e5d` | — |

The enter_run prologue (linear sweep, `--adjust-vma=0x0`):

```
2d30:  42a400      movi    a4, 0x400          ; a4 = 0x00400000 (aperture high half — LOW)
2d33:  446010      const16 a4, 0x1060         ; a4 = CSR 0x1060 = general-LR / PC_lo
2d36:  5804        l32i.n  a5, a4, 0          ; a5 = PC_lo   (CSR 0x1060)
2d38:  5961        s32i.n  a5, a1, 24         ; stack[+24] = PC_lo
2d3a:  52a400      movi    a5, 0x400
2d3d:  548010      const16 a5, 0x1080         ; (a5 = CSR 0x1080 / PC_hi address)
2d40:  4884        l32i.n  a4, a4, 32         ; a4 = PC_hi   (CSR 0x1060 + 0x20 = 0x1080)
2d42:  4971        s32i.n  a4, a1, 28         ; stack[+28] = PC_hi
```

> **CORRECTION — the const16 lands at `0x2d33`, not `0x2d36`.** A prior listing folded the
> `movi a4,0x400 ; const16 a4,0x1060` pair into one address. The byte-exact decode separates
> them: `0x2d30 movi`, `0x2d33 const16`, `0x2d36 l32i.n`. The **register identity** (a4 = CSR
> `0x1060`) and the read of `0x1060+0x20` for the high half are unchanged and HIGH. The
> absolute aperture base (`0x00400000` from the `movi`-high vs a flat `0x0` alias) is the only
> LOW part of the address — the register *identity by low offset* is HIGH. `[HIGH/OBSERVED]`

```c
/* enter_run: materialise the 64-bit architectural PC from the CSR pair. [HIGH/OBSERVED]
 * CSR_PC_LO = 0x1060 (general-LR), CSR_PC_HI = 0x1080 (= 0x1060 + 0x20).
 * The stride-0x20 sibling is read as [csr_pc_lo + 32] in the same a4 base. */
static inline uint64_t seq_read_arch_pc(void) {
    volatile uint32_t *csr = (volatile uint32_t *)CSR_PC_LO; /* 0x1060 */
    uint32_t lo = csr[0];                                    /* [0x1060]      */
    uint32_t hi = csr[8];                                    /* [0x1080] = +32 bytes / 8 words */
    return ((uint64_t)hi << 32) | lo;
}
```

> **GOTCHA — `[a4+32]` is a *byte* offset of 32 = 8 words, not slot 32.** `l32i.n a4,[a4+32]`
> adds **32 bytes** to the `0x1060` base, landing on `0x1080`. In C with a `uint32_t*` that is
> index `8`, not `32`. Re-deriving the CSR map from "a4 plus 32" as a word index gives the
> wrong register. `[HIGH/OBSERVED]`

---

## 2. The Sunda software-fetch FSM body (instruction-exact)

The loop is first reached via the `"S: Sunda seq Loop"` head log (`0x2d70`); every subsequent
iteration re-enters at the poll point `0x2d81` via the back-edge `0x31a3 -> 0x2d81`. The seven
steps below are each OBSERVED in the re-decoded image.

### 2.1 STEP 1 — poll-surprises (loop continue / exit)

```
2d81:  call8 0x6af4              ; "is there work?": returns running-flag bit0 in a10
2d84:  beqz  a10, 0x31a6         ; NO WORK -> 0x31a9 retw.n == LOOP EXIT (back to outer boot loop)
```

The poll helper `0x6af4`, OBSERVED:

```
6af4:  entry  a1, 48
6af7:  const16 a2, 8 ; const16 a2, 0x55e0 ; addi a2,a2,100  ; a2 = &state[0x855e0 + 100]
6b04:  l8ui   a2, a2, 0
6b07:  extui  a2, a2, 0, 1                                   ; a2 = byte & 1  (running flag)
```

```c
/* STEP 1: poll-surprises @0x6af4 — read the per-engine running flag. [HIGH/OBSERVED]
 * STATE_BASE = 0x855e0 (DRAM); the running flag is byte [STATE_BASE + 100], bit 0. */
static inline bool seq_is_running(void) {
    return (*(volatile uint8_t *)(STATE_BASE + 100)) & 0x1u;
}
/* in the FSM head: */
if (!seq_is_running()) return;   /* beqz a10,0x31a6 -> 0x31a9 retw.n : LOOP EXIT */
```

### 2.2 STEP 2 — surprise check then the software PC redirect

```
2d8f:  const16 a2, 0x5068 ; s32i a2,[a1+36] ; mov.n a10,a2   ; per-engine ctx ptr -> stack[+36]
2d9a:  call8 0x6b0c                                          ; sunda_check_surprises (nonzero => handled; see CORRECTION)
2d9d:  bnez.n a10, 0x2daa                                    ; surprise handled -> take redirect block
2da2:  movi.n a2,0 ; s8i a2,[a1+40] ; j 0x2e3b               ; not handled -> clear ready flag, fall to exit test
```

> **CORRECTION — `0x6b0c` is `sunda_check_surprises`, not a dedicated `pending_redirect`.** The
> STEP-2 gate call `0x2d9a: call8 0x6b0c` was earlier named `pending_redirect`/`seq_pending_redirect`.
> Re-disassembled, `0x6b0c` logs DRAM `0x819cc` (`"S: sunda_check_surprises: any=%d, surprises=0x%x"`),
> computes the surprise word, and tail-calls the surprise bit-mask handler `0x6ce0` — it is the
> surprise **check** (`surprises.hpp`). The PC-redirect body documented below (`0x4c3c` target
> compute, `0x6b70` translate, `0x5750` PC-rewrite, `0x6bb0` commit, the `"S: sunda_fetch:
> redirecting…"` strings @DRAM `0x8197b`/`0x81951`) is real and is this page's subject — it just
> runs **gated by** the surprise check rather than by a standalone `pending_redirect` function.
> The two functions are adjacent in the same packed region. Authoritative treatment of the check:
> [surprises-irq.md](surprises-irq.md) §2–§3. `[HIGH/OBSERVED]`

The redirect block (`0x2daa`) — the span FW-02 lost to FLIX desync, **re-synced**.
The interior `0x2dba..0x2dd1` decodes as garbage under the linear sweep (the
`.byte 0x8f`, a bogus `l32r a1,0xfffe9108`); the **clean resync** is at `0x2dd3`, and the
arming/clearing instructions bracket it:

```
2daa:  l8ui  a3,[a2] ; bbci a3,0,0x2df5    ; redirect-armed bit clear -> no-redir (0x2df5)
2db3:  call8 0x4c3c                        ; COMPUTE 64-bit redirect target PC (small getter -> a10/a11)
2db6:  s32i.n a11,[a1+28] ; s32i.n a10,[a1+24]  ; save target PC hi/lo to stack
       <FLIX-desync span 0x2dba..0x2dd1 — log "S: ...redirecting to pc=0x%llx" @DRAM 0x81951>
2dd3:  call8 0x5b74                        ; redirect-state helper (set flag in [redir+56])
2dd6:  call8 0x6b70                        ; TRANSLATE pc -> iram_pc / cache index (-> a2)
2dd9:  s32i.n a10,[a1+20] ; l32i.n a11,[a1+20]   ; iram_pc result
2ddd:  const16 a10,8 ; const16 a10,0x197b ; call8 0x18b84   ; LOG "redirecting to iram_pc=0x%x" @0x8197b
2de6:  l32i.n a11,[a1+20] ; mov.n a10,a2
2dea:  call8 0x6bb0                        ; COMMIT redirect (push "S: Redirect" @0x81a62)
2ded:  movi.n a3,0 ; s8i a3,[a2]           ; CLEAR pending-redirect flag
2df2:  j 0x2dfd
--- no-redirect arm (0x2df5) ---
2df5:  call8 0x5e98                        ; advance-poll accumulate (reads/updates branch SR)
2df8:  j 0x2dfd
```

> **CORRECTION — the redirect target is computed *before* the FLIX-desync, and the pending
> flag is cleared *after* the commit.** A naive single-sweep read of `0x2dba..0x2dd1`
> hallucinates a stack-pointer clobber (`l32r a1, …`). It is not real: the real boundary has
> `0x4c3c` returning the target, the two stack stores at `0x2db6`/`0x2db8`, then the
> `pc->iram_pc` translate at `0x2dd6`, the commit at `0x2dea`, and **only then** the
> `s8i 0,[a2]` clear at `0x2def`. Clearing-before-commit would re-arm an already-consumed
> redirect on the next poll. `[HIGH/OBSERVED on the bracket, MED/INFERRED on the desynced log
> bytes]`

```c
/* STEP 2: pending-redirect arming/clear (lightweight per-iteration). [HIGH/OBSERVED]
 * REDIR_STATE = 0x5068; the heavy PC-rewrite is seq_redirect_execute() @0x5750, §4. */
struct redir_state { uint8_t armed; /* [+0] */ /* ... */ uint8_t flag56; /* [+56] */ };

static void seq_check_pending_redirect(struct redir_state *rs) {
    if (!sunda_check_surprises(ctx))      /* 0x6b0c -> bnez.n a10 (surprise check gate) */
        return;                           /* no surprise handled this iteration */
    if (!(rs->armed & 0x1))               /* l8ui [a2]; bbci a3,0 -> 0x2df5 */
        return;                           /* armed bit clear: nothing to do */

    uint64_t target = seq_compute_redirect_target();   /* 0x4c3c -> {a10=lo,a11=hi} */
    stack.pc_lo = (uint32_t)target;       /* s32i a10,[a1+24] */
    stack.pc_hi = (uint32_t)(target>>32); /* s32i a11,[a1+28] */
    LOG("sunda_fetch: redirecting to pc=0x%llx", target);          /* @DRAM 0x81951 */

    redir_state_set_flag(rs);             /* 0x5b74 -> rs->flag56 */
    uint32_t iram_pc = seq_pc_to_iram(target);          /* 0x6b70 translate */
    LOG("sunda_fetch: redirecting to iram_pc=0x%x", iram_pc);      /* @DRAM 0x8197b */

    seq_commit_redirect(iram_pc);         /* 0x6bb0 -> "S: Redirect" @0x81a62 */
    rs->armed = 0;                        /* s8i 0,[a2] @0x2def : CLEAR pending AFTER commit */
}
```

### 2.3 STEP 3 — exit / stop test

```
2e35:  s8i a5,[a1+40]            ; mark "have instruction to dispatch" (from the no-redirect arm)
2e3b:  l8ui a2,[a1+40] ; bnez.n a2, 0x2e46   ; ready -> advance/fetch
2e43:  j 0x31a9                  ; not ready (stop requested) -> retw.n == LOOP EXIT
```

### 2.4 STEP 4 — advance / surprise-consume

```
2e46:  call8 0x1c64   (-> 0x3a78)  ; advance/notification-consume; a2 = next word
       <FLIX co-issue desync 0x2e49..0x2e4b: .byte 0x2f 0x10 ; const16 a13,0xa658>
2e4e:  bnez.n a5, 0x2e62           ; if a5 set, skip the re-fetch (jump straight to decode bound)
```

`0x1c64` is a one-line shim into the queue drainer: `call8 0x3a78 ; retw.n`. `0x3a78` drains
the surprise/notify queue (`0x3b00`/`0x3b14`/`0x3704` polling loops) and returns the next
in-stream word in `a2`.

```c
/* STEP 4: in-stream advance. The "next word" is the notify-consume result, NOT a raw
 * PC+len read here — the cursor write in STEP 5 is what commits the advance. [HIGH/OBSERVED] */
uint32_t next = seq_advance_consume();   /* 0x1c64 -> 0x3a78 : drain notify queue, a2 = next */
```

### 2.5 STEP 5 — fetch the instruction word

```
2e50:  s32i.n a2,[a4+0]                 ; WRITE advanced word/PC to the cursor [a4]  (ADVANCE)
2e52:  l32i.n a11,[a4+0]                ; re-read fetched word (log arg)
2e54:  const16 a10,8 ; const16 a10,0xe38 ; call8 0x18b84   ; LOG "S: Dispatch opcode=0x%x" @0x80e38
2e5d:  l32i.n a2,[a4+0]                 ; a2 = opcode word (re-read for decode)
```

> **GOTCHA — the fetch reads a full 32-bit word but dispatches on the low byte only.** The
> `l32i.n [a4]` loads 4 bytes; the decode in STEP 6 does `addi a2,a2,-65` on the **whole
> word**, but the `bgeu 177` bound plus the unsigned wrap means only words whose low byte is in
> `0x41..0xf2` (`'A'..0xf2`) select a real handler — any high bits make the index exceed 177
> (or wrap huge) and fall to the default. Reimplementations that mask `& 0xff` before the
> subtract get the **same** dispatch result, but the firmware does **not** mask: it relies on
> the bound to reject. `[HIGH/OBSERVED]`

### 2.6 STEP 6 — decode + 178-way dispatch

```
2e5f:  addi  a2,a2,-65            ; index = opcode_byte - 0x41 ('A')
2e62:  movi  a3,177              ; upper bound (178 entries: indices 0..177)
2e65:  bgeu  a3,a2,0x2e6b        ; if (unsigned)index <= 177 -> in range
2e68:  j 0x3198                  ; else DEFAULT (unknown opcode -> ErrorHandler), §5
2e6b:  const16 a3,8 ; const16 a3,0x814   ; a3 = DRAM 0x80814 (jump-table base)
2e71:  addx4 a2,a2,a3           ; &table[index] = index*4 + base
2e74:  l32i.n a2,[a2]          ; a2 = table[index] (thin trampoline addr)
2e76:  jx    a2               ; -> trampoline -> Handler::execute()
```

> **QUIRK — one `bgeu` catches *both* ends of the range.** `bgeu a3,a2` is an **unsigned**
> compare of `177` against `index`. For an opcode below `0x41`, `opcode - 0x41` underflows to a
> huge unsigned (e.g. `0x40 - 0x41 = 0xffffffff`), which is **greater** than 177, so the same
> single branch rejects under-range opcodes *and* over-range opcodes. No separate lower-bound
> test exists. `[HIGH/OBSERVED]`

```c
/* STEP 6: decode + dispatch. [HIGH/OBSERVED]  TABLE = DRAM 0x80814, 178 x 4 bytes. */
uint32_t word  = *(volatile uint32_t *)cursor;   /* l32i.n [a4] */
uint32_t index = word - 0x41u;                   /* addi -65 ; unsigned underflow on op<0x41 */
if (index > 177u) {                              /* bgeu 177,index : single unsigned bound */
    goto dispatch_default;                       /* j 0x3198 -> ErrorHandler(BadOpcode) */
}
void (*trampoline)(void) = ((void (**)(void))TABLE)[index];   /* addx4 + l32i */
trampoline();                                    /* jx a2 (tail) */
```

### 2.7 STEP 7 — back-edge and exits

```
31a3:  j 0x2d81      ; SUNDA-LOOP BACK-EDGE (every handler tail returns here)
31a6:  j 0x31a9
31a9:  retw.n        ; LOOP EXIT (no-work / stop)
```

Loop-local register roles (OBSERVED):

| Reg | Role |
|---|---|
| `a4` | instruction-stream **FETCH CURSOR** (addresses the opcode word) |
| `a2` | opcode word \| table-index \| dispatch target (reused) |
| `a3` | bound (177) → jump-table base → scratch |
| `a1+24`/`+28` | 64-bit current PC (lo/hi) snapshot from CSR `0x1060`/`0x1080` |
| `a1+40` | "instruction-ready / stop" flag byte |
| `a1+36` | redirect-state pointer (built `const16 0x5068`) |

> **GOTCHA — `a4` re-aliasing per iteration is MED.** `a4` is **confirmed** to address the
> fetch word (the `s32i`/`l32i` at `0x2e50`/`0x2e52`/`0x2e5d` are HIGH), but the exact `a4`
> reload site sits inside the FLIX-desynced bundle at `0x2e46..0x2e4e`. Whether `a4` re-aliases
> CSR `0x1060` directly or points at a DRAM cache-line slot the I-cache fill populated is
> **INFERRED/MED** — both are consistent with the observed accesses. A reimplementation should
> treat `a4` as "the validated, cache-resident pointer to the current instruction word", and
> defer the source of that pointer to [SEQ IRAM Instruction Cache / Overlay](iram-cache.md).
> `[MED/INFERRED]`

---

## 3. Sequential advance vs PC redirect — the two PC-update paths

The two ways the PC moves are structurally distinct and must not be conflated.

| | Sequential advance | PC redirect (branch/jump) |
|---|---|---|
| Trigger | every iteration, default | `sunda_check_surprises` gate `0x6b0c` returns "handled" |
| Mechanism | `0x1c64`→`0x3a78` drains notify queue → `s32i a2,[a4]` writes next word to cursor | `0x4c3c` computes 64-bit target → `0x5750` writes it to CSR `0x1060`/`0x1080` |
| PC write | implicit (cursor advances, in-stream) | explicit (architectural CSR pair rewritten) |
| Cache side-effect | reads the already-filled line | pushes a new cache-fill descriptor `{phase,iram_addr,num_instr}` |
| Cost | light (one queue drain + store) | heavy (translate + CSR write + cache fill) |
| Anchor | `0x2e46`/`0x2e50` | `0x2db3`…`0x2def` arms; `0x5750` executes |

> **NOTE — the per-iteration redirect block only *arms and clears*; the heavy lift is a
> separate function.** STEP 2 (`0x2daa..0x2def`) is the lightweight arming: compute target,
> translate, commit-and-clear-flag. The actual **CSR PC rewrite + cache-fill push** is function
> `0x5750` (§4), called from `enter_run` setup (`0x5537`) and the resume/redirect sites
> (`0x8657`, `0xcaea`). The "redirect" thus has a fast control half (per-iteration) and a slow
> data half (the CSR/cache machine). `[HIGH/OBSERVED]`

```c
/* The two PC-update paths, unified view. [HIGH/OBSERVED] */
for (;;) {
    if (!seq_is_running()) return;             /* STEP 1 poll  (0x6af4) */
    seq_check_pending_redirect(&redir);        /* STEP 2 redirect arm/clear (0x6b0c..0x2def) */
    if (!stack.ready) return;                  /* STEP 3 stop test (0x2e3b) */

    uint32_t next = seq_advance_consume();     /* STEP 4 advance (0x1c64->0x3a78) */
    *cursor = next;                            /* STEP 5 commit advance (s32i [a4]) */
    uint32_t word = *cursor;                   /* STEP 5 fetch (l32i [a4]) */
    LOG("Dispatch opcode=0x%x", word);         /* @DRAM 0x80e38 */

    seq_decode_dispatch(word);                 /* STEP 6/7 -> Handler::execute() -> back-edge */
}
```

---

## 4. The PC-redirect execution machine `@0x5750`

This is the slow data half of a redirect: it writes the new 64-bit PC into the architectural
CSR pair and pushes a cache-fill descriptor. String anchors (DEBUG): `"S: Redirecting to
pc=0x%llx"` `@DRAM 0x8134a`, `"S: Redir: phase=%x, iram_addr=0x%x, num_instr=%d"` `@0x81367`.

```
5750:  entry a1,96 ; s32i.n a2,[a1+36] ; s32i.n a3,[a1+8]   ; (a2 = pc desc, a3 = ctx)
       LOG "S: Redirecting to pc=0x%llx" (@DRAM 0x8134a)
577e:  call8 0x4cc4 ; call8 0x5a70                          ; redirect prep
5786:  l32i.n a3,[a1+8] ; l32i.n a3,[a3+0]                  ; a3 = new PC_lo (ctx[0])
578a:  movi a4,0x400 ; const16 a4,0x1060                    ; a4 = CSR 0x1060
5790:  s32i.n a3,[a4+0]                                     ; WRITE PC_lo -> CSR 0x1060
5792:  l32i.n a3,[a1+8] ; l32i.n a3,[a3+4]                  ; a3 = new PC_hi (ctx[4])
5796:  movi a5,0x400 ; const16 a5,0x1080                    ; (CSR 0x1080)
579c:  s32i.n a3,[a4+32]                                    ; WRITE PC_hi -> CSR 0x1080 (a4+0x20)
57a1:  const16 a3,8 ; const16 a3,0x55e0 ; addi a3,a3,24     ; &state[0x855e0 + 24] (descriptor base)
57a9:  call8 0x5a88 ; call8 0x5b74 ; call8 0x5a98 ; call8 0x5b84   ; translate + push cache desc
58b8:  LOG "S: Redir: phase=%x, iram_addr=0x%x, num_instr=%d" (@DRAM 0x81367)
58dc:  l8ui a3,[state 0x85070] ; bbci ...                   ; SW-cache vs HW-cache branch
       SW -> call8 0x5e88 (software cache fill) ; HW -> 0x59e7
```

The PC write at `0x5790`/`0x579c` is **byte-exact** — it is the
register-identity twin of the enter_run read (§1), just in the store direction:

```c
/* seq_redirect_execute @0x5750 — commit the redirect target PC + push a cache fill. [HIGH/OBSERVED] */
void seq_redirect_execute(const struct pc_desc *ctx) {
    LOG("Redirecting to pc=0x%llx", *(uint64_t *)ctx);

    volatile uint32_t *csr = (volatile uint32_t *)CSR_PC_LO; /* 0x1060 */
    csr[0] = ctx->lo;        /* s32i a3,[a4+0]  : WRITE PC_lo -> 0x1060 */
    csr[8] = ctx->hi;        /* s32i a3,[a4+32] : WRITE PC_hi -> 0x1080 */

    /* translate + push {phase, iram_addr, num_instr}; then SW vs HW cache fill */
    struct cache_desc d = seq_translate_descriptor(STATE_BASE + 24);  /* 0x57a1 + helpers */
    LOG("Redir: phase=%x, iram_addr=0x%x, num_instr=%d", d.phase, d.iram_addr, d.num_instr);
    if (state_byte(0x85070))      /* HW-decode-active flag (FW-01) */
        seq_hw_cache_fill(&d);    /* 0x59e7 */
    else
        seq_sw_cache_fill(&d);    /* 0x5e88 */
}
```

**Redirect helper inventory** (one-liners, OBSERVED roles; FLIX-prologue field math MED):

| Addr | Role | Tag |
|---|---|---|
| `0x4c3c` | compute/return the 64-bit redirect target PC (small getter) | `[HIGH role / MED field-math]` |
| `0x6b0c` | `sunda_check_surprises` (logs DRAM `0x819cc`, tail-calls handler `0x6ce0`; "handled" bool gates the redirect — see §2.2 CORRECTION + [surprises-irq.md](surprises-irq.md)) | `[HIGH/OBSERVED]` |
| `0x6b70` | PC → iram_pc / cache-index translate (reads `state[0x855e0+24]`) | `[HIGH/OBSERVED]` |
| `0x6bb0` | commit the redirect / push (`"S: Redirect"` `@DRAM 0x81a62`) | `[HIGH/OBSERVED]` |
| `0x5b74` | redirect-state flag set (`[redir+56]`); reads `state[0x85070]` | `[HIGH/OBSERVED]` |
| `0x6af4` | poll-surprises (returns `state[0x855e0+100]` bit0) | `[HIGH/OBSERVED]` |
| `0x1c64`→`0x3a78` | advance / surprise-notify consume (drains notify queue) | `[HIGH/OBSERVED]` |
| `0x5750` | heavy redirect machine (CSR PC write + cache-fill push) | `[HIGH/OBSERVED]` |

> **NOTE — `state[0x85070]` is the SW-vs-HW-cache fork.** The byte at DRAM `0x85070` is the
> HW-decode-active flag from [SEQ Boot / Entry Path](boot.md). `0x5750` reads it at `0x58dc` to
> pick the software cache fill (`0x5e88`) versus the hardware path (`0x59e7`). This is the same
> flag that selected the fetch front-end at boot ([HW-Decode vs Sunda Dual Fetch](dual-fetch.md)).
> The cache-fill internals past `0x58f0` are [SEQ IRAM Instruction Cache / Overlay](iram-cache.md)
> territory. `[HIGH/OBSERVED on the fork; LOW on the cache arithmetic]`

---

## 5. The dispatch handoff — table → trampoline → Handler vtable

The handoff is a **C++ virtual dispatch** reached through a thin trampoline. The table was
decoded from `dram.bin`, byte-exact.

**The 178-entry table** `@DRAM 0x80814` (`dram.bin` offset `0x814`), all rows `[HIGH/OBSERVED]`:

| Property | Value |
|---|---|
| Base | DRAM `0x80814` |
| Count | **178** entries × 4 B (span `0x814..0xadc`, 712 B) |
| Index | `opcode_byte − 0x41` |
| Default target | `0x3198` (**123** entries) |
| Real handlers | **55** entries, **all** in-range IRAM (`< 0x1c820`) |

First real targets (independently dumped): `idx 0 'A'→0x3074`, `idx 2 'C'→0x309d`,
`idx 3 'D'→0x30ad`, `idx 4 'E'→0x3064`, `idx 5 'F'→0x2fee`, `idx 6 'G'→0x2ff6`, `idx 8
'I'→0x3084`, `idx 18 'S'→0x30a5`.

**Trampoline → Handler object → vtable execute()** (the actual handoff; example for `'S'`,
opcode `0x53`, `idx 18 → 0x30a5`):

```
30a5:  call8 0x21d4 ; j 0x31a3        ; thin trampoline: call impl, then BACK-EDGE
--- impl 0x21d4 ---
21d4:  entry a1,48
21d7:  const16 a2,0 ; const16 a2,0xa02c   ; a2 = &Handler_object (IRAM 0xa02c)
21dd:  s32i a2,[a1+12]
21e7:  call8 0x96d4                        ; virtual-dispatch thunk
21ea:  retw
--- thunk 0x96d4 ---
96d4:  entry a1,48
96d7:  s32i a2,[a1+12]
96da:  call8 0x3aac                        ; (frame / notify setup)
96e6:  l32i a2,[a1+12] ; l32i.n a2,[a2+0]  ; a2 = vtable ptr  (object[0])
96eb:  callx8 a2                            ; CALL vtable slot 0 = Handler::execute()
96f5:  retw.n
```

```c
/* Dispatch handoff: opcode-indexed table -> trampoline -> Handler object -> vtable[0]. [HIGH/OBSERVED]
 * The handoff register at the jx is a2 = the Handler OBJECT pointer.
 * The opcode/operand DATA is NOT passed in a register: it lives in (a) the PC cursor / fetched
 * word still resident in the engine, and (b) the per-engine state struct @DRAM 0x855e0. */
struct Handler { void (**vtable)(struct Handler *); /* [obj+0] -> slot 0 = execute() */ };

void seq_dispatch_impl_S(void) {                 /* 0x21d4 */
    struct Handler *h = (struct Handler *)0xa02c; /* const16 a2,0xa02c : IRAM-resident instance */
    seq_vthunk(h);                                /* 0x96d4 */
}
void seq_vthunk(struct Handler *h) {             /* 0x96d4 */
    seq_frame_notify_setup();                    /* 0x3aac */
    h->vtable[0](h);                             /* l32i [obj+0]; callx8 : Handler::execute() */
}
```

> **GOTCHA — vtable slot 0 is the *first* function pointer, at `vptr + 0`.** `0x96d4` does
> `l32i a2,[obj+0]` to get the vptr, then `callx8 a2` **on the vptr itself** — i.e. slot 0 is at
> `vptr[0]`, with no `offset-to-top`/`typeinfo` header skip at the call (the header, if any,
> sits *before* the vptr the object stores). Do not add `0x10` to reach "slot 0"; the object's
> `[obj+0]` already points at the executable-pointer array. `[HIGH/OBSERVED]`

> **QUIRK — some handlers inline and skip the thunk.** Real handlers like the
> `Event_Semaphore` op `0xa0` `@0x1e18` (`"S: Event_Semaphore"`) do the work directly in the
> trampoline body instead of routing through `0x96d4`. The table-driven, vtable-dispatched form
> above is the *common* path; the inline form is an optimisation for hot/simple opcodes. Both
> tails return via `j 0x31a3`. `[HIGH/OBSERVED]`

**Unknown-opcode default → ErrorHandler** (the fetch/decode fault path):

```
3198:  l8ui a10,[a4+0]      ; reload the bad opcode byte from the cursor
319b:  call8 0x13f58        ; -> ErrorHandler(opcode)  (HandleBadOpcode)
319e:  j 0x31a3             ; back-edge
--- ErrorHandler 0x13f58 ---
13f5b:  s8i a2,[a1+12]                          ; save opcode arg
13f67:  const16 a10,0x3b11 ; call8 0x18b84       ; LOG "S: ErrorHandler : Bad Opcode(0x%x)" @0x83b11
13f7d:  call8 0x13e00                            ; dispatch error notification
```

> **CORRECTION — `0x3198` decodes as `l8ui a10,[a4+0]; call8 0x13f58`, despite the linear
> sweep.** Under `--adjust-vma=0x0` the sweep enters `0x3198` mid-bundle and prints garbage
> (`const16 a0,0xe500`). An **aligned** decode starting at `0x3198` gives the true
> `l8ui a10,a4,0 ; call8 0x13f58` — the raw bytes are `a2 04 00  e5 db 10`. The default path
> reloads the bad opcode from the cursor and calls the software ErrorHandler. `[HIGH/OBSERVED]`

Sibling ErrorHandler arms: `HandleIllegalInstruction` `@0x13f80` (`"Illegal Instruction(0x%x)"`
`@0x83b35`), `"FP Error(%d)"` (`0x83acb` `@0x13f16`), `"Int Div Zero Error"` (`0x83aeb`
`@0x13f3f`). These field the SEQ-stream fetch/decode fault classes (see
[SEQ Error-Handler / Fault Reporting](error-handler.md)).

> **NOTE — two different "out-of-range" outcomes.** An out-of-range **opcode value** (the
> dispatch index fails `bgeu 177`) is a **hard fault** → ErrorHandler. An out-of-range **fetch
> address** (the PC is outside `[lo,hi]`) is **skip-with-warning**, not a fault — that is the
> address guard in §6. Keep them distinct: the opcode bound and the PC-range bound are separate
> two-level guards. `[HIGH/OBSERVED]`

---

## 6. The PC-range guard `is_pc_in_bounds @0x68d0` (the address-level guard)

Before any fetch DMA/cache-fill is issued, the **fetch address** is range-checked against the
per-engine `[lo,hi]` sequence bounds. String anchors: `"S: is_pc_in_bounds: pc=0x%llx
bound=[0x%llx, 0x%llx]"` `@DRAM 0x81864`, `"...0x%llx smaller than lower bound 0x%llx"`
`@0x8189a`, `"...0x%llx larger than higher bound 0x%llx"` `@0x818d6`.

```
68d0:  entry a1,96            ; (logs pc + [lo,hi] bound)  [entry mid-bundle under linear sweep]
6976:  l32i.n a2,[a1+40] ; l32i.n a3,[a1+44]   ; pc lo/hi
697a:  l32i.n a4,[a1+24] ; l32i.n a5,[a1+28]   ; lower bound lo/hi
6984:  call0 0x57c18                            ; 64-bit compare (pc vs lower)
6989:  bgez a2, 0x69c4                          ; pc >= lower -> check upper ; else BELOW
69b1:  LOG "smaller than lower bound" ; s8i a2,[a1+52]   ; result = 0 (false / OOB)
69c4:  l32i.n a2,[a1+32] ; l32i.n a3,[a1+36]   ; pc vs upper bound
69d2:  call0 0x47c64                            ; 64-bit compare (pc vs upper)
69d7:  blti a2,1, 0x6a12                        ; pc <= upper -> IN BOUNDS (true)
69ff:  LOG "larger than higher bound" ; s8i a2,[a1+52]   ; result = 0 (false)
6a12:  movi.n a2,1 ; s8i a2,[a1+52]             ; result = 1 (true)
6a1a:  l8ui a2,[a1+52] ; retw.n                 ; return the bool
```

```c
/* is_pc_in_bounds @0x68d0 — gate a fetch address against the [lo,hi] sequence bounds. [HIGH/OBSERVED]
 * Returns false (OOB) on pc < lower OR pc > upper; true otherwise. The two 64-bit compare
 * helpers 0x57c18/0x47c64 are the lo/hi long-compare primitives (signed/unsigned form MED). */
bool is_pc_in_bounds(uint64_t pc, uint64_t lower, uint64_t upper) {
    LOG("is_pc_in_bounds: pc=0x%llx bound=[0x%llx, 0x%llx]", pc, lower, upper);
    if (cmp64(pc, lower) < 0) { LOG("smaller than lower bound"); return false; }   /* bgez */
    if (cmp64(pc, upper) > 0) { LOG("larger than higher bound");  return false; }   /* blti a2,1 */
    return true;
}
```

**The two callers** — exactly two `call8 0x68d0` sites:

| Caller | Context | On in-bounds | On out-of-bounds |
|---|---|---|---|
| `0x5d79` | inside `wait_for_cache_line` | `bnez.n a10,0x5dbb` → continue cache-line fetch | `0x5daf` LOG `"WARNING: …next_addr…is out-of-bounds…, skipping it"` `@0x8171c` → `j 0x5e5b` **SKIP** |
| `0x34de` | inside the RTL_PC_check / branch-prefetch path | `bnez.n a10,0x351e` → proceed | skip the prefetch |

> **CORRECTION — the bounds-failure policy is SKIP-with-WARNING, not a hard fault.** A
> reimplementer who maps "PC out of bounds" to a trap will diverge: the firmware on the
> cache-fill path **logs a warning and skips the fetch** (`j 0x5e5b`), leaving the engine to
> resolve the situation through the normal stop/redirect machinery. The hard fault is reserved
> for an out-of-range *opcode value* (§5), a different guard entirely. This is the NX-030
> PC-bounds enforcement; its host-API surface and the `[lo,hi]` provisioning are
> [SEQ PC-Bounds Enforcement + Host API](pc-bounds.md). `[HIGH/OBSERVED]`

---

## 7. The `IsaMaxInstructionSize = 32` FLIX fetch — why the fetch unit is variable-length

The SEQ runs on the Vision-Q7 NX `ncore2gp` config. The **fetch hardware** pulls a fixed
32-byte window per fetch and the **decoder** consumes a variable-length FLIX bundle inside it —
which is exactly why a linear sweep desyncs at the redirect/guard prologues above. The config
fields (all rows `[HIGH/OBSERVED]`):

| Field | Value | Source |
|---|---|---|
| `IsaMaxInstructionSize` | **32** | `ncore2gp-params:31` |
| `XCHAL_MAX_INSTRUCTION_SIZE` | **32** (`/* max instr bytes (3..8) */`) | `core-isa.h:53` |
| `XCHAL_INST_FETCH_WIDTH` | **32** bytes | `core-isa.h:228` |
| `XCHAL_ICACHE_LINESIZE` | **64** bytes | `core-isa.h:291` |
| `XCHAL_HAVE_VISION` | **1** (Vision P5/P6/Q7) | `core-isa.h:206` |
| `XCHAL_VISION_TYPE` | **7** (Q7) | `core-isa.h:208` |
| `XCHAL_VISION_SIMD16` | **32** (512-bit) | `core-isa.h:207` |
| `XCHAL_HAVE_FLIX3` | **0** | `core-isa.h:202` |
| `TargetHWVersion` | `NX1.1.4` | `ncore2gp-params:21` |
| `uarchName` / `arch` | `Cairo` / `Xtensa24` | `ncore2gp-params:24,25` |

> **GOTCHA — `XCHAL_HAVE_FLIX3 = 0` does NOT mean "no FLIX".** The basic 3-way FLIX option is
> off, but the **Vision** option (`XCHAL_HAVE_VISION = 1`, type 7 / Q7) carries its own
> wide-bundle FLIX layer; the 32-byte `MAX_INSTRUCTION_SIZE` / `INST_FETCH_WIDTH` are *its*
> footprint, not the generic FLIX3's. This is the same trap documented for the sibling core in
> [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md): reading `FLIX3=0` as
> "scalar" mis-models the SEQ. The SEQ **is** FLIX/VLIW; only the *generic* FLIX3 flag is 0.
> `[HIGH/OBSERVED]`

> **QUIRK — the *architectural* fetch is 32-byte FLIX, but the SEQ *opcode* is 1 byte.** Two
> fetch granularities coexist: the **hardware** Xtensa fetch unit pulls 32-byte FLIX bundles of
> *the SEQ's own machine code* (the code in `iram.bin` you are reading), while the **SEQ
> program** it runs is a higher-level `'S:'`-opcode stream where each "instruction" is the
> low byte of a 32-bit word read through cursor `a4`. The `IsaMaxInstructionSize=32` governs the
> former (how the SEQ's own code is fetched/decoded); the `opcode − 0x41` dispatch governs the
> latter (how the SEQ interprets its instruction stream). Do not conflate the two. `[HIGH/OBSERVED]`

---

## 8. CSR / DRAM cross-reference

| Offset | `CSR-01` name | Role in the fetch front-end | Sites | Tag |
|---|---|---|---|---|
| CSR `0x1060` | general LR | **PC_lo**: read at enter_run; WRITTEN on redirect | `0x2d33` read / `0x5790` write | `[HIGH/OBSERVED]` |
| CSR `0x1080` | general LR (`0x1060`+`0x20`) | **PC_hi**: read at enter_run; WRITTEN on redirect | `0x2d3d` read / `0x579c` write | `[HIGH/OBSERVED]` |
| DRAM `0x855e0+100` | — | running flag (poll-surprises bit0) | `0x6af4` read | `[HIGH/OBSERVED]` |
| DRAM `0x855e0+24` | — | per-engine descriptor base (pc→iram_pc) | `0x6b70`/`0x57a1` | `[HIGH/OBSERVED]` |
| DRAM `0x85070` | — | HW-decode-active flag (SW vs HW cache) | `0x5ba0`/`0x58dc` read | `[HIGH/OBSERVED]` |
| DRAM `0x855e0` | — | per-engine state struct (dispatch context carrier) | implicit | `[HIGH/INFERRED]` |
| DRAM `0x80814` | — | 178-entry opcode dispatch table | `0x2e6e` base / `0x2e76` jx | `[HIGH/OBSERVED]` |
| DRAM `0x80e38` | — | `"Dispatch opcode=0x%x"` (per-fetch log) | `0x2e54` | `[HIGH/OBSERVED]` |
| DRAM `0x81864`/`0x8189a`/`0x818d6` | — | `is_pc_in_bounds` + lower/higher sub-msgs | `0x696d`/`0x69b1`/`0x69ff` | `[HIGH/OBSERVED]` |
| DRAM `0x81951`/`0x8197b` | — | `sunda_fetch` redirect pc(64) / iram_pc(32) | `0x2dcb`/`0x2de0` | `[HIGH/OBSERVED]` |
| DRAM `0x8134a`/`0x81367` | — | `Redirecting to pc` / `Redir: phase,iram,num` | `0x5768`/`0x58b8` | `[HIGH/OBSERVED]` |
| DRAM `0x8171c` | — | `wait_for_cache_line` out-of-bounds WARNING | `0x5daf` | `[HIGH/OBSERVED]` |
| DRAM `0x81a62` | — | `"S: Redirect"` (commit push) | `0x6bbf` | `[HIGH/OBSERVED]` |
| DRAM `0x83b11`/`0x83b35` | — | ErrorHandler Bad Opcode / Illegal Instr | `0x13f67`/`0x13f8f` | `[HIGH/OBSERVED]` |

> **NOTE — the CSR aperture absolute base is LOW; the register identities are HIGH.** The
> `movi a4,0x400` high half implies an absolute aperture at `0x00400000`, but the flat-`0x0`
> alias is not resolvable from this image alone. The **register identities by low offset**
> (`0x1060` PC_lo, `0x1080` PC_hi) are HIGH and unaffected by that ambiguity. `[LOW/INFERRED on
> the absolute base; HIGH/OBSERVED on the offsets]`

---

## 9. Adversarial self-verification

Five strongest claims and the byte evidence for each (carve SHAs `iram.bin 8e4412b9…`,
`dram.bin 7bdf6ed7…`):

| # | Claim | Challenge | Verdict |
|---|---|---|---|
| 1 | **Fetch unit**: cursor `a4` reads a 32-bit word, low byte is the opcode | Decode `0x2e50..0x2e5d`: is it really `s32i [a4]` then `l32i [a4]`? | **HOLDS.** `2e50 s32i.n a2,a4,0`; `2e52/2e5d l32i.n …,a4,0`; `2e5f addi a2,a2,-65` on the word. OBSERVED. |
| 2 | **Normal PC-advance by bundle**: advance = notify-consume `0x1c64` then `s32i a2,[a4]` | Is `0x1c64` the advance, and is the store the commit? | **HOLDS, refined.** `2e46 call8 0x1c64`(→`0x3a78`); `2e50 s32i.n a2,[a4]`. The "advance" is the **notify-consume result written to the cursor**, not a raw PC+len. The exact `a4` reload is FLIX-desynced (MED) but the store/read are HIGH. |
| 3 | **Redirect mechanism**: redirect rewrites the 64-bit PC into CSR `0x1060`/`0x1080` | Decode `0x5786..0x579c`: does it really write both halves to the CSR pair? | **HOLDS.** `5788 l32i.n a3,[a3+0]`→`5790 s32i.n a3,[a4+0]` (PC_lo→`0x1060`); `5794 l32i.n a3,[a3+4]`→`579c s32i.n a3,[a4+32]` (PC_hi→`0x1080`). Register-identity twin of the enter_run read. OBSERVED. |
| 4 | **`IsaMaxInstructionSize=32` FLIX fetch** | Does the shipped config say 32, and is the SEQ actually FLIX given `FLIX3=0`? | **HOLDS, corrected.** `ncore2gp-params:31 IsaMaxInstructionSize=32`; `core-isa.h:53 MAX_INSTRUCTION_SIZE=32`, `:228 INST_FETCH_WIDTH=32`. `FLIX3=0` but `HAVE_VISION=1`/`VISION_TYPE=7` carries the FLIX layer. OBSERVED. |
| 5 | **Dispatch handoff**: table→trampoline→Handler vtable slot 0 `callx8` | Dump the table; trace `'S'` to the thunk; is slot 0 at `vptr+0`? | **HOLDS.** Table `@0x80814`, 178 entries, 55 real / 123 default `0x3198`, all in-range. `'S'(0x53)→idx18→0x30a5→call8 0x21d4→a2=0xa02c→thunk 0x96d4: l32i [obj+0]; callx8 a2`. OBSERVED. |

> **CORRECTION applied during self-verify (claim 1 / default path).** The default target
> `0x3198` decodes as garbage under the linear sweep (it enters mid-bundle). An **aligned**
> decode starting at `0x3198` (raw bytes `a2 04 00 e5 db 10`) gives the true
> `l8ui a10,a4,0 ; call8 0x13f58` → ErrorHandler. The default path is real; the sweep artifact
> was a false alarm. Fixed in §5.

---

## 10. Honesty ledger / desync spans

**HIGH/OBSERVED:**

- Carve reproduced: `iram.bin` 116768 B / sha256 `8e4412b9…ab9ed70a`, `dram.bin` 28448 B /
  sha256 `7bdf6ed7…d6816ecd`; head bytes match the reset-vector anchor `j 0x1dc`.
- The full Sunda software-fetch FSM body (poll `0x6af4` → surprise check
  `sunda_check_surprises` `0x6b0c` → redirect block `0x2daa` →
  advance `0x1c64` → fetch `[a4]` → `opcode−0x41` → `bgeu 177` → table `@0x80814` `jx` →
  back-edge `0x31a3`), **including** the redirect block `0x2db3..0x2def` (re-synced).
- 64-bit PC in CSR `0x1060`/`0x1080`; redirect writes the new PC there (`0x5790`/`0x579c`).
- `is_pc_in_bounds @0x68d0`: lower/upper sub-messages, in-bounds=1/oob=0, **two** callers
  (`0x5d79` skip+warn, `0x34de` prefetch); exactly two `call8 0x68d0` sites.
- 178-entry table re-decoded: `0x80814`, default `0x3198`, 55 real / 123 default, all in-range.
- Dispatch handoff = trampoline → Handler object → vtable execute() via thunk `0x96d4`
  (`l32i [obj+0]; callx8`). Default → ErrorHandler `0x13f58` (Bad Opcode).
- Config: `IsaMaxInstructionSize=32`, `INST_FETCH_WIDTH=32`, Vision-Q7 FLIX.

**MED/INFERRED:**

- The per-iteration **reload** of cursor `a4` (bundle `0x2e46..0x2e4e` is FLIX-desynced); `a4`
  confirmed to address the fetch word, but whether it re-aliases CSR `0x1060` or a DRAM
  cache-line slot each iteration is inferred.
- The exact signed/unsigned form of the two 64-bit compare helpers (`0x57c18`/`0x47c64`) used
  by `is_pc_in_bounds` (FLIX-prologue desync).
- `0x4c3c` redirect-target field math (small getter, FLIX prologue).

**LOW:**

- The cache-victim/eviction arithmetic in `0x5750` past `0x58f0`
  ([SEQ IRAM Instruction Cache / Overlay](iram-cache.md) scope).
- CSR aperture absolute base alias (`0x0` vs `0x00400000`).

**OUT OF SCOPE (noted, not decoded):** the **HW-decode** fetch front-end body (`0x31ac`, the
`"Seq Loop, iter"` FSM) is a flagged FLIX desync; only its head/exits are known
([HW-Decode vs Sunda Dual Fetch](dual-fetch.md)). The instruction-cache internals are
[SEQ IRAM Instruction Cache / Overlay](iram-cache.md). The branch/prefetch arming detail is
[SEQ Branch + Prefetch-Hint](branch-prefetch.md); the `[lo,hi]` provisioning + host API is
[SEQ PC-Bounds Enforcement + Host API](pc-bounds.md); the Handler-object `execute()` contract
is [SEQ Decode / Dispatch Hub](dispatch-hub.md).

---

## See also

- [SEQ Main FSM Loop](main-loop.md) — the poll/stop FSM this front-end sits inside.
- [SEQ Branch + Prefetch-Hint](branch-prefetch.md) — the redirect/prefetch arming.
- [SEQ PC-Bounds Enforcement + Host API](pc-bounds.md) — the `[lo,hi]` guard's host surface.
- [SEQ IRAM Instruction Cache / Overlay](iram-cache.md) — the cache fill the fetch rides on.
- [SEQ Decode / Dispatch Hub](dispatch-hub.md) — the Handler-object table + `execute()` contract.
- [SEQ Boot / Entry Path](boot.md) — `enter_run`, the HW-decode-active flag.
- [HW-Decode vs Sunda Dual Fetch](dual-fetch.md) — the two fetch front-ends.
- [SEQ Error-Handler / Fault Reporting](error-handler.md) — the Bad/Illegal-Opcode fault path.
- [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md) — why `ncore2gp` is the only
  config that decodes SEQ FLIX, and the `FLIX3=0`-is-not-scalar trap.
