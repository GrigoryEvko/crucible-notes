# SEQ Run-State Machine

This page reconstructs the **run-state machine** of the NX **sequencer (SEQ) engine**:
the small software FSM — `RUNNING` / `PAUSED` / `HALTED` — that gates whether the
[main FSM loop](main-loop.md) fetches and dispatches at all, and the host↔device CSR
contract that drives it. The host arms a transition by poking one bit in the
`tpb_xt_local_reg` **`nx` bundle** (`start_ctrl`, `instr_halt_ctrl`); the firmware
publishes its state into `run_state` and selects work through `intr_info`. This is **the
SEQ engine's own firmware** — windowed-ABI Xtensa on the `ncore2gp` "Cairo" core — *not*
the microcode it interprets. This page **opens the SEQ-internals cluster**
([surprises / IRQ poll](surprises-irq.md), [uarch + on-device debugger](uarch-debugger.md),
[SoC window-manager](soc-window-manager.md)); it owns the *state* and the *transitions*,
and cross-links the loop, the dispatch hub, and the error path rather than re-deriving them.

Everything below is **byte-pinned to a shipped artifact**. The anchor image is
the carved `CAYMAN_NX_POOL_DEBUG` device firmware extracted from `libnrtucode.a` (member
`img_CAYMAN_NX_POOL_DEBUG_IRAM_contents.c.o` for code, `…_DRAM_contents.c.o` for strings),
disassembled with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, Cairo µarch,
Xtensa24, RI-2022.9, `TargetHWVersion=NX1.1.4`) that ships inside the gpsimd-tools package.
The CSR offsets, field names, bit positions, and reset values are read from the shipped
`tpb_xt_local_reg.json` register description. The DEBUG build **keeps** the `'S:'` format
strings; the PERF build strips them and re-lays-out code, so PERF offsets do *not* map 1:1
onto the addresses here. Where a sibling note or the prompt-level report disagrees with the
disassembly, **the binary wins**, and an in-place **CORRECTION** says so.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/string/JSON-field read from a shipped image; `INFERRED` =
reasoned over OBSERVED facts; `CARRIED` = consolidated from a cited cross-page anchor;
crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real),
**GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading),
**NOTE** (orientation). The page default is `[HIGH/OBSERVED]`; claims that depart from it
carry an explicit tag.

> **NOTE — addressing convention.** All code addresses are **IRAM-image offsets == device
> IRAM VA** (`ResetVectorOffset=0`, InstRAM base `0x0`). DRAM string offsets are the low-16
> value built by `const16` (full DRAM VA = `0x80000 | value`, file offset = value). CSR
> offsets are `tpb_xt_local_reg` `nx`-bundle low-16 offsets (bundle base `0x0`). `.text` /
> `.rodata` are VMA==file-offset.

---

## 0. The state machine in one diagram

The run-state FSM is a **three-state software FSM** layered over a handful of host-visible
CSRs. Only two of its states have a numeric `run_state` code (`1`=RUNNING, `2`=PAUSED);
HALTED is signalled out-of-band (cleared running-flag + ISA halt), not by a third code.

```
                  host writes nx.start_ctrl(0x04000004)=1  (doorbell, polled — never IRQ)
                                       │
  ┌─────────┐   reset/boot   ┌─────────▼──────────┐  poll @2cb7 sees !=0   ┌──────────────┐
  │  RESET  │───────────────►│   IDLE / waiting   │──────────────────────►│ ENTERING_RUN │
  │ (csr=0) │  outer loop    │  enter_run spins   │                        │ ack=0; flag=1│
  └─────────┘   @0x2499      │  on start_ctrl     │◄───────┐               │ run_state=1; │
                             └────────────────────┘        │               │ restore PC   │
                                  ▲      ▲                  │               └──────┬───────┘
            poll-surprises==0 /   │      │ host start_ctrl=1│  publish run_state=1 │
            stop-flag==0 (EOP)    │      │ (resume, @0x4bbe)│                      ▼
            retw @0x31a9 ─────────┘      │                  │              ┌──────────────┐
                                         │            ┌─────┴──────┐       │   RUNNING    │
                                         └────────────│   PAUSED   │       │ fetch/decode │
                                          run_state=2 │ flag=0     │◄──────│ /dispatch    │
                                          @1ce1/1cee  │ run_state=2│ op@2e89│ (178-way)    │
                                                      └────────────┘ RER   └──────┬───────┘
                                                       bit2 SET             bit2  │ CLEAR
                                                                          ┌───────▼────────┐
   bad opcode / OOB / div0 / signal  ──── FW-09 ───────────────────────► │     HALTED     │
   raise 0x13e00 (sev=2) → 0xa2e0 → Setup-Halt+Entering-HALT → spin 13e14 │ save PC lr3/4; │
                                                                          │ flag=0;        │
   op@2e89 RER bit2 CLEAR → Setup-Halt @0x1cf8 + ISA-halt 0xa390 ───────► │ run_state=     │
                                                                          │ <halt-marker>; │
                                                                          │ 0xa390 finalize│
                                                                          └────────────────┘
```

The rest of this page makes every box and arrow **byte-exact**.

---

## 1. The control CSRs — the `nx` bundle of `tpb_xt_local_reg`

