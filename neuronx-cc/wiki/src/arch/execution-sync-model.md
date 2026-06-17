# Execution & Sync Model — Semaphores & Barriers

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; see [versions](../reference/versions.md)). The CoreV2 wire encoders, the all-engine-barrier expander, and the in-tree verifiers live in `libwalrus.so` (build-id `92b4d331`, `.text` VA == file offset); the `EngineType`/`WaitMode`/`UpdateMode`/`ClearMode` enums and `isDataPathEngine` live in `libBIR.so` (`a9b1ea38`); the reference sync kernels and the 256-counter bank model live in `libBIRSimulator.so`. The runtime instruction-stream builders (`add_semaphore_*`, `add_sync_barrier`) live in `libnrt.so`, which ships in the **separate** `aws-neuronx-runtime-lib` package, not in this compiler wheel; runtime claims are restated from disassembly of that binary and tagged accordingly. Other wheels differ; treat every address as version-pinned.*

## Abstract

The TPB has no cache-coherent shared memory and no global clock the way a CPU does. Five independent datapath engines — PE, Activation, Pool, DVE, and SP — each run their own instruction stream out of their own sequencer, asynchronously, and the only thing that makes one engine's write visible to another engine's read *in the right order* is an explicit **semaphore** rendezvous the compiler inserts. This page is the hardware sync model the backend's sync-insertion passes and the legality verifiers reason about: the **256-entry signed-counter semaphore bank** per NeuronCore, the **64-byte sync-instruction record** that carries a wait and an update in one word, the **all-engine-barrier composite** that fans one logical barrier out across all five datapath engines, the **drain** step that closes the cross-engine memory race, and the **embedded-events predicate** that lets an ordinary control op carry an inline wait/signal with no separate sync op.

The model has four moving parts a reimplementer must internalize, and they are easy to conflate. First, there are **two distinct banks**: a counting-semaphore bank (256 signed-32 counters, the flow-control fabric) and a one-shot **event** bank (256 one-bit edge flags); a "wait" on the first is a non-mutating poll, a "wait" on the second consumes an edge. Second, the **64-byte record is one wire format** shared by the compile-time encoder and the runtime builder — they stamp the same opcode word and the same wait/update sub-fields, byte-for-byte. Third, the **barrier is never a single instruction**: a logical `AllEngineBarrier` on the pseudo-engine `ALL` is mechanically cloned into one per-engine bundle before codegen, and each clone carries a `sem-inc` (the arrival fan-in) plus a `sem-ge` wait on the shared barrier counter (the release fan-out). Fourth, **the verifier never sees the composite** — it proves the abstract barrier topology correct *before* expansion, and the expander preserves that proof by duplicating without reordering.

This page is the **functional/architectural model**: the bank semantics, the record field map, the barrier fan-out, the drain ordering, and the inline-predicate mechanism — enough to reconstruct how the compiler enforces cross-engine ordering on the hardware sync fabric. It does **not** cover the SP-engine sequencer's branch/control instruction encoding (that is [ISA 2.20: SP sync/branch/control encoding](../isa/sp-encoding.md)), nor the backend passes that *decide* where to put a wait (that is [Part 8: `lower_sync` + barriercheck](../walrus/lower-sync.md)). The engine that physically issues every sync op is the SP sequencer — see [SP Engine](sp-engine.md) (1.12).

For reimplementation, the contract is:

- **256 signed-32 counting semaphores per NeuronCore**, addressed by an 8-bit index (`id < 256`), arch-invariant across gen2/gen3/gen4. A wait is a non-mutating threshold poll; an update is `inc`/`dec`/`set`.
- **The 64-byte sync record** — one fixed-length word (`0x40` bytes) whose first byte is the opcode and whose `+0x20..+0x28` lane carries a fused *wait-and-update*: comparator + wait-idx + subtype + act-idx + wait-value + act-value.
- **The five-engine barrier fan-out** — one `AllEngineBarrier(ALL)` → N per-engine bundles where N = popcount of the datapath mask `0x6E` = 5 engines {Pool, ?, PE, DVE, SP at ordinals 1,2,3,5,6}; each clone fans in (`sem-inc +1`) and fans out (`sem-ge value=N`) on one shared barrier counter.
- **Drain-before-sync** — a barrier always leads with a `Drain` (retire in-flight DMA/queue traffic) so a peer never proceeds past the barrier while this engine's writes are still draining.
- **The embedded-events predicate `@+0x04`** — an inline 8-byte wait/signal that the five sync ops *and* eleven control/sequencer ops carry, so most dependencies need no standalone sync op; data-path compute ops (matmul/pool/memset) never carry it.

| | |
|---|---|
| **Counting-sema bank** | 256 signed-32 counters / NeuronCore; 8-bit index; `NumSemaphores` = `NEURON_ISA_TPB_NUM_SEMAPHORES` = **256** (CONFIRMED off the four `<Arch>Core` ctors, identical gen1–gen4) |
| **Event bank** | 256 one-bit one-shot flags / NeuronCore (distinct from the counter bank) |
| **Sync record** | fixed **64 bytes** (`0x40`); opcode word `@+0` = `0x10<op>` little-endian (`0x10` = `inst_word_len` = 16 dwords) |
| **Sync opcodes (word)** | EventSemaphore `0x10A0` · Halt `0x10A1` · Drain `0x10A2` · MOV32 `0x10A7` · GroupReset `0x10B0` · Ordering `0x10B1` · Poll `0x10B3` · AllEngineBarrier `0x10D5` · CoreBarrier `0x10D8` (gen3+) |
| **Datapath engines** | mask `0x6E` = bits {1,2,3,5,6} = **5 engines**: PE (=3), DVE (=5), SP (=6), plus Pool/Act at {1,2}; engines 0 (Unassigned), 4 (DMA), 7 (ALL) excluded |
| **Compile sync emitters** | `CoreV2GenImpl::visitInstEventSemaphore` @ `0x1217df0` · `visitInstHalt` @ `0x122d620` · `visitInstDrain` @ `0x122d8d0` · `visitInstAllEngineBarrier` @ `0x122aa10` · `visitInstGroupResetSemaphores` @ `0x122ba40` |
| **Barrier expander** | `ExpandAllEngineBase::expandInstruction` @ `0xcee080` (the per-engine fan-out cloner) |
| **Sim bank model** | `Semaphores::needWait` @ `0x19e4b0` (wait) · `Semaphores::actOn` @ `0x19e300` (update); dense `uint32[256]` (`operator new(0x400)`) |
| **Runtime builders** | `add_semaphore_*` @ `0x273820..0x273ac0` · `add_evsem` @ `0x2737d0` · `add_ins` @ `0x322480` (memmove `0x40`) · `add_sync_barrier` @ `0x27af20` *(libnrt — restated)* |

