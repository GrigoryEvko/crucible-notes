# The ISS as Executable Oracle

> **The final page of Part 14 — the roll-up.** The other nineteen `iss/` pages
> each answer one question about the GPSIMD Vision-Q7 instruction-set simulator:
> what the cas core *schedules* ([core surface](./cas-core-surface.md), the eight
> cas op-class pages, [the timing model](./cas-timing-model.md)), what the fiss
> core *computes* ([surface + exceptions](./fiss-surface-exceptions.md), the three
> slotfill pages, [the datapath oracle](./fiss-datapath-oracle.md)), where the
> model was *generated from* ([libtie core + msem](./libtie-core-msem.md),
> [libctype c-stub](./libctype-cstub.md)), how you *run* it
> ([runnable infrastructure](./runnable-iss-infra.md)), whether `-ref-` *diverges*
> ([ref-vs-production diff](./ref-vs-production-diff.md)), and what every
> instruction *means* ([the semantic synthesis](./iss-semantic-synthesis.md)).
> **This page does the last two jobs.** First it documents the ISS's
> **introspection / single-step / fault / SystemC** surface — the debug seam a
> reimplementer drives. Then it closes the thesis the whole Part was built to
> prove: **the cas + fiss ISS *is* the executable golden oracle for GPSIMD
> semantics, and a reimplementer uses it as the differential reference.** It welds
> the nineteen siblings; it does not restate them.

All facts below are derived from static analysis of the shipped binaries plus a
**live `ctypes` drive** that certifies a fresh value leaf (§6). Each claim is
tagged **confidence** × **provenance** following
[the Confidence & Walls model](../reference/confidence-model.md): `[HIGH/OBSERVED]`
= read directly off the binary / executed via `ctypes`, `[MED/INFERRED]` = strong
deduction over observed evidence, `[…/CARRIED]` = re-used at a sibling's
confidence.

Binaries, by absolute role (paths under
`extracted/nested/gpsimd_tools_tgz/tools/`):

```
ncore2gp/config/libcas-core.so       45,878,080 B   cycle/timing core (decode + hazards, 0 values)
ncore2gp/config/libfiss-base.so      12,330,016 B   value oracle (slotfill + xdref + 61 exceptions)
ncore2gp/config/libtie-core.so       51,098,208 B   TIE-DB source the xdref/xdsem leaves were generated from
ncore2gp/config/libctype.so             388,648 B   host value marshalling (orthogonal to the oracle)
XtensaTools/lib/iss/libsimxtcore.so  ~2.8 MB        the cycle-ISS HARNESS (disasm + TRACER/TRAX + cosim)
```

`.text`/`.rodata` are VMA==file-offset in every config DLL; the writable sections
carry the ncore2gp delta **`+0x200000`** (subtract before `xxd` on `.data`/
`.data.rel.ro`). `libcas-core` ships a `.symtab` but **no `.debug_*`** — every
offset below is pinned from the `mov`/`lea`-displacement encoding, not DWARF.

---

## 1. The introspection surface — reading ISS state back out

A debugger's first need is **state readback**: given a live simulated core, recover
the value of any architectural register, the pipeline occupancy, and the pending
write set. The cas ISS exposes this through the same flat `dll_*` vocabulary that
[the runnable infrastructure](./runnable-iss-infra.md) §1 documents as a
lifecycle; here we read it as an **introspection API**.

### 1.1 The state-readback accessors

Three of the 24 `dll_*` accessors are pure descriptor publishers — a debugger
calls them once to learn *where* every piece of state lives inside the per-instance
block, then peeks the block directly. Each is a literal
`lea <table>(%rip),%rax ; ret`:

| Accessor | Target table | What it tells a debugger |
|---|---|---|
| `dll_get_state_table` `@0x17aa370` | `dll_state_table` (`0x221a3c0`, 4592 B) | the per-register **state-offset map** — the row giving each SR/regfile its byte offset inside the block |
| `dll_get_regfile_table` `@0x17aa380` | `dll_regfile_table` (`0x227eac0`, 504 B = 63 slots) | per-regfile geometry (count × width); the 8-file model `AR 64×32 … gvr 8×512` |
| `dll_get_exception_table` `@0x17aa2d0` | `dll_exception_table` (1008 B, 61 handlers) | the **fault decoder roster** (§4) |