The run-state machine is driven through six host-visible registers in the **`nx` bundle** of
`tpb_xt_local_reg`. These are read straight out of the shipped register description
(`tpb_xt_local_reg.json`), which declares the regfile as `AddrWidth 16`, `DataWidth 32`,
`SizeInBytes 0x10000` (64 KiB aperture), `InterfaceType APB`, and the `nx` bundle at
`AddressOffset 0x0`, `BundleSizeInBytes 0x1000`, `ArraySize 1`. Every field below — name,
offset, bit position, reset value, description — is verbatim from that JSON. `[HIGH/OBSERVED]`

| nx offset | JSON register | field | bit(s) | reset | role in the run-state FSM |
|---|---|---|---|---|---|
| `0x0`  | `release_run_stall` | `run_stall`  | `[0]`    | `0x1` | host writes 0 → release `RunStallOnReset` to the NX core (boot only) |
| `0x4`  | `start_ctrl`        | `ctrl`       | `[0]`    | `0x0` | host doorbell: *"start_addr is valid, exit Halt state and start executing"* |
| `0x8`  | `run_state`         | `state`      | `[31:0]` | `0x0` | fw→host status word — *"Undecoded run_state"* (SW-defined: 1=RUN, 2=PAUSE) |
| `0x14` | `instr_halt_ctrl`   | `halt_req`   | `[0]`    | `0x0` | *"Request to go to ISA HALT state"* — **never written by the SW FSM** (§6.3) |
| `0x18` | `intr_ctrl`         | `en`         | `[3:0]`  | `0x0` | NX interrupt enable (4 sources) |
| `0x1C` | `intr_info`         | `metadata`   | `[31:0]` | `0x0` | dispatcher work-selector / IRQ metadata (§6) |

> **GOTCHA — `run_state` is opaque to the hardware; the *encoding* is pure software.** The
> JSON field description is literally *"Undecoded run_state"* with width `[31:0]` and no
> enumerated values. The `{1=RUNNING, 2=PAUSED}` encoding exists **only in firmware** — it is
> recovered from the literal stores in §2, not from the register description. A reimplementer
> who reads only the CSR doc will not learn the encoding; it lives in the SEQ image.

### 1.1 How the firmware forms the absolute address

The `nx` bundle base is `0x0` *within the regfile*; the firmware addresses it absolutely
through the device MMIO window. On the CAYMAN family the absolute address is built with a
`movi`/`const16` pair that materialises the high half `0x0400` and the low half `OFF`:

```asm
    movi    a3, 0x400        ; HIGH = 0x0400  (the MMIO window high word)
    const16 a3, OFF          ; LOW  = OFF      ; a3 = (0x0400<<16) | OFF = 0x04000000 | OFF
```

So the CAYMAN-family device-side aperture is **`0x04000000 + nx-offset`**, observed directly
from the address-build bytes: `start_ctrl = 0x04000004`, `run_state = 0x04000008`,
`intr_info = 0x0400001C`.

| nx offset | CAYMAN device abs addr | byte-exact const16 site (IRAM) |
|---|---|---|
| `0x4`  | `0x04000004` | `2cad: movi a3,0x400 ; 2cb0: const16 a3,4`  (boot poll) |
| `0x4`  | `0x04000004` | `2d0d: movi a3,0x400 ; 2d10: const16 a3,4`  (boot ack=0) |
| `0x8`  | `0x04000008` | `2d1c: movi a5,0x400 ; 2d1f: const16 a5,8`  (publish=1) |
| `0x8`  | `0x04000008` | `1ce6: movi a2,0x400 ; 1ce9: const16 a2,8`  (publish=2) |
| `0x8`  | `0x04000008` | `3a61: movi a3,0x400 ; 3a64: const16 a3,8`  (halt-marker) |
| `0x1C` | `0x0400001C` | `4c5f: movi a2,0x400 ; 4c62: const16 a2,28` (dispatcher selector) |

> **NOTE — the address-map DB carries a *different* base.** The shipped
> `al_address_map_db.json` (maverick view) lists `…_SP_0_LOCAL_REG` at `base = 0x60000`
> (393216) with `size = 0x10000`, and the full 64-bit SoC physical view at
> `0x200030802027a000`. Those are the **host/SoC** addresses of the same 64 KiB aperture; the
> `0x04000000`-based addresses above are the **device-local** view the SEQ firmware actually
> dereferences. For the device-side read sites on this page, the const16-built `0x04000000`
> base is authoritative — it is what the instruction stream loads/stores.

### 1.2 The resume-PC register pair (general bundle)

The resume PC is saved/restored through two registers in the **`general` bundle** of the same
regfile (`AddressOffset 0x1000`, `ArraySize 60`, `BundleSizeInBytes 0x20`, one `lr.value`
`[31:0]` per slot). With stride `0x20`, `lr[3] = 0x1000 + 3·0x20 = 0x1060` and
`lr[4] = 0x1080`. The firmware addresses them as `0x04001060` / `0x04001080`.

| general reg | device abs addr | role |
|---|---|---|
| `lr[3].value` (`0x1060`) | `0x04001060` | resume-PC **LO** — Setup-Halt writes; `enter_run` reads back |
| `lr[4].value` (`0x1080`) | `0x04001080` | resume-PC **HI** — Setup-Halt writes; `enter_run` reads back |

> **GOTCHA — these are *not* SEQ-array CSRs.** `tpb_arr_seq` (the 4D PE-array matmul
> sequencer) carries its **own** run/halt/start registers in a different block; they are *not*
> the SEQ-engine run-state CSRs decoded here. The SEQ run-state machine is **`nx`-bundle
> only**. Do not cross-wire the two during reimplementation.

---

## 2. The state enum (published in `nx.run_state`, CSR `0x0008`)