---

## The two banks: counting semaphores vs one-shot events

The TPB's per-core sync fabric is **two independent banks**, and conflating them is the most common modeling error.

### The counting-semaphore bank

The compiler's geometry tree (the `getArchModel → Board → Device → Core` chain — see [Arch Object Model](arch-object-model.md)) carries one scalar field that bounds the bank:

```cpp
// data/include/hwm/ctm/ctm.hpp — the shipped header (binary-derived source-of-truth)
struct Core {
  // ...
  const uint32_t NumSemaphores;   // NEURON_ISA_TPB_NUM_SEMAPHORES
  // ...
};
```

The value is **256**, written identically by all four per-architecture `<Arch>Core` constructors (CONFIRMED — the constructor immediate is byte-identical across gen1–gen4; the `Core::Core` scalar copy lands it at `Core+0x68` ← `CoreParamSet+0x68`). There is no flat `.rodata` constant table and no runtime device probe: the count is a statically-constructed C++ field, the same number every architecture this wheel knows about carries.

The simulator realizes the bank as a **dense `uint32[256]` value array** — `operator new(0x400)` = 1024 bytes = 256 × 4 — plus a sparse red-black tree of touched ids for state dumps (CONFIRMED — `libBIRSimulator.so` allocation site). Each counter is a **signed-32** value; `inc`/`dec` wrap modularly; the wait comparison is a signed threshold compare.

The index is **8-bit**: every write site that stamps a semaphore id into a record is a single `movb` (`id ∈ [0, 256)`), and the compile-time id writer `sub_12173A0` masks the id to `&0xff` and *errors* if any bit above bit 7 is set (it compares `(id & 0xff) != 0` against `id != 0` and, when they differ, builds a stringstream diagnostic). So both the runtime byte store and the compile-time range check confine the index to exactly the 256-entry bank.

> **NOTE — re-deriving the count three ways.** The headline "256" is not a single grepped literal; it is triangulated. (1) The header field name `NEURON_ISA_TPB_NUM_SEMAPHORES` is the ISA constant; (2) the sim allocates `operator new(0x400)` = 256 × 4 bytes for the value array; (3) the index is one byte with an explicit `< 256` guard. All three agree. The per-arch bound the allocator asserts against is `HwmCore->NumSemaphores` (`AllocSemaphores::allocateSemaphore` @ `0x1121480`: `currSemaphore < currSemaIDFromUpperBound < HwmCore->NumSemaphores`). [CONFIRMED]

### The event bank (do not confuse with semaphores)

Parallel to the counters is a **256-bit one-shot event bank** — the sim models it as a `vector<bool>` of 256 bits. An event is a strict edge: a producer **sets** it (the sim *throws* on a double-set), a consumer **waits-then-clears** it (the sim is FATAL on clear-before-set). This is the rendezvous primitive for one-to-one handoffs; the counting semaphore is the *flow-control* primitive for many-to-one accumulation. The event bank is driven by the `0x10A4` op family (`ev_set`/`ev_clr`/`ev_wait`/`ev_wait_and_set`) and by the embedded-events predicate's `WaitMode 0` (`evt-set-then-clear`, wire `0x07`) / `UpdateMode 0` (`evt-set`, wire `0x11`). The `0x11` update byte is the one subtype value that never appears in a counting-semaphore record — it routes to the event bank instead.

> **GOTCHA — the runtime reserves part of each bank.** The `CoreParamSet` carries `RtReservedSemNum` and `RtReservedEventsNum` (CONFIRMED — header fields). The runtime claims a slice of the 256 semaphores and 256 events for its own bookkeeping (the barrier semaphore among them); the compiler's allocator must stay below `NumSemaphores`, but the *usable* range for compiler-allocated ids is `NumSemaphores − RtReservedSemNum`. A reimplementer that hands out all 256 ids will collide with the runtime's reserved set.

---

## The 64-byte sync record

Every SP-engine sync instruction is a fixed **64-byte (`0x40`) word**. The first two bytes are the header: byte `+0x00` is the 1-byte opcode and byte `+0x01` is the constant `0x10` = `inst_word_len` (16 dwords = 64 bytes). Read as a little-endian 16-bit word, byte `+0x00..+0x01` is therefore `0x10<op>` — this is why the same op appears both as "opcode `0xA0`" and "word `0x10A0`" in the symbol map.

### The header stamp

```c
// setupHeader @ 0x1172120 (libwalrus) — the 6-instruction body, byte-exact [CONFIRMED]
void setupHeader(u8 *bundle /*rsi*/, const u8 *opcode /*rdx*/) {
    u8 op       = opcode[0];      // movzbl (%rdx),%eax
    bundle[1]   = 0x10;           // inst_word_len = 16 dwords = 64 bytes
    bundle[0]   = op;             // the 1-byte opcode
    *(u16*)(bundle + 2) = 0;      // bytes [2:4] reserved
    // → opcode WORD (LE) = 0x10<op>
}
```

The runtime header agrees byte-for-byte: `add_ins` @ `0x322480` `memmove`s exactly `0x40` bytes to the output stream (`movl $0x40` at the call), and each builder stamps `0x10<op>` at `+0x00`. The CoreV3 `setupHeader` @ `0x1369280` is byte-identical to CoreV2's — the header (and the whole sync record format) is **architecture-invariant**.

### The unified EventSemaphore field map (word `0x10A0`)

The canonical sync record is the `EventSemaphore` (op `0xA0`). It can fuse **up to two waits and one update** in a single 64-byte word — the hardware wait-and-update rendezvous primitive. Offsets are byte offsets into the word; `RT` = runtime `libnrt` write site, `CT` = compile-time `libwalrus` write site.

