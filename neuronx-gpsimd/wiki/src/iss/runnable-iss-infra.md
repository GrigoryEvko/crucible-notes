# Running the ISS — Invocation / Config / Memory / Trace / Perf

> **The operational manual for the oracle.** The sibling pages in this Part
> describe *what the ISS computes*: [cas Core Surface](./cas-core-surface.md)
> fixes the plugin ABI, [The cas Timing Model](./cas-timing-model.md) the
> cycle/hazard math, [fiss Surface + Exceptions](./fiss-surface-exceptions.md)
> the value oracle, [libtie-core + msem](./libtie-core-msem.md) the TIE database
> the ISS was generated from, and [libctype — CSTUB](./libctype-cstub.md) the
> host value-marshalling floor. **This page describes how you actually *run* it.**
> It documents the `dll_*` lifecycle ABI as a runnable init→step→teardown loop,
> the `ncore2gp-params` configuration surface that wires the six DLLs together,
> the ≈4.63 MB per-instance memory model, and the trace/perf machinery. The
> capstone that fuses timing + values into one re-runnable reference is
> [The ISS as Executable Oracle](./iss-oracle-synthesis.md).

All facts below are derived from static analysis of the shipped binaries only,
plus a **live `ctypes` drive of `libcas-core.so`** that certifies the ABI
(§1.4). Each claim is tagged **confidence** × **provenance** (`OBSERVED` = read
directly off the binary / executed, `INFERRED` = strong deduction from
observed evidence, `CARRIED` = taken from a prior page). The two
recovered headers cited (`iss/libfiss.h`, the `ncore2gp-params` config) are
treated as binary-adjacent artifacts: they ship in the same toolchain and every
constant is corroborated against a binary at the point of use.

Binaries, by absolute role (paths under
`extracted/nested/gpsimd_tools_tgz/tools/`):

```
ncore2gp/config/libcas-core.so            45,878,080 B   the ISS CORE  (decode + timing, this lifecycle)
ncore2gp/config/libfiss-base.so           12,330,016 B   the VALUE oracle (answers the 119 ports)
ncore2gp/config/libctype.so                  388,648 B   host value marshalling (loadi/storei/rtor)
ncore2gp/config/libtie-core.so            51,098,208 B   xml-base provider (the TIE DB)
ncore2gp/config/libtie-Xtensa-msem.so        258,120 B   xml-msem provider (memory/control)
XtensaTools/lib/iss/libsimxtcore.so        2,804,296 B   the cycle-ISS HARNESS (the runnable driver)
XtensaTools/lib/tc/nativesim               1,199,824 B   the native-TIE-sim DRIVER (references all of the above)
```

`.text`/`.rodata` are VMA==file-offset in every DLL above; the writable sections
carry the ncore2gp delta **`+0x200000`** (subtract before `xxd` on `.data`/
`.data.rel.ro`). `libcas-core` / `libctype` ship a `.symtab` but **no
`.debug_*`** — every offset below is pinned from the `mov`-displacement encoding,
not DWARF. `libsimxtcore.so` / `nativesim` are stripped of `.symtab`
(`libsimxtcore` keeps `.dynsym`; `nativesim` is fully stripped).

---

## 0. The run model in one paragraph

The runnable ISS is **a harness (`libsimxtcore.so` / `nativesim`) plus six
config DLLs**. The harness `malloc`s one `dll_get_data_size()` block per
simulated core, `dll_initialize`s it (which installs the harness's own
function-pointer vtable *into* the state and registers every architectural state
register back through it), wires the value path (`libfiss-base` answering the
119 `nx_*_interface` ports, `libctype` marshalling typed values across the
host↔target memory window), loads the program and data **images** through the
four `get_xml_*` providers and the RAM-init parameters, then drives the pipeline
**one tick per `dll_cycle_advance` call**, reading per-(format,slot,mnemonic)
issue/stall/stage thunks each cycle, until teardown. There is **no `main`, no
`register_client` factory, no getInterface** — the contract is a flat C
vocabulary of 24 `dll_*` accessors fixed by `libfiss.h` (`LIBFISS_VERSION 2`).
---

## 1. The `dll_*` lifecycle ABI

### 1.1 The 24-accessor vocabulary, grouped by lifecycle phase

`nm libcas-core.so | rg -c ' [Tt] dll_'` = **24** — the *entire* plugin entry
surface (the other 1087 `T` exports are `opnd_sem_*` resolvers and `my_*`
scoreboard accessors, never called by name across the dlopen boundary). Every
accessor takes the per-instance state pointer in `%rdi` (arg0). Grouped by *when
the harness calls them*:

| Phase | Accessor | Addr | Action |
|---|---|---|---|
| **identity** (pre-alloc) | `dll_get_version` | `0x1776580` | `mov $0x1381f,%eax;ret` — ABI/build stamp |
| | `dll_get_data_size` | `0x1776570` | `mov $0x4a09f0,%eax;ret` — bytes to `malloc` (§3 / §4.1) |
| **init** | `dll_initialize` | `0x17b5c90` | `memset(state,0,0x4a09f0)`, install harness vtable, register state (§1.2) |
| | `dll_init_tieport_outputs` | `0x17b5c80` | reset the TIE output ports to a known state `[MED · INFERRED]` |
| | `dll_reset_states` | `0x17b5810` | reset architectural register state `[MED · INFERRED]` |
| **table publish** (read once) | `dll_get_stage_functions` | `0x17aa360` | `&slot_semantic_functions` (`0x227ecc0`) |
| | `dll_get_issue_functions` | `0x17aa340` | `&slot_issue_functions` (`0x227f2c0`) |
| | `dll_get_stall_functions` | `0x17aa2e0` | `&slot_stall_functions` (`0x227f8c0`) |
| | `dll_get_shared_functions` | `0x17aa3a0` | `&dll_shared_functions` (`.bss`, harness-set) |
| | `dll_get_exception_table` | `0x17aa2d0` | `&dll_exception_table` (1008 B, 61 handlers) |
| | `dll_get_state_table` | `0x17aa370` | `&dll_state_table` (`0x221a3c0`, regfile-offset oracle) |
| | `dll_get_regfile_table` | `0x17aa380` | `&dll_regfile_table` (`0x227eac0`, 63 slots) |
| | `dll_get_ext_regfile_table` | `0x17aa390` | `&dll_ext_regfile_table` (`0x17d0740`, all-zero → **no aux regfiles**) |
| | `dll_get_queue_info` | `0x17b57d0` | `&dll_queue_info` |
| | `dll_get_lookup_info` | `0x17b57e0` | `&dll_lookup_info` |
| **decode** (per fetch) | `dll_instruction_get_length` | `0x17b57a0` | `CSWTCH.4667[op0]` → byte length (§1.3) |
| | `dll_instruction_speculate_length` | `0x17b57b0` | pre-byte3 speculative length |
| **step** (per cycle) | `dll_cycle_advance` | `0x17aa3c0` | **the one-tick stepper** (§2) |
| | `dll_reset_cycle_advanced` | `0x17aa3b0` | `andb $0xf7,0x5a8(%rdi);ret` — clear flags bit3 |
| | `dll_kill_stage` | `0x17ae3e0` | squash a pipeline-stage range (misprediction/replay) |
| | `dll_export_state_stall` | `0x1795ba0` | stall hook for state export/checkpoint `[MED · INFERRED]` |
| | `dll_wait_for_tie_io` | `0x17aa350` | `xor %eax,%eax;ret` — **no-op stub** (never blocks) |
| **trace/mode** | `dll_enable_events` | `0x17b57f0` | toggle flags bit0 (event trace) at `0x5a8(%rdi)` |
| | `dll_set_tie_stall_eval` | `0x17aa2f0` | set/clear flags bits 1–2 (stall-eval mode) at `0x5a8(%rdi)` |

> **GOTCHA — `dll_wait_for_tie_io` is `xor eax,eax; ret`.** The cycle core never
> blocks on TIE I/O. Every port read is assumed same-tick resolvable because the
> *value* comes from the host vtable (`libfiss-base`), not from a modeled bus. A
> reimplementer must NOT model a TIE-bus stall here — there is no datapath inside
> cas that could produce a result to wait on.
### 1.2 `dll_initialize` — the install-and-register prologue

`dll_initialize @0x17b5c90 (rdi=state, rsi=harness_handle, rdx=init_ctx)`. The
prologue does three structural things, each read verbatim from the disassembly:
```c
// dll_initialize @0x17b5c90  (annotated from disasm)
void dll_initialize(state *s, void *harness_handle, init_ctx *ctx) {
    // (1) ZERO the entire per-instance block — EXACTLY dll_get_data_size() bytes
    memset(s, 0, 0x4a09f0);                  // mov $0x4a09f0,%edx ; call memset@plt
    s->harness_handle_0x0 = harness_handle;  // mov %rbp,(%rbx)        state+0x00
    s->vtable_base_0x8    = ctx->vtable_base;// mov (%r12),%rax ; mov %rax,0x8(%rbx)

    // (2) INSTALL the harness shared-helper vtable into state+0x10..
    //     rep movsq copies ctx[..] into &s[0x10]; length derived from 0x5a0/8
    rep_movsq(&s[0x10], ctx, /*qwords*/ (0x5a0 + ...) >> 3);  // f3 48 a5
    s->control_word_0x5a0 = ctx->word_0x598; // mov 0x598(%r12),%rax ; mov %rax,0x5a0(%rbx)

    // (3) REGISTER every architectural state register back through the vtable.
    //     core-calls-harness: harness->register(handle, name, &slot)
    harness->register(handle, "AR",  &s->reg_slot_0x14eb0);  // call *0x10(%rbx)
    harness->register(handle, name2, &s->reg_slot_0x14eb8);  // call *0x28(%rbx)
    harness->register(handle, name3, &s->reg_slot_0x15398);  // call *0x28(%rbx)
    ... // one per architectural state register, alternating vtable slots
}
```

The shape is **core-calls-harness**, the inverse of a factory: the harness hands
the core *its* function table (`rep movsq` into `state+0x10`), and the core walks
every architectural register and calls **back** through that table
(`call *0x10(%rbx)`, `call *0x28(%rbx)`) passing the harness handle, a name
string from `.rodata`, and the address of the matching state slot inside the
block. This is the "register" half of dlopen/register/run. The reg-slot region
begins at **`state+0x14eb0`** (`lea 0x14eb0(%rbx),%rdx`).