`run_state` is `[31:0]` and the JSON calls it *undecoded*. Its **software** encoding,
recovered from the literal stores, is a three-state FSM of which only two states carry a
numeric code:

| value | meaning | written by | byte-exact store site |
|---|---|---|---|
| `0` | RESET / idle | CSR reset value (never-run) | — (reset `0x0`) |
| `1` | RUNNING | `enter_run`, on StartCtrl accept | `2d17: movi.n a4,1` … `2d22: s32i.n a4,a3,4` |
| `2` | PAUSED  | Enter-Pause core | `1cec: movi.n a3,2 ; 1cee: s32i.n a3,a2,0` |
| `<ptr>` | HALTED / fatal **marker** | Entering-HALT core | `3a67: s32i.n a2,a3,0` (a2 = `0x00085644`) |

OBSERVED byte sequences (CAYMAN):

```
run_state = 1 :  0c14 (movi.n a4,1) 4242.. (s8i flag) 52a4 0054 0800 (a5=0x04000008) 4913 (s32i.n a4,a3,4)
run_state = 2 :  0c23 (movi.n a3,2) 3902 (s32i.n a3,a2,0) c02000 (memw) 1df0 (retw.n)
run_state = halt-marker : 32a400 340800 (a3=0x04000008) 2903 (s32i.n a2,a3,0) 1df0
```

The store at `0x2d22` is a **displaced** `s32i.n a4,a3,4`: `a3` still holds
`0x04000004` (`start_ctrl`) from the ack, and `+4` lands on `0x04000008` (`run_state`); the
`movi a5,0x400 ; const16 a5,8` at `0x2d1c` independently materialises the same `run_state`
address into `a5` but is not the store base — a small dead-code artifact of the codegen.

> **QUIRK — there is no numeric `HALTED` or `ERROR` code.** `RUNNING(1)` and `PAUSED(2)` are
> the only stable status codes the host reads. The fatal halt writes the **running-flag struct
> pointer** `0x00085644` into `run_state` as an *"I died here"* marker (§4.3); the
> *authoritative* halted/error signal is (a) the cleared SW running-flag at
> `state[0x855e0+100]`, (b) the FW-09 error record + trap ring
> ([error-handler.md](error-handler.md)), and (c) the ISA-level halt finalize `0xa390`. The
> host's interpretation of the marker as a debugger sentinel is `[MED/INFERRED]`; the store
> itself is `[HIGH/OBSERVED]`.

---

## 3. The StartCtrl entry handshake (boot **and** resume are the same algorithm)

The host arms a RUN by writing `start_ctrl(0x04000004) = 1`. The firmware **polls** this CSR
(it is never delivered as an interrupt — see §6); on a non-zero read it acks the doorbell by
writing it back to `0`, then proceeds. Boot and resume share this two-step doorbell; only boot
additionally publishes `run_state=1`, sets the running-flag, and restores the resume PC.

### 3a. Boot entry — `enter_run @0x2c64`

`enter_run` first logs `"S: Pending StartCtrl"` (DRAM `0x12ec`), then spins on `start_ctrl`,
then logs `"S: enter_run: start"` (DRAM `0x1302`) and publishes the RUNNING state:

```c
// enter_run @0x2c64 (CAYMAN POOL DEBUG, IRAM offsets == device VA)
void enter_run(running_flag_t *flag /*a2 = &state[0x855e0+100] = 0x00085644*/) {
    // 2c99: LOG "S: Pending StartCtrl"  (const16 a10,0x12ec ; call8 0x18b84)
    // ---- StartCtrl POLL (the spin) ----
    do {
        a3 = *(u32*)0x04000004;          // 2cad: movi a3,0x400 ; 2cb0: const16 a3,4 ; 2cb3: l32i.n a3,[a3]
        stash[a1+20] = a3;               // 2cb5: s32i.n a3,a1,20
    } while (a3 == 0);                   // 2cb7: beqz a3,0x2caa   *** SPIN until start_ctrl != 0 ***
    // 2cc0: LOG "S: enter_run: start"   (const16 a10,0x1302 ; call8 0x18b84)
    a2_dve = read_hw_decode_cfg();       // 2cd0: call8 0x5504 ; 2cd5: call8 0x5514  (breakpoint/step CSRs, FW-11)
    state[+96] = *(u32*)0x040014c0;      // 2cd8..2ce6: DVE perf-mode general lr[38]

    // ---- the doorbell ACK + RUNNING publish ----
    *(u32*)0x04000004 = 0;               // 2d13: movi.n a4,0 ; 2d15: s32i.n a4,a3,0   *** ACK start_ctrl = 0 ***
    *(u8 *)flag       = 1;               // 2d17: movi.n a4,1 ; 2d19: s8i a4,a2,0      set SW running-flag
    *(u32*)0x04000008 = 1;               // 2d1f: const16 a5,8 ; 2d22: s32i.n a4,a3,4  *** PUBLISH run_state = 1 (RUNNING) ***
    dispatch_ctx_init();                 // 2d2d: call8 0x3cdc

    // ---- restore the resume PC from lr[3]/lr[4] ----
    resume_lo = *(u32*)0x04001060;       // 2d30: movi a4,0x400 ; 2d33: const16 a4,0x1060 ; 2d36: l32i.n a5,[a4]
    resume_hi = *(u32*)0x04001080;       // 2d40: l32i.n a4,[a4+0x20]   (== 0x04001080, lr[4])
    // 2d4e: LOG "S: enter_run: done"  (const16 a10,0x1336)
    // -> falls through into the inner Sunda / HW-decode FSM (main-loop.md)
}
```