| Off | W | Field | Source | Conf |
|---|---|---|---|---|
| `+0x00` | 2 | **opcode word** = `0x10A0` | `setupHeader`: `[0]=0xA0`, `[1]=0x10` | CONFIRMED |
| `+0x02` | 2 | reserved (`0x0000`) | header zero | CONFIRMED |
| `+0x04` | 1 | **wait0 mode** (wire byte) | `Wait[0].getMode → sub_120be70` LUT | CONFIRMED |
| `+0x05` | 1 | wait0 sema-idx (u8 `<256`) | `getId(Wait0) → sub_12173A0` | CONFIRMED |
| `+0x08` | 4 | wait0 value / reg-id | `mode==2 → getReg`; else `getValue` | CONFIRMED |
| `+0x20` | 1 | **wait1 / CMP-OP** (wire) | `Wait[1].getMode → sub_120be70` | CONFIRMED |
| `+0x21` | 1 | wait1 sema-idx (u8) | `getId(Wait1) → sub_12173A0` | CONFIRMED |
| `+0x24` | 4 | wait1 value / reg-id | `mode==2 → getReg`; else `getValue` | CONFIRMED |
| `+0x22` | 1 | **update / SUBTYPE** (wire) | `Update.getMode → sub_120c430` | CONFIRMED |
| `+0x23` | 1 | update sema-idx (u8) | `getId(Update) → sub_12173A0` | CONFIRMED |
| `+0x28` | 4 | update value | `Update.getValue` (skipped for modes 3/4) | CONFIRMED |

Bytes `+0x09..+0x1F`, `+0x25..+0x27`, `+0x29..+0x3F` are left zero in a semaphore record.

The compile-time encoder names these three index fields directly in `.rodata` (read at the `lea` targets of the `sub_12173A0` calls): `+0x05` ← `"instr.events.wait_idx"`, `+0x21` ← `"instr.events_extended.wait_idx"`, `+0x23` ← `"instr.events_extended.update_idx"`. So the first wait is the `events` predicate (lane-0); the optional second wait and the single update are `events_extended` (lane-2). The `+0x04/+0x05/+0x08` triple **is** the embedded-events predicate (covered below): the comparator byte at `+0x04`, the physical bank index at `+0x05`, and the threshold/register at `+0x08`.

```asm
; add_semaphore_wait_eq_and_inc @ 0x2739d0 (libnrt, runtime) — the richest record [CONFIRMED byte-exact]
mov  $0x10a0,%eax        ; opcode word
mov  %dl,0x23(%rsp)      ; act (update) sema-idx @+0x23
mov  %dl,0x21(%rsp)      ; wait sema-idx @+0x21
movb $0x01,0x20(%rsp)    ; CMP-OP = EQ @+0x20
mov  %ecx,0x24(%rsp)     ; wait value (threshold) @+0x24
movb $0x13,0x22(%rsp)    ; SUBTYPE = sem-inc (INC-on-match) @+0x22
movl $0x1,0x28(%rsp)     ; act value = 1 @+0x28
call add_ins             ; memmove 0x40 to the stream
; → one record both WAITS (cmp@+0x20, idx@+0x21, val@+0x24)
;   AND UPDATES (subtype@+0x22, idx@+0x23, val@+0x28): the atomic rendezvous.
```

> **CORRECTION — runtime and compile-time place the fused wait/update in the SAME lanes.** An earlier reading had the runtime `EventSemaphore`'s wait/update block at `+0x04` (like the inline predicate). The disassembly resolves it: the runtime `add_evsem` (`0x2737d0`) builds its record at `frame+0x10` and the per-id `add_semaphore_*` builders patch the **same** 64-byte record's lane-2 — `cmp-op@+0x20`, `wait-idx@+0x21`, `subtype@+0x22`, `act-idx@+0x23`, `wait-val@+0x24`, `act-val@+0x28`. This is byte-identical to the compile-time `events_extended` lane. Lane-0 (`+0x04`) carries the inline-events predicate (runtime) / wait0 (compile). The two encoders agree on the cmp/subtype bytes. [CONFIRMED — `add_evsem` frame math + per-builder `movb` offsets]

### The comparator byte (`+0x20`/`+0x04` wait mode)

The wait-condition comparator. Runtime values from the `add_semaphore_*` builders reconcile with the compile-time `WaitMode → wire` LUT `byte_1DF577A` = `07 05 85` and the sim's `needWait`:

| Wire | Meaning | Builder / mode | Sim semantics |
|---|---|---|---|
| `0x07` | `evt-set-then-clear` (event bank) | `WaitMode 0` | event edge wait |
| `0x01` | **EQ** (`== threshold`) | `add_semaphore_wait_eq`, barrier, clear | `read(id) == val` |
| `0x04` | WAIT-default (bare wait) | `add_semaphore_wait` (pairs subtype `0x14`) | default poll |
| `0x05` | **GE** (`>= threshold`, immediate) | `add_semaphore_wait_ge`; `WaitMode 1 sem-ge-imm` | `read(id) < val` → must-wait |
| `0x85` | **GE** (`>= threshold`, register) | `WaitMode 2 sem-ge-reg` | `read(id) < RegState.read(reg)` |

The high bit (`0x85 = 0x05 | 0x80`) is the "**threshold is a register**" flag: the value at `+0x24`/`+0x08` is a RegId, and the sim reads the live register for the threshold (used for loop-iteration-scaled thresholds). `needWait == true` means the counter has *not yet* reached the threshold ⇒ the engine stalls. A wait is a **pure predicate** — it never mutates the bank; only the update side does.

> **CORRECTION — the bare "wait" is a wait-and-decrement.** The default `add_semaphore_wait` builder writes comparator `0x04` at `+0x20` *and* subtype `0x14` (sem-dec) at `+0x22`. So the "plain wait" runtime builder is actually a wait-then-decrement: it consumes one unit from the counter on pass. A reimplementer that treats `add_semaphore_wait` as a non-mutating poll under-decrements the counter. The compile-time `EventSemaphore` encoder, by contrast, only accepts `WaitMode 1` (`sem-ge-imm`, `0x05`) or `WaitMode 2` (`sem-ge-reg`, `0x85`) per wait — its assert string is verbatim `"only two wait modes supported now: SEM_GE_IMM and SEM_GE_REG"`. EQ (`0x01`) is reachable only via the runtime barrier/clear builders. [CONFIRMED]

### The subtype byte (`+0x22`/`+0x06` update mode)

The update/act-family byte — the deliverable enum, byte-exact from the runtime builders (`movb @+0x22`) cross-checked against the compile-time `UpdateMode → wire` LUT `byte_1DF5774` = `11 15 17 13 14 19` and the sim's `actOn(Update)`:

| Wire | Name | Builder(s) | Sim effect |
|---|---|---|---|
| `0x11` | `evt-set` | (event bank — never in a sema record) | set event bit |
| `0x15` | `sem-add-imm` (INC by value) | `add_semaphore_inc`, `add_semaphore_inc_val` | `inc(id, val)` |
| `0x17` | `sem-sub-imm` (DEC by value) | `add_semaphore_dec` | `dec(id, val)` |
| `0x13` | `sem-inc` (INC by 1) | `add_semaphore_wait_eq_and_inc` | `inc(id, 1)`; **assert val==1** |
| `0x14` | `sem-dec` (DEC by 1) | bare `add_semaphore_wait`, `wait_ge_and_dec` | `dec(id, 1)`; **assert val==1** |
| `0x19` | `sem-wr-imm` (SET) | `add_semaphore_set`, `wait_eq_and_clear` | `set(id, val)` |

The full `bir::Update::toUpdateMode` enum order (the string-compare order in `libBIR` @ `0x3e04d0`) is `{0 evt-set, 1 sem-add-imm, 2 sem-sub-imm, 3 sem-inc, 4 sem-dec, 5 sem-wr-imm, 6 invalid}`, mapping onto the wire bytes above. The five *semaphore* updates are `{0x13, 0x14, 0x15, 0x17, 0x19}`; `0x11` (evt-set) is the sixth value but targets the event bank. The runtime `add_semaphore_*` builders cover exactly the five — a three-binary round-trip (runtime builder ↔ compile LUT ↔ sim) agrees byte-for-byte.

For `sem-inc`/`sem-dec` (`0x13`/`0x14`) the act value at `+0x28`/`+0x08` is **not written** — it is hard-coded to 1 (runtime `movl $0x1`; compile-time skips the store via two `cmp $3`/`cmp $4` branches; the sim asserts the implicit value is exactly 1). For `sem-add-imm`/`sem-sub-imm`/`sem-wr-imm` (`0x15`/`0x17`/`0x19`) the value is the caller's operand.

> **NOTE — the producer/consumer vocabulary.** In a many-to-one accumulation, every *producer* posts `sem-inc(+1)` (wire `0x13`) on completion, and the single *consumer* waits `sem-ge-imm(value = N)` (wire `0x05`) where N is the number of producers it depends on. One accumulated-threshold wait covers many producers — this is the synchronizer's "batching" decision: a datapath op's completion increments its engine's shared completion semaphore, and one `sem-ge N` wait gates on the whole batch. [CONFIRMED — `SetupSemaphoreUpdate` @ `0x113e900`, `SetupSemaphoreWait` @ `0x113a0b0`]

### The other sync-word records

Beyond `EventSemaphore`, the SP sync sub-family is a flat `0x10xx` opcode space. Each is a 64-byte record; the non-`EventSemaphore` ones use a simpler `+0x04..+0x08` shared sync block (wait `mode@+0x04`/`idx@+0x05`, update `mode@+0x06`/`idx@+0x07`, `value@+0x08`):

| Word | Op | Builder / encoder | Body |
|---|---|---|---|
| `0x10A1` | `0xA1` | `add_halt` / `visitInstHalt` @ `0x122d620` | Halt: sequencer stop, optional 1 wait + 1 update; v2 adds notify byte `@+0x27` |
| `0x10A2` | `0xA2` | `add_drain` / `visitInstDrain` @ `0x122d8d0` | Drain: retire outstanding queue entries; **drain-count `@+0x28`** (`Inst+0xF0`; guard `Inst+0x110==0`) |
| `0x10A7` | `0xA7` | `add_mov32_imm` / `add_mov32_reg` | MOV32: scalar register write (seeds loop counters / sema thresholds); sub-op `0x901@+0x0C`, value `@+0x20` |
| `0x10B0` | `0xB0` | `add_sema_clear` / `visitInstGroupResetSemaphores` @ `0x122ba40` | GroupReset: zero a sema range or bitmask; ClearMode `@+0x0C` |
| `0x10B1` | `0xB1` | `add_set_ordering_mode` | Ordering fence: ordering-mode byte `@+0x0C`; rest zero |
| `0x10B3` | `0xB3` | `add_poll_sem` | Poll: non-blocking sema read; mode `@+0x0C`, idx `@+0x0D`, const `0x09@+0x0E` |
| `0x10D5` | `0xD5` | `visitInstAllEngineBarrier` @ `0x122aa10` | AllEngineBarrier (the per-engine fence, below) |
| `0x10D8` | `0xD8` | `CoreV3GenImpl::visitInstCoreBarrier` @ `0x1356550` | CoreBarrier — cross-**core** rendezvous, gen3+ only |