`dll_state_table` is the introspection keystone: it maps a register name to the
offset a peek must read. The regfile values themselves land at
**`state+0x14eb0`** (the architectural register-slot region; `lea 0x14eb0(%rbx),%rdx`
in `dll_initialize`), and the per-cycle committed values at **`state+0x152xx`**
(the host-value bases). A debugger that has the offset map plus the block pointer
can read any register without re-running anything.

### 1.2 The introspection-relevant state-block offsets

The per-instance state block (`0x4a09f0` = **4,852,208 B**, §5/§7) carries a fixed
set of fields a debugger reads each step. The seven that matter for introspection,
each decoded from an accessor or scoreboard body: `[HIGH/OBSERVED offsets;
MED/INFERRED region spans]`

| Offset | Field | Introspection use |
|---|---|---|
| `state+0x8` | installed harness vtable base | the `register`/value callbacks the core calls back through |
| `state+0x5a8` | **flags byte** | bit0 event-trace, bits1–2 stall-eval mode, **bit3 cycle-advanced** (§2) |
| `state+0x8f8` | **scoreboard / regfile commit array** | the committed-write landing zone (`+0x8f8 + reg*4`) |
| `state+0x24ec` | **ring head** (`& 0x1f`) | which of the 32 ring stages is "now"; decremented per tick |
| `state+0x484c` | pipeline-stage index (`& 0x1f`) | the live stage cursor every scoreboard accessor reads |
| `state+0x14eb0..` | **architectural register slots** | the readable register values; first slot = `"AR"` (§1.3) |
| `state+0x152xx` | host **value** callback bases | the datapath handoff bases (10,807 refs across the disasm) |

The register-slot region at `+0x14eb0` and the dispatch-hook bases at `+0x152xx`
(values) / `+0x158xx` (timing) give the structural rule a reimplementer needs:
**`0x152xx` = host-values, `0x14exx`/`0x158xx` = timing** — the seam
[the timing model](./cas-timing-model.md) §7 decodes in full.

### 1.3 The first registered architectural register is `"AR"`

`dll_initialize` walks every architectural register and calls back through the
harness vtable (`harness->register(handle, name, &slot)`) to publish each one
(see [core surface](./cas-core-surface.md) §2.3 / [runnable](./runnable-iss-infra.md)
§1.2). The **first** registration names the scalar register file — and the byte
truth is subtler than the name string alone:

> **CORRECTION — the first registered register is `"AR"`, the suffix-merged tail of
> `"REV8AR"`.** The `lea` for the first registration targets `.rodata 0x17cdb53`.
> `objdump -s -j .rodata` around it reads
> `… 57 42 5f 4d 00  52 45 56 38 41 52 00  4d 53 …` =
> `"WB_M\0" "REV8AR\0" "MS_…"`, so `"REV8AR\0"` begins at `0x17cdb4f` and the bytes
> at `0x17cdb53` are exactly `41 52 00` = **`"AR\0"`**. The compiler **suffix-merged**
> the `"AR"` literal into the tail of `"REV8AR"`, so the registration `lea` points
> at the `"AR"` substring four bytes in, **not** the head of `"REV8AR"`. The first
> architectural register the ISS publishes is therefore the windowed scalar **AR**
> file (`num_aregs = 64`), not the `REV8AR` special register. A page that reads the
> first registration name as `"REV8AR"` mis-attributes the `lea` displacement; the
> `+4` offset is the difference.

This is the canonical string-pool suffix-sharing trap: a tool that resolves a
registration `lea` to "the symbol the literal starts inside" rather than "the
null-terminated string at the displacement" will report `REV8AR` for what is
genuinely `AR`. For a reimplementer the introspection rule is concrete: the first
register slot at `state+0x14eb0` is the **AR** windowed scalar file.

---

## 2. Single-step — `dll_cycle_advance` is the step primitive

A debugger steps a cycle-accurate ISS **one cycle at a time** and reads state
between ticks. The GPSIMD ISS has no separate step opcode: the single-step
primitive **is** the tick stepper `dll_cycle_advance`, the same call
[runnable infrastructure](./runnable-iss-infra.md) §2 documents as the run-loop
body. The introspection reading of it is what makes it a *debugger* primitive, not
just a clock.