`[HIGH/OBSERVED]` on every store/load; the LOG helper is `call8 0x18b84` and the assert/fatal
helper used elsewhere is `call8 0xa304`.

### 3b. Resume entry — dispatcher resume path `@0x4bbe`

Resume from PAUSED runs the **identical** two-step doorbell, then re-dispatches by execution
mode (Sunda software-fetch vs HW-decode), read from the central state byte `state[0x855e0+108]`:

```c
// resume @0x4bbe
for (;;) {                               // *** SPIN-poll start_ctrl ***
    a2 = *(u32*)0x04000004;              // 4bbe: movi a2,0x400 ; 4bc1: const16 a2,4 ; 4bc4: l32i.n a2,[a2]
    if (a2 != 0) break;                  // 4bc6: bnez.n a2,0x4bce ; 4bcb: j 0x4bbe
}
*(u32*)0x04000004 = 0;                   // 4bce: const16 a2,4 ; 4bd4: movi.n a3,0 ; 4bd6: s32i.n a3,[a2]   ACK = 0
u8 mode = state[0x855e0+108];            // 4bde: l8ui a2,[a2+108]   resume by mode (Sunda vs HW-decode)
// ... re-enter the fetch/dispatch loop by mode ...
```

> **NOTE — the host StartCtrl *protocol*.** host writes the start address into the resume-PC
> LRs (`lr[3]`/`lr[4]`, §1.2), or relies on the prior value, then writes
> `start_ctrl(0x04000004)=1`. The fw spin-polls until non-zero, acks to `0`, and (on boot)
> publishes `run_state=1` and reads the PC back. The host-**seed** of `lr[3]`/`lr[4]` to set
> the initial PC is `[MED/INFERRED]` — `enter_run` only *reads* the pair; the write side does
> not appear in these firmware images. The poll/ack handshake itself is `[HIGH/OBSERVED]`.

---

## 4. The Pause / Halt decision (sync/wait handler `@0x2e89`)

Pause and Halt are **not opcodes**; both are reached inside an inline sync/wait handler. The
handler is reached through the [178-entry dispatch table](dispatch-hub.md) (DRAM `0x80814`),
and the branch between Pause and Halt is decided by a **`rer`** (read-external-register) of a
remote status word and a test of **bit 2**.

> **CORRECTION — the sync handler is dispatch index `0x60`, not `0xa1`.** The backing report
> stated *"opcode-0xa1 … dispatch table entry `0xa1` → `0x2e89`"*. Re-reading the table from
> the DRAM image disproves the index: `dispatch[0xa1] = 0x3198` (the default ErrorHandler);
> the only index whose slot holds `0x2e89` is **`0x60`**. The handler *body* (`0x2e89`,
> `call8 0x1ca4`, the `rer`, the two call targets) is exactly as reported — only the table
> index was wrong. The `0xa1` figure appears to be a transcription collision with the ISA-halt
> finalize address `0xa390`. **Use index `0x60`.**

```c
// sync/wait handler @0x2e89  (dispatch table index 0x60)
sync_primary(work);                              // 2e89: call8 0x1ca4
if (idx > 38) { bit = 0; }                       // 2e94: movi.n a3,38 ; bltu a3,a2 -> idx==0 path
else {
    ext_addr = (145u << 13) + idx*4;             // 2e9e: movi a3,145 ; 2ea1: slli a3,a3,13 (=0x122000)
                                                 // 2ea4: addx4 a2,a2,a3   -> 0x122000 + idx*4
    u32 status = read_external_reg(ext_addr);    // 2eab: rer a2, a2        *** RER remote status ***
    bit = status & 4;                            // 2eae: movi.n a3,4 ; 2eb0: and a2,a2,a3   isolate bit 2
}
if (bit != 0) {                                  // 2ec1: beqz.n a2,0x2ece
    enter_pause(&flag);                          // 2ec6: call8 0x1cc0   --- bit2 SET  -> PAUSE
    goto loop_back;                              // 2ec9: j 0x2ee2 -> j 0x31a3
} else {                                         //                     --- bit2 CLEAR -> HALT
    setup_halt(&state[0x855e0+100], /*reason=*/0); // 2ed9: call8 0x1cf8
    isa_halt_finalize();                         // 2edc: call8 0xa390
}
```

> **QUIRK — the bit-isolate is a register-form AND, not an immediate.** `2eae: movi.n a3,4 ;
> 2eb0: and a2,a2,a3` computes `a2 &= 4` (bit 2) using a materialised constant in `a3` — there
> is no immediate `andi`. The external base is built as `145<<13 = 0x122000` then combined per
> sync-index with `addx4`. The `rer`/`and`/`beqz` bytes are `[HIGH/OBSERVED]`; the *semantic*
> label *"remote bit2 ⇒ pause-vs-halt"* is `[MED/INFERRED]` from the two call targets.

### 4a. Enter-Pause `@0x1cc0` — `run_state = 2`

```c
// Enter Pause @0x1cc0  — logs "S: Enter Pause" (DRAM 0xf37)
void enter_pause(running_flag_t *flag /*a2 = &state[0x855e0+100] = 0x00085644*/) {
    // 1cd3: LOG "S: Enter Pause"  (const16 a10,0xf37 ; call8 0x18b84)
    *(u8 *)flag       = 0;        // 1ce1: movi.n a3,0 ; 1ce3: s8i a3,a2,0   *** CLEAR SW running-flag ***
    *(u32*)0x04000008 = 2;        // 1ce9: const16 a2,8 ; 1cec: movi.n a3,2 ; 1cee: s32i.n a3,a2,0  *** run_state = 2 (PAUSED) ***
    __memw();                     // 1cf0: memw     ordering barrier
    return;                       // 1cf3: retw.n
}
```