> **CORRECTION — the first registered state register is `"AR"`, not `"REV8AR"`.**
> The sibling [cas Core Surface](./cas-core-surface.md) reads the first
> registration name as `"REV8AR"`. The `lea` in `dll_initialize` actually targets
> `.rodata 0x17cdb53`, and the null-terminated string *at that displacement* is
> `"AR\0"` — the windowed scalar register file (`num_aregs = 64` in the config).
> `"REV8AR"` is the pooled string starting four bytes earlier at `0x17cdb4f`
> (`52 45 56 38 41 52 00` = `REV8AR\0`); the compiler **suffix-merged** the
> `"AR"` literal into the tail of `"REV8AR"`, so the registration `lea` points at
> the `"AR"` substring, not the head. Confirm: `xxd -s 0x17cdb4f` shows
> `REV8AR\0`; the `lea` in the disasm reads `# 17cdb53`, i.e. byte offset +4 =
> `"AR"`. The first architectural register the ISS publishes is the scalar AR
> file.

### 1.3 `dll_instruction_get_length` — the decode that begins every cycle

The fetch width that starts the cycle accounting is a three-instruction table
lookup. Its signature is `(rdi=unused, esi=op0_byte)` — **the index is arg1**,
not arg0:
```text
dll_instruction_get_length @0x17b57a0 :
  lea  CSWTCH.4667(%rip),%rax    ; 256-entry signed-byte table @0x17d0640 (.rodata)
  movzbl %sil,%esi               ; opcode byte = ARG1 (rsi), zero-extended
  movsbl (%rax,%rsi,1),%eax      ; return (int8) table[op0]
  ret
```

The table repeats every 16 bytes, so the **low nibble** of the opcode byte
selects the length. Byte-exact (`xxd -s 0x17d0640`):

| op0 low nibble | length | meaning |
|---|---|---|
| `0x0..0x7` | **3** | base 24-bit (x24) density |
| `0x8..0xD` | **2** | 16-bit (x16a/x16b) density |
| `0xE` | **16** (`0x10`) | wide FLIX bundle F0..F11 |
| `0xF` | **8** normally / **16** on a wide byte3-selector / **−1 (illegal)** in two rows |

`dll_instruction_speculate_length @0x17b57b0` uses the parallel 16×int32 table
`@0x17d0600` (`{0..7:3, 8..D:2, E/F:0}`) for pre-byte3 PC advance and
branch-target sizing.
### 1.4 LIVE CERTIFICATION — driving the ABI through `ctypes`

The lifecycle is not inferred from names — the identity and decode accessors were
**executed**. Because the core's only non-`memset` imports are the 119
`nx_*_interface` host ports (used at the execute stage), an eager `dlopen` fails;
binding **lazily** (`RTLD_LAZY`) resolves the leaf accessors while leaving the
ports unbound — which *itself certifies* the 119-port host-seam claim:

```python
import ctypes
CAS = ".../ncore2gp/config/libcas-core.so"
# eager dlopen FAILS: "undefined symbol: nx_VAddrBaseAlternate_0_interface"
#   -> proves the core cannot run standalone; the harness must supply the ports.
lib = ctypes.CDLL(CAS, mode=0x0001)            # RTLD_LAZY -> binds only what we touch

lib.dll_get_version.restype   = ctypes.c_int
lib.dll_get_data_size.restype = ctypes.c_uint
lib.dll_instruction_get_length.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.dll_instruction_get_length.restype  = ctypes.c_int

lib.dll_get_version()        # -> 0x1381f
lib.dll_get_data_size()      # -> 4852208  ( == 0x4a09f0 )
[lib.dll_instruction_get_length(0, b) for b in range(16)]
#   -> [3,3,3,3,3,3,3,3, 2,2,2,2,2,2, 16, 8]   (low-nibble decode, EXACT)
lib.dll_instruction_get_length(0, 0xFF)   # -> -1   (illegal row)
```

Observed live results (all match the disassembly): `dll_get_version → 0x1381f`,
`dll_get_data_size → 4,852,208`, the full low-nibble decode row
`[3×8, 2×6, 16, 8]`, and `0xFF → −1`. `[HIGH · OBSERVED — executed.]`

> **CORRECTION — `dll_get_data_size` is 4,852,208 bytes (`0x4a09f0`, ≈4.63 MB).**
> The binary literally encodes `b8 f0 09 4a 00` = `mov $0x4a09f0,%eax`, and
> `0x4a09f0 = 4,852,208`. An off-by-1,024 decimal carried across some early ISS
> drafts was `0x4a05f0` (a 9↔5 nibble slip) — exactly **1,024 B** below the real
> value. The live `ctypes` return is `4,852,208`, confirming the binary. Note the
> magnitude is **≈4.63 MB** (`4,852,208 / 1024² = 4.6274 MiB`; the decimal-MB
> reading `/1e6 = 4.8522 MB` is what echoes the slipped figure — prefer ≈4.63 MB).
> A reimplementer who sizes the instance buffer at `0x4a05f0` is
> **1 KB short** of what `dll_initialize` will `memset`, which would corrupt the
> last page of the block. `[HIGH · OBSERVED — disasm immediate + executed return,
> two independent witnesses.]`

### 1.5 The runnable loop in annotated C

Folding §1.1–§1.4 into the init→step→teardown skeleton a harness implements:
`[HIGH presence / MED exact harness orchestration — the `dll_*` calls are
OBSERVED; the surrounding harness flow is INFERRED from the ABI + nativesim
string refs]`