### 2.1 The step-and-observe contract

`dll_cycle_advance @0x17aa3c0 (rdi=state, esi=dir)` does three observable things
per call, each verified from the disassembly:

```text
dll_cycle_advance @0x17aa3c0 :
  17aa3cb: push regs ; mov %esi,%r12d        ; dir(esi) saved
  17aa3d4: mov  0x24e4(%rdi),%r9d            ; load cycle-advance guard
  17aa3db: 80 8f a8 05 00 00 08              ; orb $0x8,0x5a8(%rdi)  -> SET bit3 "cycle advanced"
  ...      commit up to 4 WB lanes -> state+0x8f8 scoreboard
  ...      ring head state+0x24ec := (head - 1) & 0x1f   ; exactly ONE stage / tick
  ...      shift the 32-deep ring stage+1 -> stage
  ...      dir(esi) {==0 / <0 / >0}: forward-commit vs ROLLBACK
```

The debugger-step loop is therefore:

```c
// single-step one cycle and read back state
for (;;) {
    dll_cycle_advance(state, /*dir=*/0);     // advance one tick; sets flags bit3
    bool advanced = (FLAGS(state) >> 3) & 1; // state+0x5a8 bit3 — DID a step occur?
    inspect_registers(state, dll_get_state_table(state));  // peek via the offset map (§1.1)
    inspect_scoreboard(state /* +0x8f8 */, state /* +0x24ec ring head */);
    dll_reset_cycle_advanced(state);         // andb $0xf7,0x5a8 — clear bit3, ARM the next probe
    if (halted) break;
}
```

**The `state+0x5a8` bit3 flag is the step acknowledgement.** `dll_cycle_advance`
*sets* it (`orb $0x8`); `dll_reset_cycle_advanced @0x17aa3b0`
(`andb $0xf7,0x5a8(%rdi) ; ret`) *clears* it. A debugger drives one cycle, reads
bit3 to confirm the step happened, peeks state through the §1 accessors, then
clears bit3 to re-arm the next single-step probe.

### 2.2 The `dir` argument makes the step reversible

> **QUIRK — single-step can go *backward*.** The `dir` (`esi`) argument's three-way
> split — `dir==0` / `dir<0` / `dir>0`, on a `cmp %r12d` after the commit — gives
> `dll_cycle_advance` a **rollback** path: a debugger can step the pipeline in
> *reverse*, un-committing the most recent writes from the `state+0x8f8` array and
> rotating the 32-deep ring the other way. This is the structural reason the model
> is **checkpointable**: the ring is the only in-flight state, and it is reversible
> within the interlock window. A reimplementer building a reverse-debug ISS gets
> the mechanism for free; the exact reverse-commit rule across the ~16 KB body is
> the one MED in this section. `[HIGH presence; MED for the exact rollback rule]`

### 2.3 Stage squash — the misprediction / fault replay primitive

The companion to forward/backward stepping is **stage squash**: `dll_kill_stage
@0x17ae3e0 (from_stage esi, to_stage edx)` flushes a stage range, `memset`ting the
per-stage records and clearing in-flight bits. This is what a debugger (or the
model itself) calls on a mispredicted branch, a taken exception, or a replay — it
discards the speculative stages between two ring positions. Combined with the
reversible tick, the cas ISS gives a debugger the full step / step-back / squash
triad over the 32-deep ring. `[HIGH presence; MED for the exact stage-range arithmetic]`

---

## 3. The introspection ↔ value seam — peek the value oracle, not the timing core

A debugger that wants the *value* of a register — not just its scoreboard slot —
reads it from the **fiss** side, because cas holds only scheduling tokens. The two
introspection halves are complementary:

- **cas introspection** (§1–2) reads the **timing** state: which stage is live,
  what is pending in the scoreboard, whether a step occurred. The values in
  `state+0x8f8` are the *committed-write tokens* the host produced, copied in by
  `dll_cycle_advance`; cas never computed them.
- **fiss introspection** reads the **value** state: the `regload__`-gathered
  operand blocks and the `writeback__`-committed result slots are the actual
  element data, computed in-process by the 864 `module__xdref_*` leaves
  ([the datapath oracle](./fiss-datapath-oracle.md)).