Pause = `{clear running-flag; run_state=2; memw; return}`. The engine then idles in the
dispatcher and resumes via the §3b `start_ctrl` handshake. `[HIGH/OBSERVED]`

### 4b. Setup-Halt `@0x1cf8` — save resume PC + clear flag

```c
// Setup Halt @0x1cf8  — logs "S: Setup Halt" (DRAM 0xf47)
void setup_halt(running_flag_t *flag /*a2*/, u32 halt_reason /*a3, saved at a1+16*/) {
    // 1d04: LOG "S: Setup Halt"  (const16 a10,0xf47 ; call8 0x18b84)
    teardown_flush();                       // 1d18: call8 0x13c70
    u32 lo = compute_resume_pc_lo(&state[0x855e0+24]); // 1d24: call8 0x3ac4
    u32 hi = compute_resume_pc_hi();        // 1d2a: call8 0x3ae8
    *(u32*)0x04001060 = lo;                 // 1d34: movi a4,0x400 ; const16 a4,0x1060 ; 1d3a: s32i.n a3,a4,0   *** SAVE resume-PC LO -> lr[3] ***
    *(u32*)0x04001080 = hi;                 // 1d41: const16 a5,0x1080 ; 1d44: s32i.n a3,a4,32  (a4+0x20 == 0x04001080)  *** SAVE resume-PC HI -> lr[4] ***
    *(u8 *)flag       = 0;                  // 1d46: movi.n a3,0 ; 1d48: s8i a3,a2,0   *** CLEAR running-flag ***
    *(u32*)((u8*)flag + 4) = halt_reason;   // 1d4b: l32i.n a3,a1,16 ; 1d4d: s32i.n a3,a2,4   store halt_reason -> flag+4
    return;                                 // 1d4f: retw.n   (a2 still = &flag = 0x00085644 on return)
}
```

> **GOTCHA — the HI store reuses `a4`, not `a5`.** The HI save is `s32i.n a3,a4,32` with base
> `a4`(=`0x04001060`) + `0x20` = `0x04001080`; the `movi a5,0x400 ; const16 a5,0x1080` pair
> *materialises* the HI address into `a5` but `a5` is **not** the store base — a dead pair,
> like the `a5` in §2. Both resolve to `0x04001080`; do not be misled into thinking two
> different addresses are touched.

### 4c. Entering-HALT `@0x3a44` + fatal dispatch `@0xa2e0` + finalize `0xa390`

```c
// Entering HALT @0x3a44 — logs "S: Entering HALT" (DRAM 0x1e79)
void entering_halt(void *marker /*a2 = 0x00085644*/) {
    // 3a4e: LOG "S: Entering HALT"  (const16 a10,0x1e79 ; call8 0x18b84)
    *(u32*)0x04000008 = (u32)marker;   // 3a61: movi a3,0x400 ; 3a64: const16 a3,8 ; 3a67: s32i.n a2,a3,0
                                       //       *** WRITE run_state(0x04000008) = a2 (= 0x00085644, halt-marker) ***
    return;                            // 3a69: retw.n
}

// fatal-halt dispatch @0xa2e0
void fatal_halt(void) {
    void *flag = (void*)0x00085644;    // a2e3: const16 a2,8 ; const16 a2,0x5644
    setup_halt(flag, /*reason=*/16);   // a2e9: mov a10,a2 ; movi.n a11,16 ; call8 0x1cf8
    entering_halt(flag);               // a2f0: mov a10,a2 ; call8 0x3a44
    return;                            // a2f5: retw.n
}
```