```c
// One simulated GPSIMD core, driven through the libcas-core dll_* ABI.
// h = libsimxtcore.so / nativesim (the harness). All dll_* are dlsym'd @@VERS_1.1.
void run_one_core(harness *h, const uint8_t *iram_image, const uint8_t *dram_image) {
    /* ---- identity / alloc ---- */
    if (dll_get_version() != 0x1381f) abort();          // ABI guard
    void *state = malloc(dll_get_data_size());          // EXACTLY 4,852,208 bytes

    /* ---- init: zero, install harness vtable, register state regs ---- */
    dll_initialize(state, h->handle, &h->init_ctx);     // memset + rep movsq + register("AR",...)
    dll_init_tieport_outputs(state);
    dll_reset_states(state);

    /* ---- publish the dispatch + descriptor tables (read once) ---- */
    h->issue  = dll_get_issue_functions(state);         // &slot_issue_functions
    h->stall  = dll_get_stall_functions(state);         // &slot_stall_functions
    h->stage  = dll_get_stage_functions(state);         // &slot_semantic_functions
    h->states = dll_get_state_table(state);             // regfile-offset oracle
    h->exc    = dll_get_exception_table(state);

    /* ---- wire the value path (the 119-port seam) ---- */
    fiss_bind_ports(h);                 // libfiss-base answers every nx_*_interface
    cstub_set_base_address(h->target_mem);              // libctype host<->target window
    cstub_ctypes_init();

    /* ---- load the program + data images (the four get_xml_* providers + RAM init) ---- */
    h->load_iram(state, iram_image);    // InstRAM  64KB @ 0x00000000  (init 0x6c6cb6b6)
    h->load_dram(state, dram_image);    // DataRAM  64KB @ 0x00080000  (init 0x01234567)

    /* ---- run: one tick per dll_cycle_advance, until halt ---- */
    dll_enable_events(state, h->trace_on);              // flags bit0
    while (!h->halted) {
        uint32_t pc   = h->pc;
        int      len  = dll_instruction_get_length(0, iram_image[pc]); // op0 nibble decode
        /* issue admit (stall chain) + scoreboard post per (fmt,slot,mnemonic) */
        run_issue_stall_stage_thunks(h, state, pc, len);
        dll_cycle_advance(state, /*dir=*/0);            // commit 4 WB lanes, rotate the ring
        dll_reset_cycle_advanced(state);                // clear flags bit3 for the next tick
        h->pc = h->next_pc;
    }

    /* ---- teardown: free the block; no dll_finalize exists ---- */
    free(state);                        // the block is self-contained; nothing else to release
}
```

> **NOTE — there is no `dll_finalize` / `dll_destroy`.** The 24-accessor surface
> has no teardown entry; teardown is `free(state)`. The per-instance block owns no
> external resources (the only heap inside it is whatever the harness allocator
> backs; `libctype`'s `rtor` node lists belong to the *type system*, not the core
> state).

---

## 2. The tick stepper — `dll_cycle_advance`

`dll_cycle_advance @0x17aa3c0 (rdi=state, esi=dir)` (~16 KB) is the single
function that turns wall time into pipeline progress. Decoded head + body:
```c
// dll_cycle_advance @0x17aa3c0  (annotated from disasm)
void dll_cycle_advance(state *s, int dir) {
    int enabled = s->cycle_adv_guard_0x24e4;       // mov 0x24e4(%rdi),%r9d
    s->flags_0x5a8 |= 0x8;                          // orb $0x8,0x5a8(%rdi)  set "cycle advanced"
    if (!enabled) return;                           // test %r9d,%r9d ; je out

    int head = s->ring_head_0x24ec;                 // mov 0x24ec(%rdi),%eax
    // (1) COMMIT pending writes: look-ahead (head + 12) & 0x1f, up to 4 WB lanes
    int la = (head + 0xc) & 0x1f;                    // add $0xc,%eax ; and $0x1f,%eax
    for (int lane = 0; lane < 4; lane++) {           // lanes @ +0x25f4/+0x28f4/+0x2bf4/+0x2ef4
        if (commit_flag[la][lane]) {                 // mov 0x25f4(%rbp),%r8d ; test
            int reg = reg_no[la][lane];              // movslq 0x2574(%rbp),%rax
            int val = value [la][lane];              // mov    0x24f4(%rbp),%edx
            s->scoreboard_0x8f8[reg] = val;          // add $0x8f8,%rax ; mov %edx,0x4(%rdi,%rax,4)
        }
    }
    // (2) DECREMENT the ring head: exactly ONE stage of advance per tick
    s->ring_head_0x24ec = (head - 1) & 0x1f;
    // (3) SHIFT the ring: every in-flight reservation column moves stage+1 -> stage
    // (4) dir(esi) {==0 / <0 / >0}: forward-commit vs checkpoint ROLLBACK
}
```

The pipeline is a **32-deep stage ring** indexed `& 0x1f` everywhere; the head at
`state+0x24ec` decrements by one per tick. The four write-back lanes
(`+0x25f4/+0x28f4/+0x2bf4/+0x2ef4`) land their committed values into the
`state+0x8f8` scoreboard/regfile array — the up-to-4 parallel writes a wide FLIX
bundle retires per cycle. The `dir` argument's three-way split makes the model
**reversible / checkpointable** (a debugger can step backward), consistent with a
debugger-driven cycle ISS. The deep mechanism (the latency-aware RAW scoreboard,
the 6-lane writeback drain, the per-op `mov $LAT,%esi` immediates) is the subject
of [The cas Timing Model](./cas-timing-model.md); this page documents only the
*driving* call. `[HIGH · OBSERVED head + commit/shift; MED rollback rule]`