So a faithful debugger peeks **both** cores: cas for *when* a register is valid
(its forwarding window, [timing model](./cas-timing-model.md) §4) and fiss for
*what* it holds. The seam between them is the 119 `nx_*_interface` value ports
([core surface](./cas-core-surface.md) §3) — and a host that fills that seam with
the fiss oracle gets a single core whose timing and value introspection agree
cycle-for-cycle. `[HIGH/CARRIED]`

---

## 4. The fault model — fiss's 61-handler exception surface is the fault-injection path

A golden oracle must model **faults** as faithfully as values: a reimplementer
needs to know not just what `LSNX16` *computes* but what it *traps on*. The fault
model lives on the **fiss** side as the `exception__` family — 61 handlers + the
`exception_fns` dispatch accessor — which
[fiss surface + exceptions](./fiss-surface-exceptions.md) §3 enumerates. Here we
read it as the ISS's **fault-injection / fault-observation** path.

### 4.1 The 61 handlers are self-contained predicate evaluators

`nm -D libfiss-base.so | rg -c 'exception__'` = **61**.
Each handler is a ~0x4df-byte self-contained instruction-word **predicate
evaluator** (no external calls): it decodes the live FLIX bundle and decides
whether *this* fault applies to the issuing instruction. The roster spans the full
Xtensa+extension fault set — fetch/decode (`IllegalInstruction`,
`InstFetchProhibited`, `InstTLBMultiHit`), load/store
(`LoadStoreAlignment`, `LoadStoreSGAccError`, `ExclusiveError`), arithmetic
(`IntegerDivideByZero`, `ImpreciseSGFPVec`), windowed-ABI
(`WindowOverflow8`/`Underflow8`), coprocessor (`Coprocessor0..6`), stack-limit
(`KSL`/`ISL`StackLimitViolation), and the **debug / single-step** group:
`SingleStepException`, `BreakException`, `IBreakException`, `DBreakException`,
`MaybeOCDBreakException`, `OCDInterrupt`, `HaltException`. `[HIGH/OBSERVED count; roster CARRIED from the fiss surface page]`

### 4.2 The fault path as an oracle observable

For oracle use the fault model is the third observable, alongside value and timing:

- **Fault injection** — a reimplementer drives a known-faulting bundle (a
  misaligned `L32I`, a divide-by-zero, a windowed call past the stack limit) and
  the fiss exception handler **predicate** fires, delivering through the modeled
  `EXCCAUSE`/`EXCVADDR`/`EPC`/`PS` special registers (the SR machinery the same
  page §3 decodes). The cas side surfaces the same faults through its
  `dll_exception_table` (61 `*_exc` handlers, the §1.1 introspection table) and the
  `nx_Exception{Cause,PC,VAddr,Vector}` ports.
- **Fault observation** — because the handler is a pure predicate over the bundle +
  architectural state, it is **deterministic and replayable**: the same bundle +
  state always yields the same fault decision, so a differential test can assert
  the *fault behaviour* of a reimplementation against the oracle exactly as it
  asserts values. The single-step / break / OCD handlers (§4.1) are precisely the
  debug events the §2 single-stepper raises. `[HIGH/CARRIED handler structure; INFERRED oracle-observable framing]`

