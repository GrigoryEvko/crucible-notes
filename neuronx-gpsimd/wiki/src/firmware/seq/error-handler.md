# SEQ Error-Handler / Fault Reporting

This page reconstructs the GPSIMD **SEQ engine**'s fault-handling and
error-reporting subsystem — the small, self-contained translation unit that every
bad opcode, out-of-range program counter, integer divide-by-zero, FP arithmetic
fault, Xtensa hardware exception, and failed assertion ultimately routes into. It
**owns `0x3198`**, the dispatch-default stub that the 178-entry SEQ decode table's
123 default slots jump to; it owns the six named fault entry points behind that
stub; it owns the binary *recoverable-vs-fatal* policy; and it owns the
device→host `notification_t` error record that a host runtime reads to learn
**which engine** faulted and **why**.

This is **the SEQ engine's own firmware** — windowed-ABI Xtensa code on the
`ncore2gp` "Cairo" core — not the microcode it interprets. The sibling front-end
pages all funnel here: a decode-table miss in
[SEQ Decode / Dispatch Hub](dispatch-hub.md) lands on `0x3198`; the fetch/redirect
in [SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md) hands the opcode to
that table; the poll/stop FSM in [SEQ Main FSM Loop](main-loop.md) is the loop the
fatal path *kills*; and the **soft** speculative-PC guard in
[SEQ PC-Bounds Enforcement + Host API](pc-bounds.md) is the one "out-of-bounds"
class that pointedly does **not** reach this handler (see §8). Read those if the
fetch→decode→dispatch flow upstream of a fault is unclear; this page picks up at
the moment a fault is detected.

Everything below is **byte-pinned to a shipped artifact**. The anchor
image is the carved `CAYMAN_NX_POOL_DEBUG` device firmware extracted from
`libnrtucode.a` (members `img_CAYMAN_NX_POOL_DEBUG_IRAM_contents.c.o` for code,
`…_DRAM_contents.c.o` for strings), disassembled with the native
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, Cairo µarch, Xtensa24, RI-2022.9,
`TargetHWVersion=NX1.1.4`, `IsaMaxInstructionSize=32` FLIX/VLIW) that ships inside
the gpsimd-tools package. The DEBUG build **keeps** the `'S:'` format strings *and*
the source-file-name strings (`error_handler.cpp`, `error_notifications.cpp`,
`signal_handler.cpp`) baked into `.rodata` — these recovered strings are what name
the handlers on this page, and they are themselves binary evidence.