> **QUIRK — no wall-clock frequency constant exists in `libcas-core`.** There is
> no MHz/period/nanosecond constant anywhere in the image; cas counts cycles
> *abstractly*. The cycle→ns conversion is a host concern, and it is **config
> data**, not code: `ncore2gp-params` carries `ImplTargetSpeed = 1111` (MHz) and
> the per-RAM latencies (§4.2). A reimplementer reads the frequency from the
> params file, never from the core.

---

## 3. Configuration surface — `ncore2gp-params` wires the six DLLs

The harness reads one text params file to discover every DLL by role. The
shipped `ncore2gp/config/ncore2gp-params` (auto-generated, "Do not edit") binds
the ISS exactly as the lifecycle expects:

```ini
ConfigName  = Xm_ncore2gp      arch = Xtensa24      uarchName = Cairo
TargetHWVersion = NX1.1.4      SWToolsRelease = RI-2022.9     HasVectorPipe = 1   IsaUseVision = 1

iss-base-dll      = .../ncore2gp/config/libcas-core.so        # the ISS CORE  (this page)
iss-ref-base-dll  = .../ncore2gp/config/libcas-ref-core.so    # the reference ISS (Part 15)
fiss-base-dll     = .../ncore2gp/config/libfiss-base.so       # the VALUE oracle
fiss-ref-base-dll = .../ncore2gp/config/libfiss-ref-base.so   # reference value oracle (Part 15)
ctype-base-dll    = .../ncore2gp/config/libctype.so           # host value marshalling
xml-base-dll      = .../ncore2gp/config/libtie-core.so        # TIE DB provider
xml-msem-dll      = .../ncore2gp/config/libtie-Xtensa-msem.so # memory/control provider
isa-base-dlls     = [ libisa-core-hw.so  libisa-core.so ]     # the static ISA tables
xml-tie-dll =        iss-tie-dll =        isa-tie-dll =        # the TIE roles, EMPTY in this config
```

So the run wires the **timing** core (`iss-base-dll`) to the **value** oracle
(`fiss-base-dll`) and the host **type codec** (`ctype-base-dll`), reads the ISA
tables from `isa-base-dlls`, and reads the bit-precise semantics from the two TIE
providers (`xml-base-dll` / `xml-msem-dll`). The three `*-tie-dll` roles are
present-but-empty (this config folds TIE into the base). The `*-ref-*` cores are
the cross-validation reference (Part 15).
### 3.1 The 4 image getters nativesim references

The keystone "four image getters" are the four `get_xml_*` provider accessors the
native sim resolves to pull each phase-serialization (the TIE-DB **image** blobs)
out of the `xml-base`/`xml-msem` providers. `strings -a nativesim | rg
'^get_xml_'`:
```
get_xml_post_parse        # the just-after-parse snapshot (50-B stub in this build)
get_xml_post_rewrite      # the folded, fully-elaborated DB — THE one that matters
get_xml_compiler          # compiler-directive side-table
get_xml_xinfo             # regfile-geometry quick-reference
```

`nativesim` additionally references the three provider role names
(`xml-base-dll`, `xml-msem-dll`, `xml-tie-dll`) and embeds the libctype
declarations (`cstub_loadi_function`, `cstub_set_base_address`,
`cstub_ftype_loadi`) and the TIE `interface_version` getter — i.e. it is the
driver that holds **all four config halves** (ISS core, value oracle, type codec,
TIE DB) together. The provider ABI itself (the five-getter `interface_version`/
`get_xml_*` contract, the +13 cipher on the blobs) is documented on
[libtie-core + msem](./libtie-core-msem.md).

> **CORRECTION — there is no symbol-visible IRAM/DRAM/IROM/DROM "image getter"
> quad.** It is tempting to expect four per-region memory-image getters
> (`getIramImage`/`getDramImage`/…). No such set exists as symbols: `nativesim`
> is fully stripped, and `libsimxtcore.so` (only `.dynsym` survives) exposes a
> *program-loading* surface instead — the single literal `elf_write_image`, the
> libelf-style ELF-segment accessor group (`elf_segment_data` / `elf_segment_vaddr`
> / `elf_segment_paddr` / `elf_segment_mem_size`), the higher-level
> `SYSTEM_BASE::load_bfd` / `load_dat`, and the genuine RAM peek/poke quad
> (`XTCORE_MEM::peek_phys`/`poke_phys`/`peek_virt`/`poke_virt`, `RAM::peek_u32`/
> `poke_u32`). The "image getters" the **ISS** lifecycle cares about are the four
> `get_xml_*` TIE-DB blob getters above; the **program/data** image is loaded by
> the ELF/peek-poke surface, not a per-region getter set.

### 3.2 The table accessors return `.data.rel.ro` pointers

The harness reads the dispatch + descriptor tables once via the `dll_get_*`
accessors. Each is a literal `lea <table>(%rip),%rax;ret`; the targets
(`objdump -d`):
| Accessor | Target symbol | VMA |
|---|---|---|
| `dll_get_stage_functions` | `slot_semantic_functions` | `0x227ecc0` |
| `dll_get_issue_functions` | `slot_issue_functions` | `0x227f2c0` |
| `dll_get_stall_functions` | `slot_stall_functions` | `0x227f8c0` |
| `dll_get_state_table` | `dll_state_table` | `0x221a3c0` |
| `dll_get_regfile_table` | `dll_regfile_table` | `0x227eac0` |
| `dll_get_ext_regfile_table` | `dll_ext_regfile_table` | `0x17d0740` (all-zero) |