> **CORRECTION (folds FW-09's "inferred halt CSR ~0x8_0400").** FW-09 left the halt-CSR
> identity inferred. The Entering-HALT store target is byte-exactly **`run_state`** (`nx 0x0008`
> == abs `0x04000008`), **not** `instr_halt_ctrl` (`0x0014`). The value stored is the
> halt-context **pointer** `0x00085644`, used as a fatal sentinel. `instr_halt_ctrl` (`0x0014`)
> is **never** written by the recovered software FSM; the ISA-level halt is delivered by
> `0xa390`. `[HIGH/OBSERVED]` on the `run_state` store + address; `[LOW]` on any direct
> `0x0014` write (see §8).

---

## 5. Resume-PC save / restore symmetry

This is the closing piece: Setup-Halt (§4b) **writes** the resume PC into `lr[3]`(lo) /
`lr[4]`(hi), and `enter_run` (§3a) on the next StartCtrl **reads the same pair back**. The
write/read addresses match byte-for-byte:

```
  1d3a: s32i.n a3,a4,0    @0x04001060   <-- Setup-Halt writes LO
  1d44: s32i.n a3,a4,32   @0x04001080   <-- Setup-Halt writes HI
  2d36: l32i.n a5,a4,0    @0x04001060   --> enter_run reads LO
  2d40: l32i.n a4,a4,32   @0x04001080   --> enter_run reads HI
```

So `lr[3]`/`lr[4]` (general bundle `0x1060`/`0x1080`) **are** the resume-PC register pair: a
64-bit PC saved on Halt and reloaded on the next StartCtrl. The host can also seed the pair to
set the initial PC before the first doorbell (`[MED/INFERRED]` — read-only use on the device
side). `[HIGH/OBSERVED]` on the read/write pairing.

---

## 6. Run-state ↔ main-loop / dispatcher integration

Run-state gates whether the [main loop](main-loop.md) runs at all, and the top dispatcher
re-reads `run_state` to detect a Pause and route to the resume handshake.

### 6.1 The top dispatcher `@0x4c5c` — `intr_info` selects, `run_state` confirms

```c
// top dispatcher @0x4c5c
u32 sel = *(u32*)0x0400001C;          // 4c5f: movi a2,0x400 ; const16 a2,28 ; 4c65: l32i.n a2,[a2]   read intr_info
if (sel == 0) return;                 // 4c6b: beqz.n a2,0x4c76 -> 0x4cbe retw   (idle — nothing to do)
if (sel == 1) {                       // 4c70: beqi a2,1,0x4c79   -> RUN
    enter_run(&flag);                 // 4c79: call8 0x2c64   (StartCtrl handshake + Sunda FSM, §3a)
    hw_decode_fsm();                  // 4c7c: call8 0x31ac   ("Seq Loop, iter")
    u32 rs = *(u32*)0x04000008;       // 4c85: const16 a2,8 ; 4c88: l32i.n a2,[a2]   read run_state
    if (rs == 2) goto paused_resume;  // 4c8a: beqi a2,2,0x4ca1   (run_state==2 PAUSED -> resume/idle)
    // else fall through to the assert path
} else {                              // 4c73: j 0x4ca9   sel >= 2 -> assert
    ASSERT_FAIL("handle_interrupt_"); // 4c90: const16 a10,0x1203 ("handle_interrupt_") ; a11=0x11db ("interrupt_handler.hpp") ; call8 0xa304
}
```

`intr_info(0x0400001C)` is the **work selector** (`0`=idle / `1`=run / `>=2`=fault); after a
RUN, the dispatcher reads `run_state(0x04000008)` back and, on `2` (PAUSED), routes to the
resume branch. The assert string `"handle_interrupt_"` (DRAM `0x1203`) and source-name
`"interrupt_handler.hpp"` (DRAM `0x11db`) are recovered `__FILE__`/predicate literals that
ground the fault path. `[HIGH/OBSERVED]`

### 6.2 No async vector into the FSM — everything is polled

The async event word (the *"surprises"* poll, [surprises-irq.md](surprises-irq.md)) is
**polled**, latched into `intr_info`, and gated by `intr_ctrl(0x0018)`. There is **no async
interrupt vector that re-enters the run-state FSM**: `start_ctrl` is spin-polled (§3),
`intr_info` is read at the top of the dispatcher (§6.1), and the *"surprises"* word is read on
the loop body. A reimplementer must wire these as **polled CSRs**, not as a hardware ISR into
the run-state path. `[HIGH/OBSERVED]` on the polled reads; `[CARRIED]` from
[surprises-irq.md](surprises-irq.md) on the latch detail.

### 6.3 `instr_halt_ctrl` is *requested* by the host, observed nowhere in the SW FSM

`instr_halt_ctrl.halt_req[0]` is the *host-driven* "go to ISA HALT" request. The recovered
software FSM never **writes** it; the firmware-side halt is performed by `setup_halt` +
`entering_halt` + `0xa390`. Whether the host's write to `0x0014` is *read* by a path that
FLIX-desyncs around `0xa390` is not instruction-exact here (§8). `[HIGH/OBSERVED]` on the
absence of a SW write to `0x0014`.

---

## 7. The error path + end-of-program (FW-09 integration)

| trigger | path | terminal state |
|---|---|---|
| bad/illegal opcode, OOB, int-div-0, POSIX signal | FW-09 FATAL raise `0x13e00` (sev=`2` via `movi a10,2`) → `call8 0xa2e0` (§4c) → `setup_halt(reason=16)` saves PC + clears flag → `entering_halt` writes `run_state=marker` → **spin `j 0x13e14` (never returns)** | HALTED (fatal) |
| unknown opcode (table default) | dispatch default `0x3198` (ErrorHandler) → `HandleBadOpcode 0x13f58` → FW-09 raise above | HALTED (fatal) |
| `op@2e89` RER bit2 CLEAR | `setup_halt` (§4b) + `0xa390` finalize | HALTED |
| end-of-program: `poll-surprises(0x6af4)==0` or stop-flag `[a1+40]==0` | inner FSM `retw.n @0x31a9` → outer boot loop `@0x2499` → re-enter `enter_run` → **spin-poll `start_ctrl` again** | IDLE / waiting |

```c
// FW-09 fatal raise @0x13e00 (sev = 2)
void raise_fatal(err_t *e) {            // 13e00: entry a1,48
    log_error(/*sev=*/2, e);            // 13e07: movi a10,2 ; 13e0a: call8 0x13e18
    fatal_halt();                       // 13e0d: call8 0xa2e0   (§4c)
    for (;;) {}                         // 13e10: j 0x13e14 ; 13e14: j 0x13e14   *** never returns ***
}
```

> **NOTE — "program done" is a graceful return, not a halt.** The inner Sunda/HW-decode FSM
> exits the loop on `poll-surprises==0` *or* the stop-flag clearing, returns `retw.n` at
> `0x31a9` to the outer boot loop `@0x2499`, which re-enters `enter_run` and spin-polls
> `start_ctrl` again. HALT is reserved for the fatal/error path and the explicit
> `op@2e89` bit2-clear case.

> **NOTE — PC-bounds is a *soft* guard.** `is_pc_in_bounds @0x68d0`
> ([pc-bounds.md](pc-bounds.md)) is a speculative prefetch guard (skip the prefetch on
> out-of-bounds); it is **not** a hard halt trigger. An out-of-bounds **demand** fetch is what
> raises the error that lands in the FW-09 path above. `[HIGH/CARRIED]` from
> [pc-bounds.md](pc-bounds.md).

---

## 8. Honest gaps / desync

- **`0xa390` (ISA-halt finalize) partially FLIX/IVP-desyncs.** Whether it (or a downstream
  path) ever writes `instr_halt_ctrl(0x0014)` is not instruction-exact. The software FSM does
  **not** write `0x0014`; the `run_state=marker` store **is** exact. `[LOW]` on a direct
  `0x0014` write; `[HIGH/OBSERVED]` on the `run_state` stores.
- **`op@2e89` RER `0x122000` bit2 "pause vs halt".** The bytes (`rer`; `and ,4`; `beqz`) are
  OBSERVED; the *semantic* label is INFERRED from the two call targets. `[MED/INFERRED]`.
- **The halt-marker value `0x00085644`** written by Entering-HALT is OBSERVED; the host's
  interpretation of it as a debugger sentinel is INFERRED. `[MED/INFERRED]`.
- **Resume-PC host-seed direction** (host writes `lr[3]`/`lr[4]` to set initial PC) is
  INFERRED from `enter_run`'s read-only use of the pair; the write side does not appear in
  these firmware images. `[MED/INFERRED]`.

---

## 9. Consolidated state-transition table

| FROM | EVENT / TRIGGER (source) | ACTION (byte-exact site) | → TO |
|---|---|---|---|
| RESET | boot chain ([boot.md](boot.md)) | outer loop `@0x2499` | IDLE/wait |
| IDLE/wait | host writes `start_ctrl(0x04000004)=1` (CSR) | poll `@2cb7` sees `!=0` | ENTERING_RUN |
| ENTERING_RUN | (fw) | ack `start_ctrl=0 @2d15`; running-flag=1 `@2d19`; `run_state=1 @2d22`; restore PC `lr[3]/lr[4] @2d36/2d40` | RUNNING |
| RUNNING | fetch+decode+dispatch ([main-loop.md](main-loop.md)) | 178-way jump `@0x80814` | RUNNING |
| RUNNING | `op@2e89` sync; `RER[0x122000+idx]` bit2==1 | Enter-Pause: flag=0; `run_state=2 @1ce1/1cee`; memw | PAUSED |
| RUNNING | `op@2e89` sync; `RER` bit2==0 | Setup-Halt: save PC `lr[3]/lr[4]`; flag=0; `run_state=marker`; `0xa390` | HALTED |
| RUNNING | bad/illegal opcode, OOB, div0, signal (FW-09) | raise `0x13e00` → `0xa2e0` → Setup-Halt+Entering-HALT → spin `@0x13e14` | HALTED (fatal) |
| RUNNING | `poll-surprises(0x6af4)==0` / stop-flag==0 (EOP) | `retw.n @0x31a9` → outer boot loop | IDLE/wait |
| PAUSED | host writes `start_ctrl(0x04000004)=1` (CSR) | poll `@4bbe` sees `!=0`; ack=0 `@4bd6`; resume by mode `@4bde` | RUNNING |
| HALTED | (terminal) | n/a (engine spins/idles) | host reset / fresh re-StartCtrl |
| any | dispatcher: `intr_info(0x0400001C)==0` | `retw` `@0x4cbe` | IDLE |
| any | dispatcher: `intr_info==1` | `call enter_run @0x4c79`; `call HW-decode FSM @0x4c7c` | RUNNING |
| any | dispatcher: `intr_info>=2` | ASSERT `"handle_interrupt_"` `@0x4c90` | (fault) |

**Trigger-source summary:**

| transition | trigger kind |
|---|---|
| StartCtrl / resume | **HOST CSR write** to `start_ctrl` (polled by fw, never IRQ) |
| Pause / Halt | inline sync pseudo-op (`op@2e89`, dispatch index `0x60`) + a remote-status `rer` bit2 |
| Error → Halt | in-band FAULT (bad opcode / OOB / div0) or POSIX SIGNAL |
| End-of-program | poll-surprises empties (graceful loop exit) |
| Run-selection | `intr_info` CSR (latched from the polled *"surprises"* word) |

---

## 10. Per-generation invariance

Method: carve the `NX_POOL_DEBUG` IRAM `.rodata` from `libnrtucode.a` for each generation
(`objcopy -O binary --only-section=.rodata`) and byte-search the run-state-machine
instruction signatures. Image sizes: **SUNDA 59,600 / CAYMAN 116,768 /
MARIANA 114,816 / MARIANA_PLUS 119,616 bytes**. `[HIGH/OBSERVED]`

**CAYMAN family (CAYMAN / MARIANA / MARIANA_PLUS) — identical algorithm & encoding, only
relocated.** The run-state signatures are byte-identical across the three (offsets differ,
bytes match):

| signature | CAYMAN | MARIANA | MARIANA_PLUS |
|---|---|---|---|
| `run_state=2` publish (Enter-Pause core, sig `22a4002408000c233902`) | `0x1ce6` | `0x1b8e` | `0x1b8e` |
| `run_state=1` publish (sig `52a4005408004913`, the `const16 a5,8` start) | `0x2d1c` | `0x2bcc` | `0x2bfc` |
| Entering-HALT `run_state` write (`3a44` tail) | `0x3a61` | `0x396d` | `0x3a09` |
| Setup-Halt resume-PC lo (`const16 0x1060`) | `0x1d34` | `0x1bdc` | `0x1bdc` |
| `start_ctrl` boot poll | `0x2cad` | `0x2b5d` | `0x2b8d` |
| resume `start_ctrl` ack=0 | `0x4bce` | `0x4d4e` | `0x4dea` |
| `intr_info` read (`const16 a2,28`) | `0x4c5f` | `0x4ddf` | `0x4e7b` |
| `run_state==2` dispatcher test | `0x4c82` | `0x4e02` | `0x4e9e` |

⇒ CSR aperture, register offsets, enum values (`1`/`2`), handshake, Pause/Halt logic, and
dispatcher are **100% invariant** across the three Cayman-class gens.

> **NOTE — offset anchors.** The `run_state=1` row anchors on the start of the
> `52a4005408004913` byte signature (the `const16 a5,8` materialise + the `s32i.n a4,a3,4`
> store), so its CAYMAN offset is `0x2d1c`; the `movi.n a4,1` byte that sets the value sits a
> few bytes earlier at `0x2d17` (§3a). Both bytes belong to the same RUNNING-publish group;
> the table uses the signature start so the cited bytes and the offset agree.

**SUNDA (the older gen) — same algorithm, *different* CSR aperture.** SUNDA does **not** use
the CAYMAN `movi 0x400; const16 ,OFF` (`0x04000000|OFF`) idiom; it builds run-state CSR
addresses from a different window: `movi.n a2,16; const16 a2,OFF` ⇒ `(16<<16)|OFF =
0x00100000|OFF`. The per-register offsets also differ:

| register | SUNDA abs | CAYMAN-family abs |
|---|---|---|
| `start_ctrl` | `0x00100010` | `0x04000004` |
| `run_state`  | `0x00100014` | `0x04000008` |

SUNDA Enter-Pause `@0x35c4` and resume handshake `@0x354d` are **structurally identical**:
`{clear-running-flag; run_state=2; memw}` and `{poll start_ctrl; ack=0}` — only the MMIO
window (`0x00100000` vs `0x04000000`) and the per-register offsets (`start_ctrl 0x10`/`0x04`,
`run_state 0x14`/`0x08`) changed. The enum value `run_state=2=PAUSED` is invariant.

> **QUIRK — SUNDA's run-state CSR window is a genuine architectural aperture difference.**
> `0x00100000` (SUNDA) vs `0x04000000` (CAYMAN family) is observed directly from the two
> images' address-build bytes — not assumed. SUNDA's `intr_info`-style `movi 16; const16 ,0x1c`
> RUN selector was not found, so SUNDA's dispatcher likely lacks the
> `intr_info==1` selector or encodes it elsewhere (`[MED/INFERRED]` — the SUNDA dispatcher was
> not fully traced here).

---

## 11. Scope boundary — the rest of the SEQ-internals cluster

This page owns the **run-state FSM**: the enum, the control CSRs, the StartCtrl handshake, the
Pause/Halt transitions, resume-PC save/restore, and the dispatcher/error integration. The
sibling SEQ-internals pages cover the machinery this page only touches at transition points:

- [SEQ Surprises / IRQ Poll](surprises-irq.md) — the async-event
  (*"surprises"*) poll/handler (`0x6af4` poll, `sunda_check_surprises 0x6b0c` check, the bitmask
  handler `0x6ce0`) and the `intr_info`/`intr_ctrl` latching detail.
- [SEQ uarch + on-device debugger](uarch-debugger.md) — the HW-decode
  breakpoint/step CSRs read at `enter_run` via `0x5504`/`0x5514`.
- [SEQ SoC Window-Manager](soc-window-manager.md) — the 40
  address-translation windows (`window` bundle `0x2000`, `ArraySize 40`) that map the
  IRAM/DRAM/peripheral apertures the SEQ engine fetches and stores through.

---

## See also

- [SEQ Boot / Entry Path](boot.md) — reset → boot loop → the `start_ctrl` rendezvous (the
  boot-side of §3a, with the `0x2c63`/`0x2c64` mis-decode GOTCHA).
- [SEQ Main FSM Loop](main-loop.md) — the fetch → decode → dispatch loop that run-state gates;
  the outer boot loop `@0x2499` and the inner Sunda/HW-decode FSMs.
- [SEQ Decode / Dispatch Hub](dispatch-hub.md) — the 178-entry table at DRAM
  `0x80814` (55 real / 123 default → `0x3198`), and the dispatch index `0x60` that reaches the
  sync handler in §4.
- [SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md) — the `poll_surprises`/PC-redirect
  front-end on the RUNNING path.
- [SEQ Error-Handler / Fault Reporting](error-handler.md) — the FW-09 FATAL raise
  `0x13e00`, `HandleBadOpcode 0x13f58`, and the error record / trap ring referenced in §7.
- [SEQ PC-Bounds Guard](pc-bounds.md) — `is_pc_in_bounds @0x68d0`, the soft prefetch guard
  noted in §7.
- [`tpb_xt_local_reg` CSR (nx / general / window bundles)](../../control/csr/tpb-xt-local-reg.md)
  *(canonical home for the full register-bundle map; offsets/fields
  here are read directly from `tpb_xt_local_reg.json`)*.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the `OBSERVED` /
  `INFERRED` / `CARRIED` × `HIGH` / `MED` / `LOW` tags used throughout.