Confidence and evidence tags follow the project
[Confidence Model](../../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**.

---

## 0. Carve, address model, the FLIX trap

The error subsystem is one translation unit in IRAM, plus a few shared sinks:

| Region | Span | Contents |
| --- | --- | --- |
| Error TU (code) | IRAM `0x13d88`–`0x14160` | the 16 `entry`-function bodies of §2 |
| Shared report/halt sinks | IRAM `0xa2e0`/`0xa304`/`0xa33c`/`0xa450`/`0x1cf8`/`0x3a44` | assert primitive, halt-dispatch, report sink, Setup-Halt |
| Boot install | IRAM `0x245e`/`0x2461` | wires up FP-exception arming + signal table |
| Early emitter | IRAM `0x1a80` | a pre-TU fatal emitter (code `'B'`) |
| Error strings / jump tables / signal table | DRAM `0x83a30`–`0x83bc8` | §7 |

**Address model.** All IRAM offsets equal the device IRAM VA (the reset vector is
byte 0). DRAM lives at VA `0x80000`, so a DRAM string's *file offset* = VA −
`0x80000`. The firmware materialises a DRAM VA with a `const16 aX,8 ; const16
aX,0xNNNN` pair (high half = `0x0008`), so e.g. `const16 a10,8 ; a10,0x3b11`
addresses DRAM VA `0x83b11`. **HIGH/OBSERVED** — the
`0x83b11` literal at `0x13f64` resolves to the verbatim `"S: ErrorHandler : Bad
Opcode(0x%x)"` string.

> **GOTCHA — FLIX desync inside every handler.** The `ncore2gp` config is a
> Vision-Q7 FLIX/VLIW core (`IsaMaxInstructionSize = 32`). A *linear* sweep
> desyncs across the variable-length bundles that schedule the `call8`-to-logger
> arguments. Concretely, the span right after each `call8 0x18b84` (the `'S:'`
> logger) decodes as garbage (`mulsh`, `l32r`, `orb` artifacts) in a single-pass
> disassembly. **Every body on this page was re-synced** by forcing
> `--start-address=` at the next real instruction boundary (typically the `l8ui`
> that reloads the argument, or the terminal `call8 0x13e00`/`0x13e30`). Do not
> trust a linear-sweep rendering of these functions; the re-synced bytes are what
> appears below.

The carve reproduces the shared anchor hashes exactly
(`iram.bin` SHA-256 `8e4412b9…`, `dram.bin` `7bdf6ed7…`), so this is the same
firmware as the sibling pages. **HIGH/OBSERVED.**

---

## 1. Executive model — six entry points, one pipeline, two policies

The fault subsystem has **six named fault entry points**, a uniform three-stage
reporting pipeline, and a **binary** recoverable-vs-fatal policy. Of the six,
exactly **one** (FP arithmetic) is recoverable; the other five **halt the engine
forever**.

| Fault entry point | Address | Error string (DRAM) | Code byte | Policy |
| --- | --- | --- | --- | --- |
| `HandleBadOpcode(opcode)` | `0x13f58` | `"Bad Opcode(0x%x)"` @ `0x83b11` | `0` | **FATAL** (spin) |
| `HandleIllegalInstr(op)` | `0x13f80` | `"Illegal Instruction(0x%x)"` @ `0x83b35` | (same builder) | **FATAL** (spin) |
| `HandleIntDivZero()` | `0x13f34` | `"Int Div Zero Error"` @ `0x83aeb` | `2` | **FATAL** (spin) |
| `HandleFPError(status)` | `0x13eb0` | `"FP Error(%d)"` @ `0x83acb` | FP-mapped | **RECOVERABLE** (return) |
| `signal_handler(signum)` | `0x14014` | *(none; raises code `'A'`)* | `'A'`=`0x41` | **FATAL** (spin) |
| `assert_fail(msg,file,line)` | `0xa304` | `"Assertion failure! %s(%s:%u)"` @ `0x82210` | `0` | **FATAL** (spin) |
| *(early emitter)* | `0x1a80` | *(none; raises code `'B'`)* | `'B'`=`0x42` | **FATAL** (spin) |

Every fault runs the same pipeline:

1. **Log** a one-line `'S:'` diagnostic via the per-engine printf-logger `0x18b84`
   (→ the host stdout SPSC ring).
2. **Build** a packed error-notification record — `{block_id, error_code,
   opcode/aux}` — in `build_error_record` (`0x13e40`), tagging *which engine*
   (`get_block_id`) and *which class* (the code byte).
3. **Raise**: write the record to custom TIE user-register **UR#`0x15` (21)** in
   the `raise_error` core (`0x13e18`), then dispatch a host notification through
   the report sink `0xa450`.

Then the policy branches:

- **RECOVERABLE** (`HandleFPError` only): `raise()` returns; execution continues.
- **FATAL** (all others): `raise()` calls the halt-dispatch (`0xa2e0`) — log
  `"S: Setup Halt"`, save the resume PC, log `"S: Entering HALT"`, write the halt
  CSR — then **spins forever** at `j 0x13e14`.

**HIGH/OBSERVED** for the whole pipeline; per-step disassembly follows.

> **The three "out-of-bounds" / fault classes, kept straight.** This is the single
> most reconcile-prone framing on the page, so state it once:
>
> | Class | Where detected | Outcome |
> | --- | --- | --- |
> | **HARD** (this TU) | bad opcode, illegal/unsupported sub-op, div0, failed assert, any of 6 POSIX signals (= the Xtensa HW exceptions) | infinite spin / engine dead |
> | **RECOVERABLE** (this TU) | FP arithmetic faults (FCR-trapped status) | logged + reported, then *continues* |
> | **SOFT** (NOT this TU) | speculative PC-range guard `is_pc_in_bounds @0x68d0` | skip-with-WARNING, never enters this handler |
>
> The SOFT class is documented on [SEQ PC-Bounds](pc-bounds.md); §8 here contrasts
> it. The opcode-index/dispatch bound is **hard** (→ `0x3198` → spin); the
> speculative PC range is **soft** (skip the prefetch). "Out of bounds" is
> policy-split. **HIGH/OBSERVED.**

---

## 2. The dispatch-default stub `0x3198` — the entry the table points at

The 178-entry SEQ decode table (DRAM `0x80814`, owned by
[SEQ Decode / Dispatch Hub](dispatch-hub.md)) has **123 entries** that point at a
single 3-instruction stub at IRAM `0x3198`; the other **55** point at real
handlers. Counted straight from the table words (not a decompile):
`0x3198` appears **123** times, **55** entries are real, **56** distinct targets.

```text
00003198 <dispatch-default>:
    3198:  a20400      l8ui   a10, a4, 0      ; a10 = the un-decodable opcode byte
    319b:  e5db10      call8  0x13f58         ; -> HandleBadOpcode(opcode)
    319e:  460000      j      0x31a3          ; back-edge: NEVER taken (handler spins)
```

So `0x3198` is a thin trampoline: it reloads the opcode byte from the fetch
pointer `a4`, passes it to `HandleBadOpcode`, and falls through to a `j` that can
never execute because `HandleBadOpcode` ends in an infinite spin and never returns.
**HIGH/OBSERVED.**

> **NOTE — "ErrorHandler" names two things, by design.** Sibling passes sometimes
> say "ErrorHandler `@0x3198`" and sometimes "`@0x13f58`". Both are correct and
> not in conflict: `0x3198` is the *table-target stub*; `0x13f58`
> (`HandleBadOpcode`) is the *handler body* the stub tail-calls. This page treats
> `0x3198` as the dispatch-default entry and `0x13f58` as its implementation. No
> CORRECTION needed — just the two-address distinction.

---

## 3. The error TU function map

All 16 `entry`-instruction function starts in the TU, enumerated from raw bytes
(`byte0 = 0x36`, `byte2 = 0x00`, low nibble of `byte1 == 1`) and re-synced
per-function past their FLIX log bundles. **HIGH/OBSERVED.**

| Address | Function | Role |
| --- | --- | --- |
| `0x13d88` | *(small leaf)* | `entry a1,32` |
| `0x13d90` | `get_block_id()` | engine-id → block-id remap (§5) |
| `0x13dec` | `read_engine_id_global()` | returns `*(u32*)DRAM[0x85f38]` |
| `0x13e00` | **FATAL raise wrapper (sev=2)** | → Halt → SPIN (§6) |
| `0x13e18` | `raise_error(sev, record)` core | `wur UR#0x15`; `call8 0xa450` (§6) |
| `0x13e30` | **RECOVERABLE raise wrapper (sev=1)** | → RETURN (§6) |
| `0x13e40` | `build_error_record(code, aux)` | the notification packer (§5) |
| `0x13e98` | `enable_fp_exceptions()` | `rur.fcr \| 0x7c ; wur.fcr` (boot, §7) |
| `0x13eb0` | `HandleFPError(fp_status)` | **RECOVERABLE** (§4d) |
| `0x13f34` | `HandleIntDivZero()` | **FATAL** (§4c) |
| `0x13f58` | `HandleBadOpcode(opcode)` | **FATAL** (§4a) |
| `0x13f80` | `HandleIllegalInstr(opcode)` | **FATAL** (§4b) |
| `0x13fa8` | `register_signal_handlers()` | boot install (§4e) |
| `0x14014` | `signal_handler(signum)` | **FATAL** (§4e) |
| `0x14128`/`0x14138` | *(signal-table leaves)* | small helpers |

Plus the shared sinks `0xa304` (`assert_fail`), `0xa33c` (`"S: Halt"`), `0xa450`
(report sink), `0xa2e0` (halt-dispatch), and the early emitter `0x1a80`.

---

## 4. The six fault entry points (instruction-exact)

### 4a. `HandleBadOpcode` `@0x13f58` — FATAL · code `0`

Reached from the dispatch default (§2): the 178-way table miss reloads the bad
opcode byte and `call8 0x13f58`.

```text
00013f58 <HandleBadOpcode>:
    13f58:  366100      entry  a1, 48
    13f5b:  22410c      s8i    a2, a1, 12        ; save opcode arg byte
    13f61:  b2010c      l8ui   a11, a1, 12       ; a11 = opcode  (printf %x arg)
    13f64:  a40800      const16 a10, 8
    13f67:  a4113b      const16 a10, 0x3b11      ; a10 = DRAM "Bad Opcode(0x%x)"
    13f6a:  a5c104      call8  0x18b84           ; LOG "S: ErrorHandler : Bad Opcode(0x%x)"
    ; --- re-synced past the FLIX log-arg bundle (linear sweep desyncs here) ---
    13f71:  b2010c      l8ui   a11, a1, 12       ; a11 = opcode (aux)
    13f74:  0c0a        movi.n a10, 0            ; a10 = error CODE 0
    13f76:  a5ecff      call8  0x13e40           ; build_error_record(code=0, opcode) -> a10
    13f79:  a921        s32i.n a10, a1, 8
    13f7b:  a821        l32i.n a10, a1, 8        ; a10 = packed record
    13f7d:  25e8ff      call8  0x13e00           ; FATAL raise(sev=2) -> Halt -> SPIN
```

An un-decodable SEQ opcode is a **hard fault**: logged, reported, engine spins
forever. **HIGH/OBSERVED** (re-synced from `0x13f71`).

### 4b. `HandleIllegalInstr` `@0x13f80` — FATAL

Same shape as 4a but with the `"Illegal Instruction(0x%x)"` string at DRAM
`0x83b35` and `call8 0x18b84`; terminal `call8 0x13e00` (FATAL).

> **QUIRK — "Illegal" ≠ "unknown opcode".** `HandleIllegalInstr` is **not** the
> table miss; it is raised for *valid-opcode-but-unsupported-operation* sub-cases
> and in-handler illegal-state checks. The clearest funnels: the RNG handlers
> reject an unsupported algorithm on the POOL engine — `movi a10,119 ; call8
> 0x13f80` (opcode `'w'`, after logging `"S: RandGetState : rand_algorithm(0x%x)
> not …POOL"` @ `0x80e51`) and `movi a10,120 ; call8 0x13f80` (`'x'`,
> `RandSetState` @ `0x80e99`). The decode succeeded; the *operation* is illegal on
> this engine. Still FATAL. **HIGH/OBSERVED** for the string + the `call8` sites.

### 4c. `HandleIntDivZero` `@0x13f34` — FATAL · code `2`

```text
00013f34 <HandleIntDivZero>:
    13f34:  366100      entry  a1, 48
    13f3f:  a4eb3a      const16 a10, 0x3aeb      ; (high half built prior) DRAM "Int Div Zero Error"
    13f42:  25c404      call8  0x18b84           ; LOG "S: ErrorHandler : Int Div Zero Error"
    ; --- re-synced past the FLIX log bundle ---
    13f4c:  0c2b        movi.n a11, 2            ; error CODE 2
    13f4e:  25efff      call8  0x13e40           ; build_error_record(.., code 2)
    13f51:  a931        s32i.n a10, a1, 12
    13f53:  a831        l32i.n a10, a1, 12
    13f55:  a5eaff      call8  0x13e00           ; FATAL -> SPIN
```

Both call sites are guarded by a divisor-is-zero test in the tensor-scalar /
tensor-tensor arithmetic handlers — e.g. `0xbf5f: bnez.n a2, 0xbf6c` (divisor
non-zero ⇒ skip) *else* `0xbf64: call8 0x13f34`. A zero divisor is therefore a
**hard fault**, not a silently-saturated result. **HIGH/OBSERVED.**

### 4d. `HandleFPError` `@0x13eb0` — the ONE RECOVERABLE class

```text
00013eb0 <HandleFPError>:
    13eb0:  366100      entry  a1, 48
    13eb3:  ...         s32i.n a2, a1, 8         ; a2 = fp_status (1..16)
    13eb5:  ...         movi.n a2,-1 ; s8i ..    ; errcode = -1 (default/unknown)
    13eba:  2821        l32i.n a2, a1, 8
    13ebc:  0b22        addi.n a2, a2, -1        ; index = status - 1
    13ebe:  f6b236      bgeui  a2, 16, 0x13ef8   ; out of [0,15] -> DEFAULT (assert)
    13ec1:  340800      const16 a3, 8
    13ec4:  34703a      const16 a3, 0x3a70       ; FP-status jump table @DRAM 0x83a70
    13ec7:  3022a0      addx4  a2, a2, a3
    13eca:  2802        l32i.n a2, a2, 0
    13ecc:  a00200      jx     a2                ; computed jump into the 16-arm table
    ...
    13f0d:  ...         l8ui   a11, a1, 12       ; a11 = mapped errcode
    13f13:  a4cb3a      const16 a10, 0x3acb      ; DRAM "FP Error(%d)"
    13f19:  a5c604      call8  0x18b84           ; LOG "S: ErrorHandler : FP Error(%d)"
    ; --- re-synced past the FLIX log bundle ---
    13f2d:  25f0ff      call8  0x13e30           ; RECOVERABLE raise(sev=1)
    13f30:  1df0        retw.n                   ; *** RETURNS — execution CONTINUES ***
```

The 16-entry table at DRAM `0x83a70` remaps the one-hot `fp_status` bit into a
*numbered* error code, decoded from the LE words:

| `fp_status` | table arm | error code |
| --- | --- | --- |
| `1` | `0x13ed8` | `16` |
| `2` | `0x13ef0` | `8` |
| `4` | `0x13ee8` | `4` |
| `8` | `0x13ed0` | `2` |
| `16` | `0x13ee0` | `1` |
| `3,5,6,7,9..15` (non-power-of-two) | `0x13ef8` | **DEFAULT → assert** |

The DEFAULT arm `0x13ef8` builds `const16 a10,0x3ab0` (`"fp_error"`),
`const16 a11,0x3ab9` (`"error_handler.cpp"`), `movi a12,40` (line 40), and
`call8 0xa304` — i.e. a non-single-bit status is an assertion failure (itself
FATAL). The single caller is at `0xc2ff`, inside a loop that tests up to 5 FP
status flags and calls `HandleFPError` once per set flag. **HIGH/OBSERVED** —
re-verified all 16 table words and re-synced the `call8 0x13e30 ; retw.n`
recoverable tail.

> **GOTCHA — recoverable does not mean masked.** An FP fault still *logs* and
> *reports* a host notification (sev 1). The only difference from the fatal path
> is that `raise()` returns instead of halting. A host runtime distinguishes
> "engine alive, reported an FP fault" (sev 1) from "engine hung" (sev 2) purely
> by the severity byte and by whether further notifications keep arriving.

### 4e. `signal_handler` `@0x14014` + `register_signal_handlers` `@0x13fa8` — FATAL

`register_signal_handlers` is called once at boot. It copies the 6-word signal
table from DRAM `0x83b70` = `{6, 2, 4, 8, 0xb, 0xf}` onto its frame and loops
`i = 0..5`, installing one handler:

```text
00013fa8 <register_signal_handlers>:
    ...
    13fd2:  f6623b      bgeui  a2, 6, 0x14011    ; loop bound i<6
    13fe2:  3022a0      addx4  a2, a2, a3
    13fe5:  a802        l32i.n a10, a2, 0        ; a10 = signum = table[i]
    13fe7:  b40100      const16 a11, 1
    13fea:  b41440      const16 a11, 0x4014      ; a11 = handler addr 0x14014
    13fed:  a5c704      call8  0x18c68           ; signal(signum, 0x14014)
    13ff0:  660a10      bnei   a10, -1, 0x14004  ; if registration failed:
    13ffc:  a4883b      const16 a10, 0x3b88      ; "...signal_handler.cpp:9 0"
    14001:  25c904      call8  0x18c94           ; LOG the failure
    1400e:  86efff      j      0x13fd0           ; next i
    14011:  1df0        retw.n
```

`0x18c68` is a libc-style `signal()`: it validates `signum-1 <= 42` and
`handler != -1`, stores the handler into a sig-table at DRAM `0x86b50` indexed by
signum (`addx4 a4, a4, 0x6b50`), and returns `-1` on bad args. The sig-table is
runtime-zero in the static image. The 6 numbers are the canonical POSIX signal
numbers:

`2 = SIGINT`, `4 = SIGILL`, `6 = SIGABRT`, `8 = SIGFPE`, `11 = SIGSEGV`,
`15 = SIGTERM`.

The handler body is a single catch-all:

```text
00014014 <signal_handler>:
    14014:  366100      entry  a1, 48
    14017:  2931        s32i.n a2, a1, 12        ; a2 = signum
    14019:  4c12        movi.n a2, 65            ; error code = 'A' (0x41)
    1401b:  224108      s8i    a2, a1, 8
    1401e:  25d7ff      call8  0x13d90           ; get_block_id -> a10
    14021..53:                                   ; pack block_id<<4 | code-nibble (see §5)
    14056:  a22102      l32i   a10, a1, 8         ; a10 = packed record
    14059:  65daff      call8  0x13e00           ; FATAL -> SPIN
```

So **any delivered signal hard-faults the engine** with code `'A'`. This is the
on-device coupling to the Xtensa hardware-exception model: the device's trap
delivery raises one of these six signals, which dispatches to `0x14014` →
FATAL spin. **HIGH/OBSERVED** for the table, registration, and handler body;
**MED/INFERRED** that the device's `exccause`/HW-exception vectors map onto these
specific POSIX signal numbers — grounded in the libc `signal()` shape and the
`{SIGILL, SIGFPE, SIGSEGV, SIGABRT}` set matching the Xtensa fault classes
(illegal instruction, divide-by-zero, load/store/stack-limit, abort) exactly.

> **NOTE — forward link to the interrupt/exception Part.** The full Xtensa
> hardware-exception roster (illegal instruction, load/store, integer-divide,
> window over/underflow, the ISL/KSL stack-limit violation, break/debug,
> interrupt/syscall) and the precise `exccause`→signal binding live on the
> [interrupt handler-bodies](../../control/interrupt/handler-bodies.md) and
> [Q7 surprises binding](../../control/interrupt/q7-surprises-binding.md) pages
> (**forward links**). The on-device producer side documented here is HIGH/OBSERVED;
> the HW-vector mapping is the piece those pages pin.

---

## 5. The record builder, block-id, and the recoverable-vs-fatal switch

### 5a. `get_block_id` `@0x13d90` — *which engine* faulted

```text
00013d90 <get_block_id>:
    13d90:  366100      entry  a1, 48
    13d93:  ...         movi a2,7 ; s32i ..      ; default result 7
    13d99:  ...         call8  0x13dec           ; a10 = engine-id global *(u32*)0x85f38
    13d9c:  ...         bgeui  a10, 6, 0x13dd0   ; engine-id >= 6 -> DEFAULT (assert)
    13d9f:  ...         const16 a2,8; a2,0x3a30  ; 6-entry block-id table @DRAM 0x83a30
            ...         addx4 a2,a10,a2; l32i; jx ; computed jump
    ; JT arms -> block_id:  0->2  1->1  2->3  3->DEFAULT  4->4  5->5
    13dd0:  (DEFAULT)   const16 a10,0x3a48 ("get_block_id")
            ...         const16 a11,0x3a55 ("error_notifications.cpp")
            ...         movi a12,38 ; call8 0xa304   ; assert_fail(line 38)
    13de5:  ...         l32i.n a2,a1,12 ; retw.n     ; return block_id
```

`read_engine_id_global` `@0x13dec` is `const16 a2,8 ; a2,0x5f38 ; l32i.n a2,[a2]`
— it reads `*(u32*)DRAM[0x85f38]`, the engine-identity global (runtime-zero in the
static image, set at boot). The 6-entry table at DRAM `0x83a30` remaps it to the
host-visible `block_id`. The table words decode as:
`{0x13dad, 0x13db4, 0x13dc2, 0x13dd0, 0x13dc9, 0x13dbb}`, i.e. engine-id
`{0→2, 1→1, 2→3, 3→assert, 4→4, 5→5}`. **HIGH/OBSERVED** that it reads `0x85f38`
and remaps via the JT; **INFERRED** (from the `get_block_id` symbol string) that
`0x85f38` *is* the current engine id.

### 5b. `build_error_record` `@0x13e40` — the `notification_t` packer

```text
00013e40 <build_error_record>:
    13e40:  366100      entry  a1, 48
    13e43:  22410c      s8i    a2, a1, 12        ; [12] = code byte
    13e46:  324108      s8i    a3, a1, 8         ; [8]  = aux byte (opcode / signum)
    13e49:  22010c      l8ui   a2, a1, 12
    13e4c:  224104      s8i    a2, a1, 4         ; [4]  = code (raw copy, return value)
    13e4f:  25f4ff      call8  0x13d90           ; a10 = block_id
    13e52:  220106      l8ui   a2, a1, 6         ; assemble 16-bit field from [5]/[6]
    13e55:  802211      slli   a2, a2, 8
    13e58:  320105      l8ui   a3, a1, 5
    13e5b:  3a22        add.n  a2, a2, a3
    13e5d:  a03034      extui  a3, a10, 0, 4     ; a3 = block_id & 0xf
    13e60:  7c04        movi.n a4, -16           ; mask = 0xfffffff0
    13e62:  402210      and    a2, a2, a4        ; clear low nibble of the field
    13e65:  302280      add    a2, a2, a3        ; OR in (block_id & 0xf)  [first pack]
    13e68:  224105      s8i    a2, a1, 5
    13e6b:  202841      srli   a2, a2, 8
    13e6e:  224106      s8i    a2, a1, 6
    13e71:  6548f6      call8  0xa2f8            ; external-reg notify helper (rer/wer)
    13e74:  220106      l8ui   a2, a1, 6        ; second pass: fold in (code & 0xf) | block_id<<4
    13e77:  220105      l8ui   a2, a1, 5
    13e7a:  c03a11      slli   a3, a10, 4        ; a3 = block_id << 4
    13e7d:  202034      extui  a2, a2, 0, 4      ; low nibble of [5]
    13e80:  3a22        add.n  a2, a2, a3        ; [5] = (low4) | (block_id<<4)
    13e82:  224105      s8i    a2, a1, 5
    13e85:  202841      srli   a2, a2, 8
    13e88:  224106      s8i    a2, a1, 6
    13e8b:  220108      l8ui   a2, a1, 8        ; [7] = aux byte
    13e8e:  224107      s8i    a2, a1, 7
    13e91:  2811        l32i.n a2, a1, 4        ; return the record word [4..7]
    13e93:  1df0        retw.n
```

The record is a packed 4-byte tuple assembled in stack bytes `[a1+4 .. a1+7]` and
returned as one word:

| Record byte | Field | Meaning |
| --- | --- | --- |
| `[4]` | `error_code` | the class byte (`0` BadOpcode/assert, `2` Div0, FP-mapped, `'A'`/`'B'`) |
| `[5]` | `(code & 0xf) \| (block_id << 4)` | low nibble = code class, high nibble = engine block-id |
| `[6]` | high byte of the assembled field | opcode/aux high bits |
| `[7]` | `aux` | the secondary byte (`opcode` for §4a, `signum` for §4e) |

`HandleBadOpcode` (code `0`), `HandleIntDivZero` (code `2`), `signal_handler`
(code `'A'`), and the early emitter `0x1a80` (code `'B'`, `movi.n a2,66`) all build
**this same record shape**; the code byte is what distinguishes the class in the
host-visible record. **HIGH/OBSERVED** that a packed record is built and that the
`block_id<<4 | code-nibble` packing is exactly as shown; **MED/INFERRED** for the
full semantic meaning of `[6]`/`[7]` beyond "opcode/aux".

> **CORRECTION (vs the backing survey's framing).** The survey lists `[a1+5..7]`
> only as "{block_id, 4-bit code, opcode/aux}" with the byte semantics partly
> inferred. The re-disassembly above pins the packing two passes precisely:
> `0x13e5d`–`0x13e65` first OR-folds `block_id & 0xf` into the field, and
> `0x13e7a`–`0x13e82` re-packs `[5] = (low4) | (block_id << 4)`. The return value
> is `[a1+4]` (the raw code), loaded at `0x13e91`. The table above reflects the
> instruction-exact two-pass packing, not the single-line summary.

### 5c. `raise_error` core `@0x13e18` — the host-visible write

```text
00013e18 <raise_error>:
    13e18:  366100      entry  a1, 48
    13e1b:  22410c      s8i    a2, a1, 12        ; a2 = severity byte
    13e1e:  3921        s32i.n a3, a1, 8         ; a3 = packed record
    13e20:  222102      l32i   a2, a1, 8         ; a2 = record
    13e23:  20 15 f3    .byte ...                ; *** wur a2, UR#0x15 (21) ***
    13e26:  a2010c      l8ui   a10, a1, 12       ; a10 = severity
    13e29:  6562f6      call8  0xa450            ; report sink (host notify dispatch)
    13e2c:  1df0        retw.n
```

> **GOTCHA — the unnamed user-register `0x15`.** `objdump` emits `.byte 20 15 f3`
> at `0x13e23` because this core's config has **no symbolic name** for user
> register `0x15`. But the `f3`-major **WUR** format is unambiguous: the *very
> next* user-register write in this TU, `0x13ea8: 20 e8 f3`, decodes cleanly as
> `wur.fcr a2` (same `f3` major, named register `0xe8`). So `20 15 f3` is
> `wur a2, UR#0x15`. Only **two** `wur` ops exist in the whole TU — this `UR#0x15`
> write and the FCR write — so `UR#0x15` *is* the error/notify status register the
> raise path publishes. **HIGH/OBSERVED** for the write; **INFERRED** that a host
> runtime reads `UR#0x15` (it is a custom TIE state register — the natural
> host-pollable error latch; the on-the-wire host read is out of this blob's
> scope).

### 5d. The two severity wrappers — the policy switch

```text
00013e00 <raise_FATAL sev=2>:
    13e00:  366100      entry  a1, 48
    13e03:  2931        s32i.n a2, a1, 12 ; l32i.n a11,[12]  ; a11 = record (-> a3)
    13e07:  a2a002      movi   a10, 2                        ; severity 2 (-> a2)
    13e0a:  e50000      call8  0x13e18                       ; raise_error(2, record)
    13e0d:  254df6      call8  0xa2e0                        ; Halt-dispatch (§6)
    13e10:  060000      j      0x13e14
    13e14:  06ffff      j      0x13e14                       ; *** INFINITE SELF-LOOP ***

00013e30 <raise_RECOVERABLE sev=1>:
    13e30:  366100      entry  a1, 48
    13e33:  2931        s32i.n a2, a1, 12 ; l32i.n a11,[12]  ; a11 = record
    13e37:  0c1a        movi.n a10, 1                        ; severity 1
    13e39:  e5fdff      call8  0x13e18                       ; raise_error(1, record)
    13e3c:  1df0        retw.n                               ; *** RETURNS (no halt) ***
```

`call8` rotates `a10→a2`, `a11→a3` into the callee window (windowed ABI), so the
wrapper's `a10 = severity` becomes `raise_error`'s `a2`, and `a11 = record`
becomes `a3` (**MED/INFERRED** arg path; the bodies are **HIGH/OBSERVED**).

**Severity 2 = HARD FAULT** (`raise → Halt → spin`); **severity 1 = soft**
(`raise → return`). Of the six fault classes, **only `HandleFPError` takes the
severity-1 path** — confirmed by caller census. **HIGH/OBSERVED.**

---

## 6. The fatal halt-spin (instruction-exact) and the report sink

### 6a. Halt-dispatch `@0xa2e0` and the halt CSR

```text
0000a2e0 <halt-dispatch>:
    a2e0:  364100      entry  a1, 32
    a2e3:  240800      const16 a2, 8
    a2e6:  244456      const16 a2, 0x5644        ; a2 = halt-context block @DRAM 0x85644 (BSS)
    a2e9:  ad02        mov.n  a10, a2
    a2eb:  1c0b        movi.n a11, 16
    a2ed:  a5a0f7      call8  0x1cf8             ; Setup Halt: save resume PC; LOG "S: Setup Halt"
    a2f0:  ad02        mov.n  a10, a2
    a2f2:  2575f9      call8  0x3a44             ; "S: Entering HALT" + write halt CSR
    a2f5:  1df0        retw.n
```

The halt CSR write, inside `0x3a44`:

```text
00003a44 <entering-halt>:
    ...
    3a4e:  a40800      const16 a10, 8
    3a51:  a4791e      const16 a10, 0x1e79       ; DRAM "S: Entering HALT"
    3a54:  e51215      call8  0x18b84            ; LOG
    ...
    3a61:  32a400      movi   a3, 0x400
    3a64:  340800      const16 a3, 8             ; a3 = 0x80400 (run-state / halt CSR)
    3a67:  2903        s32i.n a2, a3, 0          ; *** write resume-PC/halt CSR @ 0x80400 ***
    3a69:  1df0        retw.n
```

So the **complete fatal sequence** for any severity-2 fault is:
`build record → wur UR#0x15 → 0xa450 (report) → Setup Halt (0x1cf8, save PC, LOG
"S: Setup Halt") → Entering HALT (0x3a44, LOG, store to halt CSR @0x80400) → spin
forever at j 0x13e14`. The engine is dead until reset. **HIGH/OBSERVED** for the
LOG calls and the CSR store; **MED** that the memory-mapped store at VA `0x80400`
is exactly the run-state/halt CSR (the address and the store are observed; the CSR
identity is inferred from the surrounding "Entering HALT" log).

> **NOTE — fatal spin vs. clean halt.** The fatal spin above is **distinct** from
> a *commanded* halt. The `0xa33c` function logs `"S: Halt"` (DRAM `0x8224e`) and
> invalidates prefetch hints (`"S: PrefetchHelper : Invalidating all hints"`) on a
> clean/run-state-driven halt; it does not enter the `j 0x13e14` error spin. The
> sibling [SEQ Main FSM Loop](main-loop.md) owns the run-state machine that drives
> the clean halt. This page only owns the *error*-driven spin.

### 6b. Report sink `@0xa450`

Called by `raise_error`, `0xa450` is the common reporting egress. It logs one of
`"S: NOTIFY"` (`0x82286`) / `"S: sending interrupt"` (`0x82257`) / `"S: sending
notification"` (`0x8226d`) and performs the external-register/TIE write that
publishes the notification to the host. This region is heavily FLIX-scheduled: the
LOG calls and the *intent* to publish a host notification are **OBSERVED**; the
exact destination CSR write is partly FLIX-desynced (**MED**) and cross-referenced
to the per-TPB error-trap notification ring (see §7). It is the bridge from
`raise_error` to the host notify path.

### 6c. `assert_fail` `@0xa304` — captures `__FILE__`/line/expr

```text
0000a304 <assert_fail>:
    a304:  366100      entry  a1, 48
    a307:  2931        s32i.n a2, a1, 12        ; a2 = expr/msg string ptr
    a309:  3921        s32i.n a3, a1, 8         ; a3 = __FILE__ string ptr
    a30b:  4911        s32i.n a4, a1, 4         ; a4 = __LINE__
    a318:  a40800      const16 a10, 8
    a31b:  a41022      const16 a10, 0x2210      ; "S: Assertion failure! %s(%s:%u)"
    a31e:  65860e      call8  0x18b84           ; LOG the 'S:' copy
    a32a:  b40800      const16 a11, 8
    a32d:  b43122      const16 a11, 0x2231      ; the non-'S:' copy "Assertion failure! …"
    a330:  0c1a        movi.n a10, 1
    a332:  a5d209      call8  0x1405c           ; threshold-gated host logger
    a335:  0c0a        movi.n a10, 0            ; code 0
    a337:  a5ac09      call8  0x13e00           ; FATAL -> SPIN
```

Every failed assertion — including `get_block_id`'s default arm and
`HandleFPError`'s default arm — captures the **`__FILE__` / line / expression**
strings into the report and then hard-faults. The DEBUG build's assertion call
sites pass the recovered source-file strings directly: `"error_handler.cpp"`
(`0x83ab9`, line 40 from the FP default), `"error_notifications.cpp"` (`0x83a55`,
line 38 from `get_block_id`), and the full
`/opt/workspace/NeuronUcode/src/handlers/signal_handler.cpp:9 0` (`0x83b88`, used
by the signal-registration failure log). These baked `__FILE__` strings are the
binary evidence that names the three source files of this TU. **HIGH/OBSERVED.**

---

## 7. The host-visible error state + the error-string / code catalog

On a fault the firmware publishes error state on **three** host-observable channels:

1. **TIE user-register `UR#0x15` (21)** — the packed error record (block_id + code
   + opcode/aux) written by `raise_error` at `0x13e23`. **HIGH/OBSERVED** write;
   **MED/INFERRED** that a host runtime reads this specific UR.
2. **The per-TPB error-trap notification ring** — the report sink `0xa450`
   dispatches a notification (`"S: NOTIFY"` / `"sending interrupt"` / `"sending
   notification"` + the external-register write) that ultimately reaches the host
   (MSI-X via the notification chain). **MED** — the dispatch is OBSERVED; the
   exact ring CSR (`tpb_notific_sw_queue_num3_errors_nt`, the N3/error class) is
   cross-referenced, not re-derived in this TU.
3. **The host stdout/stderr SPSC ring** — the printf-logger `0x18b84` writes the
   human-readable `"S: ErrorHandler : …"` line, drained at end-of-execution.
   **HIGH** that `0x18b84` is the per-engine `'S:'` logger used by every handler;
   **MED** that the ring is the exact transport.

The full error-string / code catalog read directly from DRAM `.rodata`:

| DRAM VA | String | Raised by | Code |
| --- | --- | --- | --- |
| `0x83acb` | `"S: ErrorHandler : FP Error(%d)"` | `0x13eb0` | FP-mapped (RECOV) |
| `0x83aeb` | `"S: ErrorHandler : Int Div Zero Error"` | `0x13f34` | `2` (FATAL) |
| `0x83b11` | `"S: ErrorHandler : Bad Opcode(0x%x)"` | `0x13f58` | `0` (FATAL) |
| `0x83b35` | `"S: ErrorHandler : Illegal Instruction(0x%x)"` | `0x13f80` | (FATAL) |
| `0x82210` | `"S: Assertion failure! %s(%s:%u)"` | `0xa304` | `0` (FATAL) |
| `0x82231` | `"Assertion failure! %s(%s:%u)"` (non-`S:` copy) | `0xa304` | — |
| `0x8224e` | `"S: Halt"` | `0xa33c` (clean halt) | — |
| `0x80f47` | `"S: Setup Halt"` | `0x1cf8` (PC save) | — |
| `0x81e79` | `"S: Entering HALT"` | `0x3a44` (fatal halt) | — |
| `0x82286` | `"S: NOTIFY"` | `0xa450` (report) | — |
| `0x82257` | `"S: sending interrupt"` | `0xa450` (report) | — |
| `0x8226d` | `"S: sending notification"` | `0xa450` (report) | — |
| `0x80e51` | `"S: RandGetState : rand_algorithm(0x%x) not …POOL"` | → `0x13f80` | — |
| `0x80e99` | `"S: RandSetState : rand_algorithm(0x%x) not …POOL"` | → `0x13f80` | — |

Source-file strings: `0x83ab9` `"error_handler.cpp"`; `0x83a55`
`"error_notifications.cpp"`; `0x83b88`
`"/opt/workspace/NeuronUcode/src/handlers/signal_handler.cpp:9 0"`. Symbol/arg
strings: `0x83a48` `"get_block_id"`; `0x83ab0` `"fp_error"`. Jump/data tables:
`0x83a30` (6-entry block-id JT), `0x83a70` (16-entry FP-status→code JT), `0x83b70`
(6-word signal table `{6,2,4,8,0xb,0xf}`). **All HIGH/OBSERVED.**

**Error codes** (the code byte folded into the record): `BadOpcode = 0`,
`IntDivZero = 2`, `FPError = {1,2,4,8,16}` (FP-status-mapped), `signal_handler =
'A' (0x41)`, early emitter `0x1a80 = 'B' (0x42)`, `assert_fail = 0`.

**Boot install.** Two adjacent boot calls arm the model:
`0x245e: call8 0x13e98` (`enable_fp_exceptions`: `rur.fcr ; or fcr, 0x7c ;
wur.fcr` — sets the FP exception-enable bits so HW FP faults trap into
`HandleFPError`) and `0x2461: call8 0x13fa8` (`register_signal_handlers`). So at
init the engine arms FP-exception trapping **and** registers the catch-all signal
handler; thereafter any FP fault is recoverable-reported and any other trap signal
is fatal. **HIGH/OBSERVED.**

> **COUNT DISCIPLINE — do not conflate two populations.** The **178** on this page
> is the *dispatch-table entry count* (123 default → `0x3198`, 55 real). It is a
> different population from the `'S:'` format-string census: this POOL DEBUG image
> contains **187** `'S: '` substring occurrences in DRAM (the PERF build strips
> them to **0**). Grounded straight from the binary (`python3 -c
> "print(open('dram.bin','rb').read().count(b'S: '))"` → `187`; the dispatch table
> `0x3198` count → `123`). Never write "178 `'S:'` strings".

---

## 8. SOFT-warning vs RECOVERABLE vs HARD-fault — the policy table

| Error source | Entry | Severity | Behavior |
| --- | --- | --- | --- |
| Unknown opcode (dispatch default) | `0x13f58` | 2 FATAL | log + report + SPIN |
| Illegal/unsupported sub-op (RNG …) | `0x13f80` | 2 FATAL | log + report + SPIN |
| Integer divide-by-zero | `0x13f34` | 2 FATAL | log + report + SPIN |
| Failed assertion (DEBUG asserts) | `0xa304` | 2 FATAL | log + report + SPIN |
| Any registered POSIX signal / Xtensa HW exception | `0x14014` | 2 FATAL | log + report + SPIN |
| Early-init fatal emitter (code `'B'`) | `0x1a80` | 2 FATAL | log + report + SPIN |
| **FP arithmetic fault (FCR-trapped)** | **`0x13eb0`** | **1 RECOVER** | **log + report + RETURN** |
| Speculative PC-range OOB (`is_pc_in_bounds @0x68d0`) | *(not this TU)* | — SOFT | log WARNING + **skip the prefetch**; no ErrorHandler |
| `"S: ASSERT, unknown dim"` diag | *(log-only)* | — SOFT | log only; continues |

**Exactly one class — FP arithmetic — is recoverable.** Everything routed into the
severity-2 wrapper (`0x13e00`) is a permanent engine halt. The SOFT cases never
enter this TU at all. **HIGH/OBSERVED** for the fatal/recoverable split via the
`0x13e00`-vs-`0x13e30` callers.

> **The SOFT/HARD "out-of-bounds" split, pinned.** The speculative PC-range guard
> `is_pc_in_bounds @0x68d0` is a *soft* guard: its only two callers
> (`wait_for_cache_line` prefetch / `ok_to_evict`) treat OOB as skip-with-WARNING
> (`"S: WARNING: wait_for_cache_line's next_addr … is out-of-bounds … skipping
> it."` @ DRAM `0x8171c`), never a fault — and they have **zero** call edges into
> this ErrorHandler TU (verified: `0x13e00`/`0x13f58`/`0x13f80`/`0x13f34` have no
> callers in the prefetch/bounds region). Conversely the **dispatch-bound miss**
> (opcode index outside `[0,177]`) *is* hard — it routes `0x3198 → 0x13f58 →
> spin`. So "out of bounds" is policy-split: speculative PC range = soft skip;
> opcode-index/dispatch bound = hard Bad-Opcode fault. The soft side is owned by
> [SEQ PC-Bounds](pc-bounds.md). **HIGH/OBSERVED**, cross-checked against the
> dispatch hub and PC-bounds siblings.

---

## 9. Relation to the wider fault model (forward links)

- **Xtensa HW exception roster.** The hardware-exception classes (illegal
  instruction, instruction-fetch error, load/store, integer-divide-by-zero,
  coprocessor, window over/underflow, the KSL/ISL stack-limit violation,
  break/debug, interrupt/syscall) are the *fault set this ErrorHandler must field*.
  On the real device the corresponding HW traps are delivered to the firmware as
  one of the 6 registered signals (§4e) and hard-fault via `0x14014`. The
  firmware's coupling is the `signal()` table, **not** a per-cause handler array.
  The exception bodies and the `exccause`→signal binding live on the
  [interrupt handler-bodies](../../control/interrupt/handler-bodies.md) and
  [Q7 surprises binding](../../control/interrupt/q7-surprises-binding.md) pages
  (**forward links**). **MED/INFERRED** for the signal-number bridge.
- **Stack-limit (ISL) faults.** A kernel that stores below its HBM stack floor
  raises an ISL `StackLimitViolation` (HW) → a signal → `0x14014` → FATAL spin. So
  a kernel stack overflow is an *unrecoverable engine halt*: the library installs
  the limit, the HW enforces it, this ErrorHandler fields the fault. **MED.**
- **Security/boot fault chain.** The boot-time security/fault posture (and where a
  *boot* fault diverges from this *run-time* ErrorHandler) is on the
  [boot-fault overview](../../control/security/boot-fault-overview.md)
  (**forward link**).
- **Host error model.** A host runtime reading channels (1)+(2)+(3) above
  distinguishes "engine still alive, reported an FP fault" (sev 1) from "engine
  hung — bad opcode / illegal instr / div0 / trap / assert" (sev 2, the spin). The
  host-side binding is documented on the
  [runtime lifecycle error model](../../runtime/lifecycle-error-model.md)
  (**forward link**). The on-device producer side here is
  HIGH/OBSERVED; the host consumer side is out of this blob's scope and inferred.

---

## 10. Confidence ledger

**HIGH / OBSERVED** (direct disassembly or byte read):

- Carve reproduced; `iram.bin 8e4412b9…` / `dram.bin 7bdf6ed7…` match the sibling
  anchors exactly.
- The dispatch-default stub `0x3198` (`l8ui ; call8 0x13f58 ; j`), and the
  178-entry table census (123 default / 55 real / 56 distinct) re-counted from the
  table words.
- All six fault entry points re-disassembled instruction-exact and re-synced past
  their FLIX log bundles; each traced to its `'S:'` string, error code, and
  terminal raise wrapper.
- The recoverable-vs-fatal switch: `0x13e00` (sev 2, halt + spin at `j 0x13e14`)
  vs `0x13e30` (sev 1, `retw.n`); only `HandleFPError` uses `0x13e30`.
- `raise_error` writes the packed record to TIE `UR#0x15` (the `20 15 f3` /
  `f3`-major WUR proof, anchored by the cleanly-decoding `20 e8 f3 = wur.fcr` at
  `0x13ea8`).
- `get_block_id` reads engine-id global `@DRAM 0x85f38`, remaps via the 6-entry JT
  `@0x83a30`; FP-status 16-entry JT `@0x83a70` re-decoded `{1→16,2→8,4→4,8→2,16→1}`;
  signal table `{6,2,4,8,0xb,0xf} @0x83b70`; `register_signal_handlers` installs
  `0x14014` via libc-style `signal()` (`0x18c68`).
- Full error-string / code catalog + the three source-file strings
  (`error_handler.cpp`, `error_notifications.cpp`, `signal_handler.cpp`) read from
  `.rodata`.
- Fatal halt sequence: Setup Halt (`0x1cf8`) → Entering HALT (`0x3a44`, store to
  CSR `@0x80400`) → infinite `j 0x13e14`.
- Boot wiring `0x245e`/`0x2461`; `enable_fp_exceptions` `or fcr,0x7c ; wur.fcr`.

**MED / INFERRED:**

- `UR#0x15` / the notify ring / the stdout SPSC ring are the host-visible channels
  — the writes are OBSERVED; the host-side read binding is out of scope.
- The POSIX-signal-number ↔ Xtensa `exccause`/HW-vector mapping is the inferred
  bridge (grounded in the libc `signal()` shape + the matching fault classes).
- The per-byte layout of the packed record beyond `{block_id, code-nibble,
  opcode/aux}` is partly inferred.
- `0xa450`'s exact notify-CSR write is FLIX-desynced (LOG calls + intent OBSERVED).
- The halt CSR identity (`0x80400` in `0x3a44`) is inferred from the surrounding
  "Entering HALT" log.

**Divergences flagged for the per-Part reconcile:** (a) "ErrorHandler `@0x3198`"
vs "`@0x13f58`" — both correct (stub vs body, §2 NOTE), no CORRECTION; (b) the
`build_error_record` byte-layout pinned more precisely than the survey's one-line
summary (§5b CORRECTION); (c) the 178 dispatch count vs the 187 `'S:'` census kept
strictly separate (§7 callout).