The three `slot_*_functions` tables are **47 records × 32 B** in `.data.rel.ro`
(subtract `0x200000` for `xxd`); the per-record `dispatch_array` field points at
the per-(format,slot) `{mnemonic, fn}` arrays the harness invokes each cycle. See
[The cas Timing Model](./cas-timing-model.md) §3 for the record layout.

---

## 4. The memory model

### 4.1 The ≈4.63 MB per-instance state block

`dll_get_data_size()` returns **`0x4a09f0` = 4,852,208 bytes** (≈4.63 MB, §1.4) — the size
the harness `malloc`s per core and `dll_initialize` `memset`s. Layout, from the
accessor bodies that read each region: `[HIGH · OBSERVED offsets; MED · INFERRED
region spans]`

| Offset | Region | Role |
|---|---|---|
| `state+0x0` | harness handle | arg1 of `dll_initialize` (`mov %rbp,(%rbx)`) |
| `state+0x8` | vtable base | `ctx->vtable_base` |
| `state+0x10..` | **harness shared-helper vtable** | copied via `rep movsq`; the `register`/value hooks live here (`call *0x10`, `call *0x28`) |
| `state+0x5a0` | control word | mirrored from `ctx+0x598` |
| `state+0x5a8` | **flags byte** | bit0 event-trace, bits1–2 stall-eval mode, bit3 cycle-advanced |
| `state+0x8f8` | **scoreboard / regfile commit array** | `dll_cycle_advance` lands committed writes here (`+0x8f8 + reg*4`) |
| `state+0x24e4` | cycle-advance guard | tested at `dll_cycle_advance` entry |
| `state+0x24ec` | **ring head** (`& 0x1f`) | decremented one per tick |
| `state+0x24f4 / +0x2574 / +0x25f4 ...` | 4 WB-lane commit blocks | `{value, reg#, commit_flag}` per lane, stride `0x300` |
| `state+0x484c` | current pipeline-stage index (`& 0x1f`) | read by every scoreboard accessor |
| `state+0x14eb0..` | **architectural register slots** | first slot = `"AR"`; one per registered state register |
| `state+0x152xx` | host **VALUE** callback bases | the datapath handoff to fiss (10,807 refs) |
| `state+0x158xx` | issue-time def/reserve hooks | the cas **timing** side (`+0x158c0` base, `+0x158c8` method) |

The register-slot table at `+0x14eb0` and the dispatch hooks at `+0x152xx`/
`+0x158xx` give the structural rule a reimplementer needs: **`0x152xx` =
host-values, `0x14exx`/`0x158xx` = timing** (see
[cas Timing Model](./cas-timing-model.md) §7). `[HIGH · OBSERVED for the cited
offsets; the inter-region spans are INFERRED.]`

> **NOTE — the 32-deep ring is the `MAX_POSSIBLE_PARTIAL_STAGES` bound.** Both
> `state+0x484c` and `state+0x24ec` mask `& 0x1f` (a 32-entry ring), matching
> `libfiss.h`'s `#define MAX_POSSIBLE_PARTIAL_STAGES 32` exactly — the header
> bound and the binary mask agree, which is why the header is trustworthy as a
> binary-adjacent spec. The 16 logical pipeline stages (the
> `_inst_stage0..15` thunks) map into the 32-physical ring 2× over so producer
> and consumer waves never alias within the interlock window. The header also
> fixes `SEMANTIC_STAGES 7` (stateload/regload/memload/memstore/memstore_check/
> writeback/opcode_complete) and `MAX_POSSIBLE_EXCEPTIONS 64` — the value-side
> stage taxonomy the fiss page enumerates. `[HIGH · OBSERVED mask; header is
> corroboration.]`

### 4.2 Local memory windows — IRAM / DataRAM (the SBUF window)

The modeled core's local memories are **config data**, read by the harness from
`ncore2gp-params`, not baked into the core. The `ISS*RAMInfo` rows give the
geometry the harness maps into the simulated core:
```ini
# [ size  base_address  access_width  busy dma cbox rcw parity ecc enable_mask enable_code udma dyn_base ]
ISSInstRAMInfo  = [ 0x10000 0x00000000 256 0 1 0 0 0 0 0          0 0 0 ]   # IRAM  64 KB @ 0x0,        256-bit
ISSDataRAMInfo  = [ 0x10000 0x00080000 512 0 1 5 0 0 0 0xe0000000 0 1 0 ]   # DataRAM 64 KB @ 0x80000, 512-bit  (the SBUF window)
ISSDataRAMBanks = 4    ISSDataRAMSubBanks = 8
LoadStoreWidth  = 512  LoadStoreUnitsCount = 2     # 512-bit vector LSU, 2 parallel lanes (the nx_*_0/_1 ports)
num_aregs       = 64                               # the windowed AR file (the first registered state reg, §1.2)
InstFetchWidth  = 256  InstCIFCycles = 3  DataCIFCycles = 1   InstRAM0Latency = 3  DataRAM0Latency = 4
```