The **drain count** is the most subtle field. `visitInstDrain` reads `Inst+0xF0` (= 240) into `+0x28` of the bundle, but first guards on `Inst+0x110` (= 272) being zero — that tag is the variant discriminator, and a non-zero tag throws `bad_variant_access`. In other words the drain count **must be a compile-time constant** (the `QuasiAffineExpr`'s constant arm), and `lower_control` mints fence drains with count = 1 (full drain), tag cleared:

```asm
; visitInstDrain @ 0x122d8d0 — the count + guard [CONFIRMED]
movzbl 0x110(%r13),%eax ; test %al,%al ; jne <throw>   ; +272 variant-tag guard (must be 0)
movzbl 0x0f0(%r13),%eax ; ... ; mov %al,0x28(%r12)     ; +240 → bundle +0x28 (the drain count)
```

> **CORRECTION — the runtime GroupReset emits ONLY the range form.** The compile-time `GroupResetSemaphores` writes `ClearMode@+0x0C` and *either* a contiguous range (`range_first@+0x0D`, `range_last@+0x0E`, ClearMode `0x01`) *or* a 256-bit bitmask (`@+0x20`, ClearMode `0x02`). The runtime `add_sema_clear` writes **only** the range form, and stages the range in lane-0 (`range_first@+0x04`, `range_last@+0x08`) before a `movdqa` repacks it down. Any blanket claim that the reset range lives at `+0x0D/+0x0E` is a **compile-time-only** fact. The ClearMode byte position (`+0x0C`) and the `SemaphoreZero` value (`0x01`) agree on both sides. [CONFIRMED — `add_sema_clear` frame math vs `visitInstGroupResetSemaphores` disasm]

---

## The embedded-events predicate

The marquee mechanism: **every SP-engine sync/control record can carry an inline 8-byte wait/signal predicate at `+0x04`**, so an instruction performs a semaphore (or event) wait-or-signal *without* a separate sync op preceding it.

### The runtime mechanism

```asm
; insert_events @ 0x271910 (libnrt) — the one-liner [CONFIRMED]
mov %rdx, 0x4(%rdi,%rsi,1)   ; rdi = record base, rsi = run offset, rdx = the 8-byte events payload
                             ; → writes the events qword at record+0x04
```

Inside every builder that can carry a predicate (`add_evsem`, `add_poll_sem`, `add_drain`, `add_halt`, `add_mov32_imm`, `add_br_hint_rel_imm`, …) the tail is the identical idiom:

```c
// the optional-predicate tail, present byte-for-byte in every predicate-bearing builder
if (events_ptr /*r8*/ != NULL) {
    record[0x04 .. 0x0B] = *(u64*)events_ptr;   // test %r8,%r8 ; je skip ; mov (%r8),%rax ; mov %rax,0x4(%rsp)
}
```

So `+0x04` is an **optional, shared** slot across the whole SP family. The 8-byte predicate packs `mode@+0x04` (the wait/signal wire byte, same comparator/subtype values as above), `idx@+0x05` (the 8-bit bank index), and the dword `@+0x08` (threshold/value). The compile-time analog is exactly the `EventSemaphore` `wait0` triple (`mode@+0x04`/`idx@+0x05`/`val@+0x08`, written by `setupSyncWait`/`setupSyncUpdate`) — the encoder fills `+0x04` from the instruction's first `SyncInfo` wait, the very slot the runtime drops the inline events qword into.

### Who carries the predicate — and who does not

This is the precise answer to "how do most dependencies avoid a separate sync op." Sweeping every call site of `bir::Instruction::getSyncInfo()` (190 sites) and filtering to the CoreV2 codegen band, the encoders that read `getSyncInfo` — and therefore stamp an inline wait/update — are exactly:

- **The five sync ops**: `EventSemaphore`, `Halt`, `Drain`, `AllEngineBarrier`, `GroupResetSemaphores`.
- **Eleven control/sequencer ops**: `TensorCompletion`, `Exit`, `Return`, `BranchHint`, `UnconditionalBranch`, `RegisterMove`, `SetRandState`, `DMATrigger`, `NoOp`, `GetCurProcessingRankID`, `InlineASMBytes`.

The **data-path compute encoders make zero `getSyncInfo` calls** — verified at `visitInstMatmul` (`0x1248650`), `visitInstPool` (`0x1239e50`), `visitInstMemset` (`0x125b320`): count = 0 each; `visitInstActivation` (`0x12596f0`) makes no sync calls at all. The conclusion is structural: in the TPB ISA, a *compute* op's dependencies are **not** satisfied by an inline predicate stamped into the compute bundle. They are realized by a **standalone `EventSemaphore`** op that `lower_sync` inserts adjacent to the compute op. The inline predicate is what *control/sequencer* ops carry — the synchronizer attaches a `Wait`/`Update` to whatever convenient control op already sits on the engine, and the L3 encoder stamps it inline.

```c
// setupSyncWait<ISA_STRUCT> — the WAIT half of the shared template [CONFIRMED, byte-walked]
void setupSyncWait(u8 *bundle, const Wait &w) {
    bundle[0x04] = wire_of_waitmode(w.getMode());   // sub_120be70 LUT {07,05,85}
    bundle[0x05] = (u8) w.getRef().getId();         // sub_12173A0, id < 256
    if (w.getMode() == 2 /*sem-ge-reg*/)
        *(u32*)(bundle + 0x08) = w.getReg();        // threshold is a register
    else /*sem-ge-imm*/
        *(u32*)(bundle + 0x08) = w.getValue();      // threshold is an immediate
}
// setupSyncUpdate<ISA_STRUCT> — the UPDATE half [CONFIRMED]
void setupSyncUpdate(u8 *bundle, const Update &u) {
    bundle[0x06] = wire_of_updmode(u.getMode());    // sub_120c430 LUT {11,15,17,13,14,19}
    bundle[0x07] = (u8) u.getRef().getId();         // sub_12173A0
    if (u.getMode() != 3 /*sem-inc*/ && u.getMode() != 4 /*sem-dec*/)
        *(u32*)(bundle + 0x08) = u.getValue();      // value skipped for the implicit ±1 modes
}
```

> **NOTE — `EventSemaphore` is the inline exception.** The shared template writes the update mode/idx at `+0x06`/`+0x07`. `EventSemaphore` does *not* use the template — it has its own two-wait/one-update walk that places `wait0` at `+0x04`, `wait1` at `+0x20`, and the update at `+0x22`/`+0x23`/`+0x28`. This is why the `EventSemaphore` field map differs from every other sync op: it is the only record that can fuse two waits and is the record `lower_sync` materializes when a dependency needs a standalone carrier. [CONFIRMED]

---

## The all-engine barrier composite

A barrier is **never a single instruction**. The compiler mints one logical `AllEngineBarrier` on the pseudo-engine `ALL`, the verifier proves the abstract topology, and *then* an expander mechanically clones it into one bundle per datapath engine before codegen.

### The five datapath engines

The fan-out width N is the number of datapath engines, derived from the mask in `bir::isDataPathEngine` (`libBIR` @ `0x47fa00`):

```c
bool isDataPathEngine(EngineType e) { return (1 << e) & 0x6E; }   // test al, 0x6e [CONFIRMED]
```

Re-deriving the mask: `0x6E` = `0b0110_1110` = bits **{1, 2, 3, 5, 6}** — exactly **five** engines. The wiki's confirmed `EngineType` ordinals pin three of them: **PE = 3** (`getDefaultEngine` @ `libBIR 0x3e2cb0` → 3 for all matmul ops), **DVE = 5** (`EngineType2string → "DVE"`, external name "Vector"), **SP = 6** (`isDataPathEngine ... SP=6 ∈ datapath`). The remaining two bits {1, 2} are **Pool** and **Activation**. Excluded from the mask: engine **0** (Unassigned), engine **4** (**DMA** — it owns rotated per-queue semaphores, not the datapath barrier), and engine **7** (**ALL** — the pseudo-engine, which is precisely why a barrier on `ALL` cannot be encoded directly).

> **CORRECTION — the name↔ordinal binding for the five engines.** The five datapath engines are the bits of `0x6E` = {1,2,3,5,6}. The confirmed ordinals are **PE=3, DVE=5, SP=6** (from the wiki's `getDefaultEngine`/`EngineType2string`/`isDataPathEngine` evidence). The two engines at ordinals {1, 2} are Pool and Activation; *which is 1 and which is 2* is not separately byte-pinned here. An earlier informal list assigned PE=1/Pool=3/DVE=6, which conflicts with the confirmed PE=3/DVE=5 and is **wrong** — use the confirmed ordinals. [CONFIRMED for the mask, PE=3, DVE=5, SP=6; INFERRED for the Pool/Act split across {1,2}]

### Why expansion is mandatory

The SP encoder `visitInstAllEngineBarrier` @ `0x122aa10` asserts `isDataPathEngine(I.getEngine())` (the verbatim assert string is `"isDataPathEngine(I.getEngine())"`, plt call @ `0x122abc0`). The pseudo-engine `ALL(7)` is **not** in the mask `0x6E`, so an `ALL` barrier cannot be encoded — the assert is the forcing function that requires the fan-out. The same assert guards `GroupResetSemaphores` @ `0x122ba40`.

### The fan-out cloner

```c
// ExpandAllEngineBase::expandInstruction @ 0xcee080 — the per-engine fan-out [CONFIRMED, full disasm]
void expandInstruction(Instruction *I) {            // I = rdx
    if (I->engine /*+0x90*/ != 7 /*ALL*/) return;   // cmp [rdx+0x90],0x7 ; jne <passthrough>
    auto engines = function.datapathEngines();       // vector<EngineInfo{type,sub}>, from listEnginesUsed

    // engine[0]: REASSIGN the original in place
    I->engine    = engines[0].type;                  // mov %eax,0x90(...)
    I->subengine = engines[0].sub;                   // +0x94
    rewireRegisterAccesses(I);                       // per-engine register remap
    result.push_back(I);

    // engines[1..N-1]: fullClone one new instruction each
    for (auto ei : engines[1:]) {
        auto suffix = EngineInfo2string(ei);                       // "<name>" per-engine tag
        auto clone  = I->fullClone(I->name + "." + suffix, I->parentBB);
        insertIntoSymbolTable(clone);
        clone->copyDependencies(I);                                // preserve happens-before
        clone->copyDescendents(I);
        clone->engine    = ei.type;  clone->subengine = ei.sub;    // per-engine stamp
        rewireRegisterAccesses(clone);
        result.push_back(clone);
    }
}
```

One `ALL(7)` barrier op → the first datapath engine **reuses** the original (engine field rewritten in place), and each remaining datapath engine gets a `fullClone` with a per-engine name suffix, copied dependencies/descendents, and its engine field stamped. Result: **N per-engine `AllEngineBarrier` bundles** (N = number of datapath engines the module uses, sourced from `Module::listEnginesUsed` @ `0x5f9010`, with `enterFunction` @ `0xcf9200` building the `EngineInfo` set and pre-allocating a per-engine register).

There are **two** expand passes: `expand_all_engine_pre_alloc_sema_and_reg` (`ExpandAllEnginePreAllocSemaAndReg::run` @ `0xce97e0`) expands the control/register ops *early* so the synchronizer allocates per-engine semaphores; `expand_all_engine_final_pre_codegen` (`ExpandAllEngineFinalPreCodegen::run` @ `0xce9b10`) expands the barrier/reset fences *late*, just before codegen, when the sema indices are final.

### The per-engine wait/update — the fan-in / fan-out

After expansion each per-engine barrier instance is an ordinary datapath instruction, and the **synchronizer** attaches its `SyncInfo`:

- **`SetupSemaphoreUpdate`** (@ `0x113e900`) gives the producer side `Update{mode = sem-inc(3), value = 1}` on the shared barrier counter — the **arrival post**, the fan-in. Each engine increments the barrier semaphore by 1 on reaching the barrier.
- **`SetupSemaphoreWait`** (@ `0x113a0b0`) gives the consumer side `Wait{mode = sem-ge-imm(1), value = N}` on the same counter — the **central wait**, the fan-out. Each engine blocks until the barrier counter reaches N = number of engines arrived.

`PadControlFlowPaths` balances the per-CFG-path post counts so the immediate threshold N is correct on every branch. The resulting per-engine `0xD5` bundle's shared sync block holds:

```
+0x04 wait mode   = 0x05 (sem-ge-imm GE)   +0x05 wait sema-idx = <barrier sema>
+0x06 update mode = 0x13 (sem-inc)         +0x07 update sema-idx = <barrier sema>
+0x08 value       = N  (the wait threshold; the sem-inc update value is implicit 1)
```

This is a **flat all-to-one fan-in + one-to-all fan-out** on a single counting semaphore — not a tree/log-depth barrier. Each of N engines does `sema[S] += 1` (arrival) and waits `sema[S] >= N` (release).

### Whether the barrier uses one shared counter or one per engine

The **runtime** form is unambiguous: `tdrv_sync_get_sync_barrier` allocates a **single** barrier semaphore id, and all N `wait_eq_and_inc` records reference it (each engine increments the same id, each waits for it to reach N). The **compile-time** synchronizer's general model is one completion semaphore *per engine*; the all-engine barrier is realized either as an all-to-all mesh or (matching the runtime, and consistent with the `GroupResetSemaphores` reset-all flag re-arming the whole bank) a single dedicated barrier counter all engines increment and wait on.

> **GOTCHA — the exact compile-time id-sharing policy is INFERRED.** It is STRONG that each per-engine barrier instance posts/waits on a *shared* barrier semaphore (the GroupReset reset-all and the runtime single-id form both point that way), but whether the compile-time expansion uses exactly **one** id or one-per-engine for the arrival posts is not separately field-pinned — it lives in the `SetupSemaphoreUpdate`/`SetupSemaphoreWait` id resolution for the expanded barrier ops. The runtime's single-id form is the authoritative on-silicon end state. [INFERRED — flagged]

### The runtime composite (libnrt)

The runtime realizes the same shape as a function-level composite with no opcode of its own:

```asm
; add_sync_barrier @ 0x27af20 (libnrt) — restated from disassembly of that binary [CONFIRMED there]
call tdrv_sync_get_sync_barrier   ; allocate / reserve a single barrier semaphore id  → r15
call al_hal_tpb_get_tpb_eng_count ; N = number of engines
call add_drain                    ; DRAIN (queue flush) — leads the barrier
call add_semaphore_wait_eq_and_inc ; per-engine post+wait (looped/unrolled across N engines)
call add_semaphore_wait_eq_and_inc ;   cmp EQ 0x01 + subtype INC 0x13 + inc-amount 1
; → runtime all-engine barrier = DRAIN + N × wait_eq_and_inc on a freshly-allocated barrier sema.
```

The runtime uses **EQ** (`0x01`) where the compiler uses **GE** (`0x05`) — both are correct on a monotonic +1 counter: EQ works because the runtime drains and re-arms the barrier sema each pass; GE is the compiler's robust form. The runtime opcode word's low byte equals the compile-time 1-byte opcode for every op (`drain 0x10A2 ↔ 0xA2`, `sema 0x10A0 ↔ 0xA0`), so the two encoders agree on bytes `+0x00`/`+0x01` and on the `+0x04` predicate. The runtime `add_sync_barrier` is the on-silicon realization of the compile-time per-engine `0xD5` bundle family — same drain, same per-engine sema fan-in/fan-out, same 256-counter/signed-32/8-bit-index bank.

---

## Drain & anti-race ordering

A cross-engine semaphore rendezvous guarantees only **program-order arrival**, not **completion** of in-flight asynchronous work. An engine can reach the barrier instruction while its previously-issued DMA / queue descriptors are still draining through the DMA queues. If it posted its barrier-arrival semaphore *before* draining, a peer could proceed past the barrier while this engine's writes were still in flight — a cross-engine memory race. So the order is mandatory: **DRAIN (retire in-flight) → POST arrival → WAIT for all → proceed.** The sim models `Drain` as an `on_wait` that blocks until the engine's outstanding ops complete.

`lower_control` (pass order 128) brackets every control-transfer basic block's tail, immediately before the terminator, with a five-op fence on the pseudo-engine `ALL(7)`:

```
<all real engine work>
  → AllEngineBarrier(ALL)        # rendezvous #1 — no engine starts the reset early
  → GroupResetSemaphores(ALL)    # re-arm the sema bank (zero the counters)
  → AllEngineBarrier(ALL)        # rendezvous #2 — no engine observes a half-reset bank
  → Drain(ALL)                   # flush all in-flight DMA / queue traffic
  → [CoreBarrier(all cores)]     # cross-core rendezvous, only if lnc_size > 1
  → <terminator>
```

Two `AllEngineBarrier`s **bracket** the semaphore reset so no engine ever observes a half-reset counter bank across the edge; the `Drain` forces in-flight DMA to retire; the optional `CoreBarrier` makes the inter-core rendezvous explicit (gen3+, multi-core only — see [LNC memory model](lnc-memory-model.md)). Every real instruction in the block is chained as a dependency of the fence head, so the fence is a true barrier (happens-after all engine work). After per-engine expansion, *each* of `{Barrier1, Reset, Barrier0, Drain}` becomes N per-engine bundles; `GroupResetSemaphores` deep-copies its `sema_group` vector per engine so each engine resets its own range.

> **GOTCHA — `CoreBarrier` is a different axis; do not conflate it with `AllEngineBarrier`.** `AllEngineBarrier(IT15, 0xD5)` is **intra-core, cross-engine** — the per-engine sema fan-in/fan-out on this page. `CoreBarrier(IT87, 0xD8, gen3+)` is **cross-core** — a hardware barrier register plus a module-wide index plus a `coreIdList`, present only when `lnc_size > 1`. The all-engine barrier synchronizes the five engines *within* a NeuronCore; the core barrier synchronizes the fused cores of an LNC. They are orthogonal. [CONFIRMED]

---

## What the verifiers see

The legality verifiers reason about the **abstract** barrier, *before* expansion — and that ordering is what makes the model sound.

The pipeline runs `lower_control` (128, mints the abstract `ALL(7)` fence) → `lnc_barriercheck` (130) + `bir_racecheck` (131, the last legality gates) → **then** codegen, where the `ExpandAllEngine` passes run. So every verifier always operates on the abstract, pre-expansion IR:

- **The LNC barrier checker** (`BarrierRanger`, `libLncBarriercheck.so`) operates on `CoreBarrier(IT87)` band indices — **not** `AllEngineBarrier` and not the per-engine `0xD5` bundles. It assigns each instruction a `[first, second]` band of `[latest-ancestor-CoreBarrier, earliest-descendant-CoreBarrier]` per engine, and reports a missing-barrier race iff two conflicting **cross-core** accesses' bands overlap. The seed set is `CoreBarrier` nodes only; `AllEngineBarrier` (the intra-core fence) does not seed the band.
- **The BIR verifier's `visitInstAllEngineBarrier`** (`libwalrus` @ `0xfa5fa0`) does a generic `visitInstruction` + `checkMemType` on the abstract `AllEngineBarrier` — it validates the op's memory-type / IO, **not** a per-engine composite.
- **The simulator** models `AllEngineBarrier(IT15)` as a **scheduler counted join**: in the ready-selector (`getNextInstruction` @ `0x14f520`), each engine whose head is `IT15` increments an arrival counter and is skipped; when `numDataPathEngines == arrivals`, the scheduler releases the joint barrier as one instruction (`fetchAllEngineInst<InstAllEngineBarrier>` @ `0x16bf00`), whose own `SyncInfo` then fires. `Exit(IT82)` uses the identical join. The sim confirms the *semantics* — "all data-path engines must arrive before any proceeds" — at the abstract level, exactly the fan-in/fan-out the per-engine sema records implement on hardware.

The contract is therefore: **verify abstract → expand → synchronize → encode.** The verifier proves the abstract barrier topology correct; the expander then lowers that proven-correct barrier into the concrete per-engine sema mesh, which the synchronizer wires and the SP encoder emits. The expansion is a mechanical per-engine clone that copies dependencies/descendents and never reorders, so the happens-before the verifier validated is preserved through codegen. The verifier never needs to see the composite.

---

## Lifecycle: from a dependency edge to a 64-byte word

End to end, a cross-engine ordering constraint becomes hardware sync like this (the wire mode bytes are the bridge that ties each layer's vocabulary together):

1. **`build_fdeps`/anti/order** — the dependency graph names every cross-engine edge.
2. **`dep_reduction`** — stamps each instruction's engine position at `Inst+0x90`.
3. **Synchronizer** — `allocSema` (@ `0x11324e0`) hands out ids 0..255 (linear counter + a freed-chunk reuse pool); `assignEngineSemaphore` gives each datapath op its engine's shared **completion** semaphore (batching: one accumulated-count wait covers many producers); `assignDMASemaphore` gives DMA ops a **rotated** per-queue semaphore (with a same-loop drain wait on reuse). `SetupSemaphoreUpdate` posts `sem-inc(3)`; `SetupSemaphoreWait` attaches `sem-ge-imm(1)`/`sem-ge-reg(2)`; `PadControlFlowPaths` balances per-path counts; same-engine edges are elided.
4. **`alloc_semaphores`** — bank-bounds the ids (`< NumSemaphores`) and inserts DMA rotation drains.
5. **`lower_sync`** — materializes standalone `EventSemaphore(IT13)` ops for the dependencies that need a separate carrier, and leaves the rest as inline predicates on a convenient control/seq op; `GroupResetSemaphores(IT14)` re-arms the bank.
6. **`lower_control`** — brackets control-transfer blocks with `[Barrier · Reset · Barrier · Drain (· CoreBarrier)]` on engine `ALL(7)`.
7. **`ExpandAllEngineFinalPreCodegen`** — `fullClone`s the `ALL` fences per datapath engine (stamping `Inst+0x90`).
8. **CoreV2 encoder** — reads each instruction's `SyncInfo` and emits the 64-byte bundle.

The wire-mode mapping is the through-line: synchronizer `sem-inc` / `sem-ge-imm` / `sem-ge-reg` ↔ wire `0x13` / `0x05` / `0x85` ↔ sim `inc(1)` / `read < imm` / `read < reg`. The simulator replays the base `SyncInfo` against the 256-counter bank and the 256-bit event bitset, closing the loop.

---

## Confidence summary

**CONFIRMED** (byte-exact, multi-binary):

- The unified 64-byte `EventSemaphore` field map (opcode word `@+0x00`; wait0 `@+0x04/+0x05/+0x08`; wait1/cmp `@+0x20/+0x21/+0x24`; update/subtype `@+0x22/+0x23/+0x28`), with all three idx-field `.rodata` strings.
- `add_ins` `memmove` = exactly `0x40` bytes; `setupHeader` stamps `[1]=0x10` → the 64-byte word; CoreV3 header byte-identical.
- The comparator set `{0x01 EQ, 0x04 default, 0x05 GE-imm, 0x85 GE-reg, 0x07 evt}` and the subtype set `{0x13 sem-inc, 0x14 sem-dec, 0x15 add-imm, 0x17 sub-imm, 0x19 wr-imm, 0x11 evt-set→event bank}`.
- The opcode-word family `{0x10A0 sema, 0x10A1 halt, 0x10A2 drain, 0x10A7 mov32, 0x10B0 group-reset, 0x10B1 ordering, 0x10B3 poll, 0x10D5 all-engine-barrier, 0x10D8 core-barrier}`.
- `NumSemaphores` = **256**, signed-32 counters, 8-bit index, arch-invariant gen1–gen4; the sim's `operator new(0x400)` dense array and the `< 256` index guard.
- The datapath mask `0x6E` = {1,2,3,5,6} = **five** engines; PE=3, DVE=5, SP=6 confirmed.
- The barrier fan-out cloner `expandInstruction` @ `0xcee080`; `isDataPathEngine` assert forces expansion; per-engine `sem-inc`/`sem-ge-imm value=N` wait/update; the drain-leads ordering; the verify-abstract-then-expand pipeline order.
- The embedded-events predicate (`insert_events` one-liner + the per-builder optional-`r8` idiom + the `setupSyncWait`/`setupSyncUpdate` template) and the `getSyncInfo` census (5 sync ops + 11 control ops carry it; compute ops carry zero).

**INFERRED / STRONG** (flagged inline):

- The Pool/Act split across engine ordinals {1, 2} — confirmed they are the two remaining datapath engines, but not which is 1 vs 2.
- Whether the compile-time barrier uses exactly **one** shared barrier-counter id (matching the runtime) or one-per-engine for the arrival posts — STRONG that it is shared; the exact id-equality policy is not separately field-pinned. The runtime single-id form is authoritative for the end state.
- The internal packing of the 8-byte events qword above `+0x05` (whether `+0x06/+0x07` hold a second idx or padding) — the `insert_events` store is one qword; the mode/idx/val sub-structure is STRONG, not byte-walked.
- The runtime `add_sync_barrier` decode is **restated** from disassembly of `libnrt.so`, which is absent from this compiler wheel; the compile↔runtime identity on the shared bytes is STRONG.

---

## Cross-References

- [SP Engine](sp-engine.md) (1.12) — the sequencer engine that physically issues every sync op on this page; the `0x10xx` opcode space is its instruction set.
- [PE Engine](pe-engine.md) · [Pool Engine](pool-engine.md) · [DVE Engine](dve-engine.md) — three of the five datapath engines that the all-engine barrier fans out across (`getDefaultEngine → PE=3`, `EngineType::DVE=5`).
- [Arch Object Model](arch-object-model.md) — the `getArchModel → Board/Device/Core` chain carrying `NumSemaphores=256` and the `EngineType` ordinals; the `RtReservedSemNum`/`RtReservedEventsNum` reserve fields.
- [LNC memory model](lnc-memory-model.md) — the multi-core fusion that introduces `CoreBarrier`; the cross-core axis orthogonal to the all-engine barrier.
- [ISA 2.20: SP sync/branch/control encoding](../isa/sp-encoding.md) — the bit-level encoding of the SP sequencer's sync/branch/control instructions whose field map this page summarizes.
- [Part 8: `lower_sync` + barriercheck](../walrus/lower-sync.md) — the backend passes that *decide* where to insert a wait, mint the abstract fence, and run `lnc_barriercheck`/`bir_racecheck` before expansion.
- [Part 7: BIR sim `SyncState`](../bir/sim-syncstate.md) — the reference interpreter's 256-counter bank + 256-bit event bitset and the scheduler counted-join barrier model.