> **NOTE — `libfiss.h` caps exceptions at `MAX_POSSIBLE_EXCEPTIONS 64`.** The
> 61-handler roster sits one class below the header bound (and matches the cas
> `dll_exception_table`'s 61-handler / 123-reloc count), so the fault model fits the
> spec's slot budget with three to spare — a reimplementer who allocates a 64-entry
> fault-dispatch table matches the binary exactly. `[HIGH/OBSERVED count; header is
> corroboration.]`

---

## 5. SystemC / harness integration — `disasm_from_pc`, `TRACER`/TRAX, and the SystemC verdict

The cas core is *plugged into* a harness, and a reimplementer needs the harness
seam: the disassembler that renders a PC to text, the trace subsystem, and whether
a SystemC TLM wrapper exists to graft the ISS into a larger system model.
**The harness is `libsimxtcore.so`, not `libcas-core`** — the flat cas core holds
no disassembler and no trace writer.

### 5.1 The harness disassembler — `XTCORE_MEM::disasm_from_pc`

`nm -D -C libsimxtcore.so` resolves the harness disassembler surface:

```
XTCORE_MEM::disasm_from_pc(XTCORE_BASE*, std::ostream&, unsigned, unsigned, unsigned)        @0xed140
XTCORE_MEM::disasm_from_fmt_buff(XTCORE_BASE*, std::ostream&, unsigned*, unsigned, ...)       @0xecd60
XTCORE_MEM::disasm_from_slot_buff(XTCORE_BASE*, std::ostream&, unsigned*, ...)                @0xec890
CycleCore::disasm_from_di(std::ostream&, DECODED_INSTRUCTION*, unsigned, unsigned, unsigned)  @0x15d2c0
```

`XTCORE_MEM::disasm_from_pc` is the entry point: given a core and a PC it renders
the bundle at that PC to an `ostream`. The `_fmt_buff` / `_slot_buff` siblings
render a raw bundle / a single FLIX slot, and `CycleCore::disasm_from_di` renders
an already-decoded instruction. **There is no `xt_iss_disassemble`** — a debugger
that wants disassembly calls `disasm_from_pc`, not a flat C entry.

### 5.2 The trace subsystem — `TRACER` + TRAX + the 2 TraceBus ports

`libcas-core` itself carries **no trace writer**: its only trace coupling is the
**`state+0x5a8` bit0 event-trace flag** (set by `dll_enable_events @0x17b57f0`,
checked by `dll_cycle_advance` before firing the harness trace hook) plus exactly
**two undefined TraceBus imports** — `nx_RSRTraceBus_interface` and
`nx_WSRTraceBus_interface` (the SR/ER register-bus trace passthrough). The harness
*defines* both (as `CycleCore::RSRTraceBus_interface` / `WSRTraceBus_interface`),
so the trace seam is exact: the core raises the event through a single flag + two
ports, the harness writes the record.

The trace machinery itself lives in `libsimxtcore.so`:

- **`TRACER`** — the writer class (ctors/dtor `@~0x1de1f0`), threaded through every
  core block (`MMU(CycleCore*, TRACER*, …)`, `IntrControl(CycleCore*, TRACER*, …)`,
  `TRACE_block(unsigned, TRACER*)`).
- **TRAX** — the `libdb_ereg_trax` data symbol (`@0x49f680`) is the TRAX trace-buffer
  hook; the trace *port* geometry is config (`ncore2gp-params`: `TracePort = 1`,
  `TRAX = 1`, `TracePortMemBytes = 8192`) — an 8 KB TRAX buffer the harness's
  `TRACER` drives, not the core.

> **NOTE — `CycleCore` is the harness class, not a cas symbol.** Everything
> introspection-shaped that *looks* like it should live inside the cycle core —
> `disasm_from_di`, the `TraceBus` definitions, the `TRACER` plumbing — is a
> **`libsimxtcore.so`** C++ class (`CycleCore`), the harness wrapper above the flat
> 24-accessor `dll_*` C core. The dlopen-boundary ABI is still the flat C
> vocabulary; `CycleCore` is the driver that *uses* it and supplies the two
> TraceBus ports the core imports.

### 5.3 SystemC / TLM verdict — ABSENT

> **GOTCHA — there is NO SystemC / TLM wrapper anywhere in the ISS.** A reimplementer
> hoping to graft the GPSIMD ISS into a SystemC system model via a TLM-2.0 socket
> will not find one in the shipped toolchain. Checked across **all three** binaries
> — `nm -D -C` and a raw `rg` for `sc_core::` / `sc_module` / `tlm::` /
> `tlm_` — the count is **0 / 0 / 0**: `libcas-core` = 0, `libfiss-base` = 0,
> `libsimxtcore` = 0. The one apparent string hit in `libsimxtcore`
> (`SystemClockPeriod…`) is a **substring false positive** (it is a clock-period
> config token, not `SystemC`), as are the `xtsc_*` / `*desc_control` `nm`
> near-matches (substring `sc_`/`tlm_`, not the namespaces). The ISS integrates
> through the **flat C `dll_*` ABI + the harness `CycleCore`/`TRACER` C++ classes +
> the 119 `nx_*_interface` ports**, *not* through SystemC TLM sockets. A reimplementer
> wires the ISS into a host system model through those ports and the
> `XTCORE_MEM`/`peek_phys`/`poke_phys` memory surface, never through a `sc_module`.
> Do **not** invent a TLM wrapper.

---

## 6. Oracle synthesis — the cas + fiss ISS *is* the executable golden oracle

This is the closing thesis of Part 14, and of the whole `iss/` chapter. Stated
plainly: **a reimplementer never has to *infer* GPSIMD behaviour — they can
*execute* the reference and diff against it, bit-for-bit, in value, timing, and
fault.** The nineteen siblings each proved one face of this; here they fuse.

### 6.1 The oracle, in five facts

The oracle is a **two-library co-simulator on a shared roster**:

| Fact | Value | Why it makes the ISS an oracle |
|---|---:|---|
| cas / fiss split | decode+timing **\|** value | cas answers *when*; fiss answers *what*; the split is the whole architecture ([core surface](./cas-core-surface.md), [datapath oracle](./fiss-datapath-oracle.md)) |
| cas value-delegation ports | **119** `nx_*_interface` | the value seam — cas computes **0** element values (0 packed-arith in 45 MB); a host fills these with fiss |
| fiss value leaves | **864** `module__xdref_*` | the executable per-lane element-math primitives — the golden value model |
| fiss decode leaves | **12,569** `slotfill__` | pure bitfield decoders, inverse of the libisa encode — the decode half of the oracle |
| fiss self-containment | **0** host callbacks | the value oracle is a free-standing in-process interpreter — drivable directly via `ctypes` |

Plus the four supporting counts: fiss **20,379** dynsym
exports (the largest toolchain artifact), the TIE-DB **864 xdref** source the
leaves were generated from (cas carries the dual **68 xdsem** + **0 xdref**), the
**61** `exception__` fault handlers (§4), and the **157,775** cas `_inst_stage`
per-(format,slot,mnemonic) schedule thunks.

### 6.2 Why the reference *is* golden — ref == production, value-identical

The toolchain ships `-ref-` twins (`libcas-ref-core.so`, `libfiss-ref-base.so`)
that [ref-vs-production diff](./ref-vs-production-diff.md) certifies are
**value-identical** to production: the fiss-ref leaf bodies are byte-identical at
exact `nm` size, the two cas variants share the same ABI version (`0x1381f`) and
modeled config, and a 71,706-call live `ctypes` battery found **0** mismatches
between the production and `-ref-` value oracles. The consequence for oracle use is
decisive: **there is no "more correct" reference hiding behind production** — the
production fiss leaf a reimplementer drives *is* the golden value, and the `-ref-`
twin would return the identical answer. The oracle is singular. `[HIGH/CARRIED]`

### 6.3 The certification methodology — drive the real leaf live

Because `libfiss-base.so` is a self-contained, in-process, integer-only reference
interpreter (0 host callbacks, 0 hardware FP), **every value claim in Part 14 is
certifiable by `ctypes`**: `dlopen` the oracle, declare the leaf's ABI, call it on
a known input, and the answer it returns *is* the certified golden value. The
siblings each drove a leaf this way —
[the semantic synthesis](./iss-semantic-synthesis.md) drove `avgr_16_16_16` (and
the `add`/`adds` wrap-vs-saturate pair), [fiss datapath](./fiss-datapath-oracle.md)
the `mula_48_48_16_16` widening MAC, [ref-vs-prod](./ref-vs-production-diff.md) a
71,706-call cross-variant battery. To certify *this* page independently, it drives
a **fresh** leaf no sibling touched, and one with the **unary** ABI shape (3 args,
not the binary 4-arg shape).

#### A fresh leaf, driven live — `module__xdref_nsau_16_16`

`module__xdref_nsau_16_16` (unsigned **normalize-shift-amount** = leading-zero
count of a 16-bit value) is the natural oracle for an introspection chapter: it is
the bit-observability primitive. The body, read first:

```asm
module__xdref_nsau_16_16  @0x8585a0 :
  test %esi,%esi ; jne +              ; A == 0 ?
  mov  $0x10,%eax ; mov %eax,(%rdx) ; ret   ; A==0 -> 16 (all leading zeros)
+ mov  $0xf,%eax ; bsr %esi,%esi      ; bsr = index of highest set bit (0..15)
  sub  %esi,%eax ; mov %eax,(%rdx) ; ret    ; result = 15 - bsr = leading-zero count
```

The ABI is the **unary** shape: A in `%esi` (SysV arg2), result-pointer in `%rdx`
(SysV arg3) — so the C-callable signature is **three** args with **arg 0 (`%rdi`)
dead**. Driven via `ctypes`:

```python
import ctypes
lib = ctypes.CDLL(".../ncore2gp/config/libfiss-base.so")
f = lib.module__xdref_nsau_16_16
f.restype, f.argtypes = None, [ctypes.c_void_p, ctypes.c_int32,
                               ctypes.POINTER(ctypes.c_uint32)]
out = ctypes.c_uint32()
f(None, 0x0100, ctypes.byref(out))   # arg0 ignored; A=0x0100, result ptr
# out.value == 7    (leading-zero count of 0x0100 in 16 bits)  ✓
```

Live results, all **byte-exact** against the read-from-byte expectation
(`16` for zero, else `15 − bsr`):

| `nsau(a)` | got | expect | | `nsau(a)` | got | expect |
|---|---:|---:|---|---|---:|---:|
| `nsau(0x0000)` | 16 | 16 | | `nsau(0x0100)` | 7 | 7 |
| `nsau(0x0001)` | 15 | 15 | | `nsau(0x4000)` | 1 | 1 |
| `nsau(0x0008)` | 12 | 12 | | `nsau(0x8000)` | 0 | 0 |
| `nsau(0x00ff)` | 8 | 8 | | `nsau(0xffff)` | 0 | 0 |

The fresh leaf is certified: `nsau_16_16` is the 16-bit leading-zero count
(`0 → 16`), executed live, byte-exact. The oracle is executable for the unary ABI
shape as well as the binary one.

> **GOTCHA — the unary value-leaf ABI is 3-arg with a dead `%rdi`.** The leaf reads
> A from `%esi` and writes through `%rdx` — SysV integer registers **2** and **3**.
> A `ctypes` caller that declares the intuitive 2-arg signature `(int32 a, uint32*
> res)` passes A in `%rdi` and the pointer in `%rsi` — the leaf then reads garbage
> from `%esi` and writes through whatever is in `%rdx`, so the result slot stays
> **unwritten** (it keeps its sentinel `0xbeef`). The control run here confirmed it:
> the wrong 2-arg signature left `0xbeef` in the slot. The correct signature is
> **three** args `(c_void_p ignored, int32 a, uint32* res)`. (The binary leaves are
> the 4-arg analogue — A=`%esi`, B=`%edx`, ptr=`%rcx`, arg0 dead, per
> [the semantic synthesis](./iss-semantic-synthesis.md) §6.2.) This single ABI
> detail is the difference between a certified oracle and a silent no-op.

### 6.4 How a reimplementer uses the ISS as the differential reference

The oracle is not documentation — it is a **test fixture**. A reimplementer
building a Vision-Q7-compatible GPSIMD engine drives the ISS as the golden
differential reference on all three axes: `[HIGH/INFERRED]`

1. **Values** — for any instruction, `dlopen` `libfiss-base.so`, drive the matching
   `opcode__<m>__stage_{5,14}` → `module__xdref_<op>_<W>` leaf on a fuzzed input
   set, and assert the reimplementation's per-lane result equals the oracle's
   bit-for-bit (§6.3; the 864 leaves are the full value surface).
2. **Timing** — drive `libcas-core.so` through the `dll_initialize` →
   `dll_cycle_advance` loop (§2), reading the scoreboard / ring head (§1) each
   tick, and assert the reimplementation's per-op latency and hazard stalls match
   the cas model (the per-unit LAT table, [timing model](./cas-timing-model.md) §4).
3. **Faults** — drive a known-faulting bundle and assert the reimplementation
   raises the same `exception__` the fiss predicate fires (§4), through the same
   `EXCCAUSE`/`EXCVADDR` delivery.
4. **Trace / disasm** — render reference bundles through
   `XTCORE_MEM::disasm_from_pc` (§5.1) and diff against the reimplementation's
   disassembler; capture the reference's `TRACER`/TRAX event stream as the golden
   trace.

The seam a reimplementer must fill is always the **119 `nx_*_interface` ports**
([core surface](./cas-core-surface.md) §3.1): wire them to the fiss oracle and the
two cores become one, whose timing, value, fault, and trace introspection all agree.

> **The reimplementation takeaway.** The GPSIMD Vision-Q7 ISS is the **executable
> golden oracle**: cas for cycle-accurate timing, fiss for bit-exact values, the 61
> `exception__` handlers for faults, and the `libsimxtcore` harness
> (`disasm_from_pc` + `TRACER`/TRAX) for trace — all drivable in-process, all
> value-identical to their `-ref-` twins, none hidden behind a SystemC wrapper that
> does not exist. You do not reverse-engineer GPSIMD semantics from a spec; you
> **run the reference and diff**. The next Part — *Driving the ISS as a Golden
> Oracle* (the validation material, referenced by title; that Part consumes this
> oracle) — builds the differential harness around exactly this surface.

---

## 7. Confidence ledger & open items

**HIGH / OBSERVED:** the
`0x4a09f0 = 4,852,208 B` cas state size (disasm immediate `b8 f0 09 4a 00` +
executed `ctypes` return, two witnesses); the first-registered register `"AR"` as
the `.rodata 0x17cdb53` suffix-merged tail of `"REV8AR\0"`; `dll_cycle_advance`
setting `state+0x5a8` bit3 (`orb $0x8` @0x17aa3db) and `dll_reset_cycle_advanced`
clearing it (`andb $0xf7` @0x17aa3b0); the SystemC/TLM **0/0/0** absence across all
three binaries and the `SystemClockPeriod` false-positive; the `XTCORE_MEM::disasm_from_pc`
disassembler, the `TRACER`/TRAX subsystem, and the 2 cas-import / simx-export
TraceBus ports; the counts (119 / 0 / 864 / 12,569 / 61 / 20,379 /
157,775); the fresh `nsau_16_16` leaf driven live byte-exact + the dead-`%rdi`
control.

**HIGH / CARRIED (proven at a sibling, re-used here):** the cas/fiss two-library
split and the value seam; the fiss 61-handler fault roster and SR delivery; the
ref==production value-identity (the singular oracle); the `(format, slot, regfile,
value)` closure; the TIE provenance of the xdref/xdsem leaves.

**MED / INFERRED:** the exact `dll_cycle_advance` rollback (`dir<0`) reverse-commit
rule (presence HIGH, the rule across ~16 KB not fully traced); the `dll_kill_stage`
exact stage-range arithmetic; the fault-injection-as-oracle-observable framing (the
predicate determinism is OBSERVED; the differential-test workflow is the
reimplementation recipe).

**LOW / open (none material to the oracle):** the inter-region byte spans of the
≈4.63 MB block; the cas dispatch role labels (`esi` 1=def-post vs 3=set_def); the
Newton-seed (`recip0`/`rsqrt0`/…) polynomial coefficients (named SEED leaves,
bodies not disassembled); the reduce hardware lane-pairing tree (the oracle uses a
flat fold, value-equivalent by associativity).

> **CORRECTION — the cas per-instance state is 4,852,208 B (`0x4a09f0`, ≈4.63 MB),
> not the off-by-1,024 figure.** A decimal carried across the early ISS reports
> was `0x4a05f0` (a 9↔5 nibble slip) — exactly **1,024 bytes** below the value
> `dll_get_data_size` actually loads. The disassembly is unambiguous
> (`1776570: b8 f0 09 4a 00  mov $0x4a09f0,%eax`), `0x4a09f0 = 4,852,208`, and the
> live `ctypes` return is `4,852,208` — two independent witnesses. A reimplementer
> who sizes the instance buffer at `0x4a05f0` is **1 KB short** of what
> `dll_initialize` will `memset`, corrupting the last page of the block. The exact
> byte count must read **4,852,208 B (0x4a09f0, ≈4.63 MB)**. This is the single
> strongest correction of this page.

With this page Part 14 is closed: the GPSIMD Vision-Q7 ISS is the **executable
golden oracle** — a differential reference for value, timing, fault, and trace,
drivable in-process via the flat `dll_*` ABI and the fiss value leaves, with no
SystemC wrapper present. Every claim across the chapter is certifiable by running
the reference, and §6.3 ran one fresh leaf live to prove it.