So the **SBUF / DataRAM window is 64 KB at target VA `0x00080000`, 512-bit
access, 4 banks × 8 sub-banks**, and the instruction RAM is 64 KB at `0x0`,
256-bit fetch. The 512-bit `LoadStoreWidth` and the **2** load/store units are
the binary-side `nx_*MemAccess_0`/`_1` dual-pipe LSU from
[cas Core Surface](./cas-core-surface.md) §3.1. The host↔target bridge for typed
values is `libctype`'s `base_addr` window (`cstub_set_base_address`,
[libctype — CSTUB](./libctype-cstub.md) §2.1); cas models the **timing** of these
accesses (load-use latency + write-buffer occupancy), the host owns the **data**.
`[HIGH · OBSERVED config; the cas-timing/host-data split is CARRIED from the
timing + surface pages.]`

> **GOTCHA — per-region (SBUF vs HBM) latency is NOT in `libcas-core`.** The core
> models a fixed load-use latency (4 scalar / 10 vector) plus structural
> write-buffer occupancy; there is no per-region latency table inside it. The
> region geometry and per-RAM latencies (`InstRAM0Latency 3`, `DataRAM0Latency 4`,
> `DramLongLatency 0`) live **in the params file**, applied host-side. A
> reimplementer who needs region-aware DRAM latency reads it from the config and
> applies it in the host memory model, not from cas.

### 4.3 RAM image initialization

The four RAM-init constants in the config are the fill patterns the harness
writes before loading the program/data images via the ELF/peek-poke surface
(§3.1):
```ini
iss_iram_init_value = 0x6c6cb6b6     # IRAM fill (an illegal-op poison pattern)
iss_dram_init_value = 0x01234567     # DataRAM fill
iss_irom_init_value = 0x00000000     iss_drom_init_value = 0x00000000
ISSSysRamBytes = 0x40000000  ISSSysRamPAddr = 0x00100000   # 1 GB system RAM @ 0x100000
```

---

## 5. Trace and performance counters

### 5.1 Event trace — the `state+0x5a8` flags byte (core) → `TRACER` (harness)

The only trace machinery *inside* `libcas-core` is the **flags byte at
`state+0x5a8`**, toggled by three accessors and read by `dll_cycle_advance`:
| Accessor | Disasm | Effect |
|---|---|---|
| `dll_enable_events @0x17b57f0` | `movzbl 0x5a8(%rdi); or %esi; and $~1 ...; mov %sil,0x5a8(%rdi)` | set/clear **bit0** = event trace |
| `dll_set_tie_stall_eval @0x17aa2f0` | `... shl $0x2; and $~4; ...` | set/clear **bits 1–2** = TIE stall-eval mode |
| `dll_reset_cycle_advanced @0x17aa3b0` | `andb $0xf7,0x5a8(%rdi);ret` | clear **bit3** = cycle-advanced |

`dll_cycle_advance` *sets* bit3 (`orb $0x8`) on entry and the commit path checks
bit0 before firing the (harness-side) trace hook. So **trace is a host
responsibility gated by a single core flag** — the core raises the event, the
harness writes the trace record. `libcas-core` itself carries **no disassembler
and no trace writer**: its only trace symbols are the two undefined imports
`nx_RSRTraceBus_interface` / `nx_WSRTraceBus_interface` (the SR/ER register-bus
trace passthrough), which the harness *defines*
(`CycleCore::RSRTraceBus_interface` / `WSRTraceBus_interface`).
The full trace/disassembly subsystem lives in the harness `libsimxtcore.so`:

- **Disassembler** — `XTCORE_MEM::disasm_from_pc`, `disasm_from_fmt_buff`,
  `disasm_from_slot_buff`, and `CycleCore::disasm_from_di` (there is **no**
  `xt_iss_disassemble`; the entry point is `disasm_from_pc`).
- **Trace port** — the `TRACER` writer class, configured by `TraceControl`
  (`set_level` / `set_start` / `set_stop` / `set_fp` / `set_explicit_bitmask` /
  `update_bitmask`), with the TRAX buffer (`libdb_ereg_trax`; strings
  `traceport --trax --memsz=%d`, `event_trace --level %u`, `--trace=n` /
  `--dtrace=n`).
- **PC/branch callbacks** — `MP_xtcore::setBranchTraceCallBack` and
  `setTurboPcTraceCallBack` register host callbacks invoked per branch / per PC.

The trace *port* geometry is config: `ncore2gp-params` carries `TracePort = 1`,
`TracePortData = 0`, `TracePortMemBytes = 8192`, `TRAX = 1` — i.e. an 8 KB TRAX
buffer the harness's `TRACER` drives, not the core.

> **NOTE — `CycleCore` is the *harness* class, not a cas symbol.** The surface
> page's CORRECTION that there is no `CycleCore` *inside* `libcas-core` stands:
> `CycleCore` (with `disasm_from_di`, `RSRTraceBus_interface`,
> `WSRTraceBus_interface`) is a class in **`libsimxtcore.so`** — it is the
> harness's wrapper that drives the flat `dll_*` cas core and *defines* the two
> `nx_*TraceBus_interface` ports the core imports. The dlopen-boundary ABI is
> still the flat 24-accessor C vocabulary; `CycleCore` is the C++ driver above it.

### 5.2 The per-stage / issue / stall counters

The "counters" a reimplementer diffs against are the **census of per-op pipeline
functions** — the distributed schedule, grounded from `nm` on the one binary:
| Symbol pattern | `nm libcas-core.so \| rg -c` | Role |
|---|---|---|
| `_inst_stage[0-9]+` | **157,775** | per-(format,slot,mnemonic) × stage0..15 semantic latches |
| `_inst_.*_issue` | **2,149** | per-op issue thunks (admit + POST def reservation) |
| `_inst_.*_stall` | **1,651** | per-op stall thunks (tail-chain into the shared `my_*` predicates) |
| `my_.*_cycle` | **81** | per-SR stage-indexed shift registers (`CCOMPARE0/1/2`, `IVP_FS0..7`, `EPC/EXCCAUSE`) |
| `nx_.*_interface` (U) | **119** | host value/memory ports — the value seam |

> **GOTCHA — there is no aggregate cycle/perf-counter readout accessor.** The cycle
> count is **abstract**: the wall-clock cycle is whatever the harness accumulates
> by counting `dll_cycle_advance` calls. The architectural **CCOUNT** register
> exists *only* as RSR/WSR/XSR opcode handlers (`RSR_CCOUNT_opcode_stage0..5`,
> etc.) — it has **no `my_CCOUNT_*` pipeline state machine at all** (unlike
> `CCOMPARE0/1/2`, which have the full `my_CCOMPARE*_cycle`/`_def`/`_stall`
> machinery). A sweep for `cycle_count` / `get_cycle` / `perf` / `icount` /
> `retired` / `instret` returns **zero** symbols and **zero** strings. The config
> exposes `perfCounterCount = 8` but **`IsaUsePerfCounters = 0`** — this core
> ships with the hardware PMU disabled, so the ISS exposes no perf-counter
> readout; performance is measured by the harness counting ticks against the
> per-op latency model.

---

## 6. Summary table — the run model at a glance

| Aspect | Model | Tag |
|---|---|---|
| Plugin ABI | 24 `dll_*` `extern "C"` accessors, `@@VERS_1.1`, `LIBFISS_VERSION 2`; no factory/`getInterface` | HIGH |
| Identity | `dll_get_version → 0x1381f`; `dll_get_data_size → 0x4a09f0 = 4,852,208 B` (live-certified) | HIGH |
| Init | `dll_initialize`: `memset(0x4a09f0)` → install harness vtable (`rep movsq`, state+0x10) → register state regs (first = `"AR"`) | HIGH |
| Step | `dll_cycle_advance(state,dir)`: commit 4 WB lanes → `+0x8f8`, dec ring head (`& 0x1f`), shift ring; `dir` ⇒ rollback | HIGH |
| Decode | `dll_instruction_get_length(_,op0)`: op0-low-nibble → {3,2,16,8,illegal} B | HIGH |
| Teardown | `free(state)` — no `dll_finalize` exists | HIGH |
| Config | `ncore2gp-params` binds iss/fiss/ctype/xml-base/xml-msem DLLs by role; 4 `get_xml_*` image getters in nativesim | HIGH |
| State block | 4,852,208 B; harness vtable @+0x10, flags @+0x5a8, scoreboard @+0x8f8, ring head @+0x24ec, regs @+0x14eb0, values @+0x152xx | HIGH |
| Memory | IRAM 64 KB @0x0 (256-bit), DataRAM/SBUF 64 KB @0x80000 (512-bit, 4×8 banks), 2-lane 512-bit LSU; per-region latency host-side | HIGH |
| Trace | one core flag (state+0x5a8 bit0) + 2 TraceBus imports; harness owns disasm (`disasm_from_pc`) + `TRACER`/TRAX | HIGH |
| Perf | abstract cycles (count `dll_cycle_advance`); 157775 stage / 2149 issue / 1651 stall thunks; no CCOUNT machine; PMU disabled | HIGH |

---

## 7. Open items / uncertainty

- **[MED]** The exact harness orchestration around the `dll_*` calls (§1.5) — the
  individual `dll_*` invocations are OBSERVED, but `libsimxtcore`/`nativesim` are
  stripped, so the precise call *order* and the image-load plumbing are INFERRED
  from the ABI + the `get_xml_*`/`cstub_*`/ELF-loader symbol references.
- **[MED]** The `dll_cycle_advance` **rollback** path (`dir<0`) — presence is
  HIGH (the three-way `dir` branch is in the disasm); the exact reverse-commit
  semantics across ~16 KB are not fully traced.
- **[MED]** Whether `dll_init_tieport_outputs` / `dll_reset_states` /
  `dll_export_state_stall` have side effects beyond their inferred names — they
  are part of the 24-accessor surface (OBSERVED present) but their bodies were
  not walked in full.
- **[LOW]** The exact byte spans of the inter-region gaps in the ≈4.63 MB block —
  the cited offsets are OBSERVED from accessor bodies; the spans between them are
  not exhaustively mapped.
- **[HIGH]** §1 (the 24-accessor ABI, the lifecycle, the two live-certified
  CORRECTIONs), §2 (`dll_cycle_advance` commit/shift), §3 (the config wiring +
  the 4 `get_xml_*` getters), and the §5 counts are direct binary evidence
  (disasm + bytes + executed `ctypes`).

The unified, re-runnable reference — one driver that emits both the cycle count
(this lifecycle + the cas timing model) and the architectural value (the fiss
oracle) for any bundle — is assembled in
[The ISS as Executable Oracle](./iss-oracle-synthesis.md). The cross-validation
of this run path against the `libcas-ref-core.so` / `libfiss-ref-base.so`
reference cores named in `ncore2gp-params` is on
[ref-vs-production diff](./ref-vs-production-diff.md).
