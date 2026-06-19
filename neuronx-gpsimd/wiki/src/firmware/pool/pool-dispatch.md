# POOL Engine Main Dispatch Loop

This page **owns and fully characterizes** the GPSIMD **POOL engine's** per-core compute
dispatch loop — the engine that actually *runs* the tensor kernels on each Vision-Q7 pool
core, the execute-side counterpart to the [SEQ Decode / Dispatch Hub](../seq/dispatch-hub.md).
Where the SEQ engine is the **front-end** that fetches the instruction stream and hands out
opcodes (its `'S:'` log stream, its dense 178-entry direct-indexed jump table at DRAM
`0x80814`), the POOL engine is the **back-end** that receives one opcode, looks up its kernel
through the **`kernel_info_table`** (key = `(opcode<<24)\|(spec<<16)`), and executes that
tensor kernel across the core's share of channels (its `'P%i:'` per-core log stream).

This page is the definitive reference for the POOL **dispatch loop and its opcode→kernel
map**; the table's own binary layout has a dedicated companion page
([kernel_info_table Binary Layout](kernel-info-table.md)), and the two-level `0xf0`
extended-instruction sub-dispatch its own ([POOL Extended-Opcode (0xF0) Dispatch](pool-ext-0xf0.md)).
The opcode→kernel rows enumerated here in §6 are the table the per-kernel pages
(Iota, Copy, Cast, Cross-Lane-Reduce, Tensor-Tensor-Arith, Tensor-Dequantize, …) cross-link
back to.

The POOL engine runs on the same Vision-Q7 NX `ncore2gp` "Cairo" datapath core as the SEQ
engine (`IsaMaxInstructionSize = 32`, `XCHAL_HAVE_VISION = 1`, `XCHAL_VISION_TYPE = 7` — the
FLIX/VLIW layer; `XCHAL_HAVE_FLIX3 = 0` does **not** mean "scalar"). Decode the POOL images
with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); the scalar-LX rule decodes the
*different* NCFW management core and is wrong here.

> **NOTE — what was re-carved this session, and the exact objects used.** Every fact below was
> re-derived from a fresh independent carve out of the static archive `libnrtucode.a`
> (`sha256 158dadc5…d7bd6130`; byte-identical to the repo copy under
> `extracted/…/gpsimd/custom_op/c10/lib/`). Two members carry the POOL dispatcher:
>
> - **PERF (the canonical code+table image):** `img_CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_contents.c.o`.
>   Its `.rodata` payload (file offset `0x60`, size `0xa260`) **is** the embedded Vision-Q7
>   ELF `internal_CAYMAN_0.so` — **41,568 B, sha256 `910d41c3ededce67cd00ec7041a5e66c3c39536d2e9b16fe21ea019db4b55527`**,
>   ELFCLASS32 LE, `e_machine = 94` (Xtensa), `ET_EXEC`, `e_entry = 0x01005610`. This is the
>   POOL dispatcher + the `kernel_info_table`.
> - **DEBUG (same logic + the `'P%i:'` log strings):** `img_CAYMAN_Q7_POOL_DEBUG_DRAM_contents.c.o`.
>   Its `.rodata` payload (carved with `objcopy -O binary --only-section=.rodata`) reproduces
>   `dbg_dram.bin` = **89,344 B, sha256 `226f4254…f6f0128e`**, which carries the
>   `'P%i:'`-prefixed dispatch trace strings.
>
> The `'P%i:'` prefix (per-pool-core index `i`) is what distinguishes the POOL execute path
> from the SEQ engine's `'S:'` stream. The `kernel_info_table` and `.text` were re-parsed /
> re-disassembled from `internal_CAYMAN_0.so` this session; the table is fully byte-exact, the
> dispatch *loop body* is partly FLIX-desynced (§4, honestly flagged). `[HIGH/OBSERVED]`

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/string read from the shipped image this session; `INFERRED` = reasoned
over OBSERVED facts (often across a FLIX/literal-pool desync); `CARRIED` = consolidated from a
cited cross-page anchor at its original confidence. Crossed with `HIGH`/`MED`/`LOW`. Callouts:
**QUIRK** (counter-intuitive but real), **GOTCHA** (a reimplementation trap), **CORRECTION**
(overturns a naive reading), **NOTE** (orientation).

---

## 0. The POOL dispatch loop in one diagram

```
   opcode (small int, handed in by the SEQ front-end — POOL does NOT self-fetch)
        │
        │  log "P%i: In dispatch, CPU ID: %0d, got opcode 0x%x."   (i = pool-core idx)
        │  cpu = get_cpu_id()       // [0, get_cpu_count())  — WORK-PARTITION gate, not a skip
        │  key = (opcode<<24)|(spec<<16)
        ▼
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │  base = 0x02000380   end = 0x02000408   count = (end−base)>>3 = 17             │
   │  LINEAR SCAN  for (i=0; i<17; ++i)                                             │
   │      if (kernel_info_table[i].key == key)  goto HIT;     // 8-byte stride      │
   │  (table is REGISTRATION order, NOT sorted → cannot binary-search)              │
   └───────────────┬──────────────────────────────────────────┬────────────────────┘
                   │ HIT                                        │ MISS (scan exhausted)
                   ▼                                            ▼
   funcVA = kernel_info_table[i].funcVA   (entry+4)     log "P%i: UNKNOWN OPCODE=0x%x"
   callx8 funcVA   → per-opcode kernel ENTRY TRAMPOLINE       → (POOL miss policy)
        │
        │  trampoline: entry a1,N ; load per-kernel .bss state ptr ; run kernel
        │  kernel partitions work by get_cpu_id() across its channels (num_chans/pool_num)
        │  opcode 0xf0 → dispatch_extended_inst, sub-selected by the SPEC byte:
        │       spec 1 = ExtendedInstCopy ; spec 2 = ExtendedInstTensorTensorArith ; …
        │       sub-miss → log "P%i: UNKNOWN EXTENDED OPCODE=%d"
        ▼
   log "P%i: Exiting Dispatch"
```

One-line verdict: the POOL dispatch loop is a **sparse, keyed (not indexed) LINEAR SCAN over a
17-entry, 8-byte-record `kernel_info_table`** at VMA `0x02000380`, matching the packed
`(opcode<<24)\|(spec<<16)` key; on a hit it does a **single `callx8`** to a per-opcode kernel
entry trampoline (one hop — no SEQ-style impl/thunk indirection); on a miss it logs
`"P%i: UNKNOWN OPCODE=0x%x"`. The opcode `0xf0` row family (five entries keyed by the spec
byte) is a **second-level** extended-instruction dispatcher. Source module: `dispatch.hpp`
(baked `dispatch` + `dispatch.hpp` strings).

---

## 1. Key facts

| Fact | Value | Anchor | Conf |
|---|---|---|---|
| Anchor PERF image | `internal_CAYMAN_0.so` = `.rodata` of `img_CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_contents.c.o` | `ar t` + `objcopy`/`dd` carve | `[HIGH/OBSERVED]` |
| PERF image sha256 | **`910d41c3…b4b55527`**, 41,568 B (`0xa260`) | `sha256sum` | `[HIGH/OBSERVED]` |
| Anchor DEBUG strings | `dbg_dram.bin` = `.rodata` of `img_CAYMAN_Q7_POOL_DEBUG_DRAM_contents.c.o` | `objcopy --only-section=.rodata` | `[HIGH/OBSERVED]` |
| DEBUG image sha256 | **`226f4254…f6f0128e`**, 89,344 B | `sha256sum` | `[HIGH/OBSERVED]` |
| Engine prefix | `'P%i:'` (per-pool-core index `i`) — distinct from SEQ `'S:'` | string grep | `[HIGH/OBSERVED]` |
| Dispatch entry | `e_entry = 0x01005610` (`entry a1,32`; interior FLIX entry of fn START `0x0100511c`) | `readelf -h` + prologue bytes `36 41 00` | `[HIGH/OBSERVED]` |
| Table base | `kernel_info_table` VMA `0x02000380` (file off `0x7400`) | `readelf -S` | `[HIGH/OBSERVED]` |
| Table end | VMA `0x02000408` (== `.globstruct` base; no sentinel) | `readelf -S` | `[HIGH/OBSERVED]` |
| Entry stride | **8 bytes** (`key4` + `funcVA4`) | `0x88/17`; reloc stride | `[HIGH/OBSERVED]` |
| Entry count | **17** (three independent ways — §3) | `0x88/8`; 17 relocs; `(end−base)>>3` | `[HIGH/OBSERVED]` |
| Record layout | `{ u8 0; u8 0; u8 spec(+2); u8 opcode(+3); u32_le funcVA(+4) }` | byte decode of all 17 | `[HIGH/OBSERVED]` |
| Key formula | `(opcode<<24)\|(spec<<16)` == native-LE u32 of `entry[0..4]` (all 17) | `struct.unpack` check | `[HIGH/OBSERVED]` |
| funcVA relocation | `R_XTENSA_RELATIVE`, exactly 17, at `base+8*i+4` (i=0..16) | `readelf -r` | `[HIGH/OBSERVED]` |
| funcVA targets | all 17 land on `entry a1,N` (16× `a1,32`; 1× `a1,48` @ entry 9) | prologue bytes `36 41 00` / `36 61 00` | `[HIGH/OBSERVED]` |
| Lookup | LINEAR SCAN by packed key (registration order, NOT sorted) | key column `7e 7c 7d 45 51 41 f0×5 52 46 47 be f2 7b` | `[HIGH order / MED loop body]` |
| Call on hit | single `callx8 funcVA` (1 hop to kernel trampoline) | trampoline prologues; desynced call site | `[HIGH structure / MED encoding]` |
| Two-level `0xf0` | five `0xf0` rows keyed by spec → `dispatch_extended_inst` | spec column `0,1,2,4,3` + DEBUG strings | `[HIGH/OBSERVED]` |
| CPU-ID gate | `get_cpu_id()` (`[0, get_cpu_count())`) — work-partition, not skip | shipped `neuron-utils.hpp` + `'P%i:'` trace | `[HIGH api / MED in-loop branch]` |
| Miss path | `"P%i: UNKNOWN OPCODE=0x%x"` (top) / `"P%i: UNKNOWN EXTENDED OPCODE=%d"` (0xf0 sub) | string-anchored, 1 record each | `[HIGH/OBSERVED]` |
| `'P%i:'` records (DEBUG) | **156** (`rg -c` == literal-substring count) | `rg -c -a 'P%i:'` / `bytes.count` | `[HIGH/OBSERVED]` |
| `'P%i:'` strings (PERF) | **0** (stripped) | `strings internal_CAYMAN_0.so` → no logs | `[HIGH/OBSERVED]` |
| Cross-image stability | same 17-entry table @ same VMA in MARIANA / MARIANA_PLUS | §10 | `[HIGH/CARRIED]` |

---

## 2. Engine topology — SEQ fetch/decode → POOL execute

Two distinct dispatch engines ship in the firmware, distinguishable by log prefix and string
set. They are not two copies of one loop; they are the **two halves of one pipeline**.

| | **SEQ engine** ([dispatch-hub.md](../seq/dispatch-hub.md)) | **POOL engine** (this page) |
|---|---|---|
| Role | front-end: **fetch + decode**, hand opcodes to pool cores | back-end: **execute** the tensor kernel per core |
| Log prefix | `'S:'` (`img_*_NX_POOL_DEBUG_*`) | `'P%i:'`, `i` = pool-core index (`img_*_Q7_POOL_DEBUG_*`) |
| Owns the fetch | yes — `"fetch_cache_line"`, `"S: start_fill_siram: fetch_addr=0x%llx"`, `"setup_enqueue_dispatch_descriptors"`, HW/SW decode toggle | **no fetch vocabulary at all** in the `'P%i:'` strings |
| Table | DRAM `0x80814`, 178 entries, **4-byte** absolute targets, DIRECT-INDEXED (`opcode−0x41`) | `kernel_info_table` VMA `0x02000380`, 17 entries, **8-byte** keyed records, LINEAR-SCAN |
| Indirection | table → trampoline → impl → thunk → `Handler::execute()` (4 hops) | table → `callx8 funcVA` → kernel (1 hop) |

> **NOTE — the SEQ↔POOL handoff is the architectural division.** The SEQ engine alone owns the
> instruction-stream fetch (it can run with HW opcode decode ON — `"S: NX in HW Decode mode"`
> — or OFF — `"S: NX in Sunda mode: HW decode disabled"`) and enqueues DMA descriptors. By the
> time a pool core logs `"P%i: In dispatch, CPU ID: %0d, got opcode 0x%x."`, it already **has**
> the opcode (it *"got"* it) and tags the trace with its CPU ID. The pool core does **not**
> parse a ucode byte stream; that is the SEQ engine's job. The SEQ side's full dispatch table
> and the cross-engine contrast are characterized once on
> [dispatch-hub.md §9](../seq/dispatch-hub.md#9-seq-vs-pool-dispatch--the-structural-contrast)
> and are not re-derived here. `[direction HIGH/OBSERVED; exact handoff register/slot MED]`

---

## 3. Table bounds & entry count — byte-exact, three independent ways

The `kernel_info_table` is delimited by the section geometry and by a **base/end getter pair**
the dispatcher calls — there is **no in-band sentinel**. The count `17` is verified three
mutually-independent ways:

**(1) Section size / stride.** `readelf -S internal_CAYMAN_0.so`:

```
[ 7] kernel_info_table  PROGBITS  Addr 0x02000380  Off 0x007400  Size 0x000088  Al 8
[ 8] .globstruct        PROGBITS  Addr 0x02000408  Off 0x007488  Size 0x000048  Al 8
```

`0x88 / 8 = 17`. The table ends exactly where `.globstruct` begins (`0x02000408`); the bytes
after entry 16 are `.globstruct` data (`0x6099cb34 …`), **not** a `0x00000000` / `0xffffffff`
terminator. `[HIGH/OBSERVED]`

**(2) Relocation count.** Exactly **17** `R_XTENSA_RELATIVE` (type 5) relocations exist whose
`r_offset = base + 8*i + 4` for `i = 0..16` — one per entry, each pointing at the `funcVA`
slot, stride 8, addend 0. This independently proves 8-byte stride, `funcVA` at `+4`, key
**not** relocated, and count = 17. (Re-checked this session: 17/17 land exactly at
`base+8*i+4`.) `[HIGH/OBSERVED]`

**(3) Firmware count arithmetic.** The dispatcher loads the bounds inline and computes the
count. Two helper getters sit just before the entry point:

```
0x010055f8  base-getter : entry a1,32 ; const16 a2,0x200 ; const16 a2,0x380 ; retw.n
            → kernel_info_table BASE = 0x02000380
0x01005607  end-getter  :              const16 a2,0x200 ; const16 a2,0x408 ; retw.n
            → kernel_info_table END  = 0x02000408
0x01005610  entry a1,32   (dispatch prologue; bytes 36 41 00 — OBSERVED)
```

and inside the dispatcher the count is derived:

```
const16 a4,0x200 ; const16 a4,0x380   ; a4 = 0x02000380  (BASE)
const16 a3,0x200 ; const16 a3,0x408   ; a3 = 0x02000408  (END)
sub     a3,a3,a4                       ; a3 = end − base = 0x88
srli    a3,a3,3                        ; a3 = 0x88 >> 3 = 17   (>>3 == /8 confirms 8-byte stride)
```

`(0x408 − 0x380) >> 3 = 0x88 >> 3 = 17`. `[base/end VMAs HIGH/OBSERVED from §1 geometry; the
sub/srli arithmetic is in the FLIX-scheduled span (§4) → the *result* 17 is HIGH (confirmed
three ways), the exact instruction encodings INFERRED from the bounds + the count]`

> **QUIRK — the table length is materialised exactly once, from a getter PAIR, not a sentinel.**
> The `0x380`/`0x408` const16 immediates that build BASE and END occur **once** in the entire
> `.text`; there is no other place the table is bounded, and no terminator record. A
> reimplementation that scans for a `0x00000000`/`0xffffffff` sentinel will read into
> `.globstruct`. Bound the scan by `count = (end−base)>>3`. `[HIGH/OBSERVED]`

---

## 4. The dispatch loop body — annotated pseudocode (HIGH structure / MED encoding)

Immediately after the count compute, the per-entry compare loop is **hand-scheduled FLIX VLIW
with literal pools interleaved into `.text`**. `xtensa-elf-objdump`'s linear sweep loses bundle
sync across this span (the documented Vision-Q7 corpus limitation). **Concrete proof of the
desync:** at `~0x01005670` the byte stream contains a literal-pool data constant that the
linear sweep mis-renders as a FLIX bundle; the spurious `call8`/`call0`/`callx8` targets the
sweep prints in `0x01005657..0x01005669` fall **outside** the image's `0x01000000..0x01006f1e`
`.text` range, so they are **not** trusted and **not** reported as real here.

What is **structurally certain** (from the table format, the byte-exact count arithmetic, and
the trampoline layout — all OBSERVED) is the loop *shape*. The exact per-entry compare-and-
branch encoding is **not** recoverable from this image with the linear sweep; it is reported as
a linear-scan-by-packed-key with `callx8`-on-hit.

```c
/* POOL per-core dispatch loop — structure HIGH/OBSERVED, exact body MED.
 *
 * Symbols/addresses (PERF image internal_CAYMAN_0.so, all OBSERVED unless noted):
 *   BASE  = 0x02000380   (base-getter @0x010055f8)
 *   END   = 0x02000408   (end-getter  @0x01005607; == .globstruct base)
 *   ENTRY = 0x01005610   (e_entry, `entry a1,32`)
 *
 * struct kernel_info_entry {            // 8 bytes, see kernel-info-table.md
 *     uint8_t  zero0, zero1;            // +0,+1 : always 0 (high bytes of BE key)
 *     uint8_t  spec;                    // +2    : sub-opcode / spec selector
 *     uint8_t  opcode;                  // +3    : POOL / extended-inst opcode
 *     uint32_t funcVA;                  // +4    : kernel entry trampoline VA (R_XTENSA_RELATIVE)
 * };                                    // native-LE u32 of [0..4] == (opcode<<24)|(spec<<16)
 */
void pool_dispatch(uint8_t opcode, uint8_t spec) {     /* opcode handed in by SEQ front-end (§5) */
    /* DEBUG build only: */
    log("P%%i: Entering Dispatch");                    /* string @ dbg_dram.bin             */
    uint32_t cpu = get_cpu_id();                        /* [0, get_cpu_count()); §8           */
    log("P%%i: In dispatch, CPU ID: %%0d, got opcode 0x%%x.", cpu, opcode);

    const kernel_info_entry *base = (void *)0x02000380; /* 0x010055f8 base-getter             */
    const kernel_info_entry *end  = (void *)0x02000408; /* 0x01005607 end-getter              */
    uint32_t count = (uint32_t)(end - base);            /* = (END-BASE)>>3 = 17  (§3, byte-exact)*/
    uint32_t key   = ((uint32_t)opcode << 24)           /* native-LE u32 of entry[0..4]       */
                   | ((uint32_t)spec   << 16);

    for (uint32_t i = 0; i < count; ++i) {              /* LINEAR SCAN (registration order)   */
        uint32_t entry_key = *(const uint32_t *)&base[i]; /* read packed key (opcode@+3,spec@+2)*/
        if (entry_key == key) {                          /* MED: exact compare encoding desynced*/
            void (*kernel)(void) = (void(*)(void))base[i].funcVA; /* entry+4, load-relocated   */
            kernel();                                    /* `callx8 funcVA` — ONE hop (§4a)    */
            log("P%%i: Exiting Dispatch");
            return;
        }
    }
    /* MISS: scan exhausted */
    log("P%%i: UNKNOWN OPCODE=0x%%x", opcode);            /* string @ dbg_dram.bin (§9)        */
    /* → POOL miss policy (§9) */
}
```

> **GOTCHA — keyed scan, NOT a direct index. Do not reuse the SEQ index trick.** The SEQ engine
> computes `index = opcode − 0x41` and indexes its dense 178-slot table in O(1). The POOL table
> is **sparse and keyed**: it holds only the ~17 opcodes *this image* implements, the key
> column is in **registration order** (`7e 7c 7d 45 51 41 f0 f0 f0 f0 f0 52 46 47 be f2 7b`,
> not ascending), and it cannot be binary-searched. The lookup is an O(17) linear key compare.
> A reimplementation that direct-indexes by opcode reads garbage. `[HIGH/OBSERVED]`

### 4a. The hit path — `callx8` to a per-opcode kernel entry trampoline

On a key match the dispatcher loads `funcVA` from `entry+4` (the only relocated field) and does
a **register-indirect windowed call** (`callx8 <funcVA>`) to the per-opcode kernel **entry
trampoline**. There is no SEQ-style impl-shim/thunk chain — the POOL hit is **one hop**. The
presence of a register-indirect call in the routine is consistent with the `callx8` opcodes the
sweep prints in this region, but the exact hit-bundle is desynced; reported MED.

All 17 `funcVA`s were re-confirmed this session to land on a clean Xtensa windowed-ABI prologue
(`entry a1,N`), proving each is a real function entry, not the middle of an instruction:

- **16 of 17** begin `entry a1, 32` (bytes `36 41 00`).
- **entry 9** (`0xf0` spec 4, funcVA `0x010037a8`) begins `entry a1, 48` (bytes `36 61 00`) — a
  larger frame.

### 4b. Trampoline shape — two flavours, OBSERVED

Re-disassembling each `funcVA` this session splits the trampolines into two structural shapes:

**State-pointer trampolines** (entries 3, 5, 8, 15, 16) decode **cleanly** as:

```
entry a1,32 ; const16 a2,0x200 ; const16 a2,0x4NN     ; a2 = per-kernel .bss state pointer
```

with the second const16 building a DRAM pointer in `.bss` (`0x02000450 .. 0x0200048c`):

| entry | opcode | funcVA | `.bss` state ptr |
|---|---|---|---|
| 3 | `0x45` (pool/decode_pool) | `0x01000b90` | `0x02000458` |
| 5 | `0x41` (tensor_tensor_arith) | `0x01000f1c` | `0x0200045c` |
| 8 | `0xf0` spec 2 (ext tt-arith) | `0x01003484` | `0x02000468` |
| 15 | `0xf2` (nonzero_with_count) | `0x0100484c` | `0x0200047c` |
| 16 | `0x7b` (tensor_dequantize) | `0x01004dc4` | `0x02000480` |

**FLIX-immediate trampolines** (the remaining entries 0,1,2,4,6,7,9,10,11,12,13,14) have a FLIX
format-selector byte (`0x3f` / `0x2f` / `0x4f` / `0x6f` / `0x7f`) in the second slot — the body
is a hand-scheduled FLIX bundle the linear sweep desyncs on, so only the `entry a1,N` prologue
(§4a) is byte-exact for these. Entry 6 (`0xf0` spec 0) notably decodes `entry a1,32 ; const16
a2,0 ; retw.n` — a near-empty stub.

> **CORRECTION (vs the backing report's specific route immediates) — the cleanly-decoding
> trampolines load a `.bss` state pointer first; the deeper `const16 a2,0xbc0; callx8
> decode_pool` step lives *past* the FLIX-desync point.** SX-FW-14/§6 reports e.g. funcVA[3]
> (`0x45`) as `const16 a2,0xbc0 ; callx8 → decode_pool@0x1000bc0`. Re-disassembled this session,
> funcVA[3]'s *first* materialised constant is `const16 a2,0x200 ; const16 a2,0x458` =
> **state pointer `0x02000458`** (a `.bss` slot), and the byte after that (`8f …`) is a FLIX
> format-selector beginning the desynced bundle that holds the deeper decode-call. Both
> statements describe the same trampoline; the byte-exact, re-verified fact is the
> **`.bss` state-pointer load**, and the `decode_pool` route is the (correct but desync-
> recovered) continuation. The state-pointer column above is HIGH/OBSERVED; the deeper route
> targets are MED. `[CORRECTION folded in place]`

> **QUIRK — POOL kernels use boot-constructed `.bss` state objects, mirroring SEQ's
> boot-constructed `Handler` singletons.** The five clean trampolines each load a consecutive
> 4-byte pointer slot in `.bss` (a NOBITS section, zero-initialised, populated at boot). This is
> the POOL analogue of the SEQ engine's "no static vtable — objects built at boot" finding
> ([dispatch-hub.md §4d/§5](../seq/dispatch-hub.md#4d-the-handler-object-layout--registration--medinferred)):
> the per-opcode kernel state is a boot-constructed singleton the trampoline materialises per
> call, not a static table. `[`.bss` membership + consecutive slots HIGH/OBSERVED; boot-
> construction INFERRED]`

---

## 5. How the loop gets the next opcode (the source) — MED

Evidence points to a **SEQ→POOL handoff**, not the pool core self-fetching from DRAM:

- **SEQ owns the fetch vocabulary** (`"fetch_cache_line"`, `"S: start_fill_siram: fetch_addr=0x%llx"`,
  `"setup_enqueue_dispatch_descriptors"`, the HW/SW-decode toggle). The `'P%i:'` strings contain
  **no** fetch/fill/`fetch_addr` token at all (re-grepped this session). `[HIGH/OBSERVED]`
- The POOL dispatcher logs `"P%i: In dispatch, CPU ID: %0d, got opcode 0x%x."` — by the time the
  core is *"In dispatch"* it already **has** the opcode (it *"got"* it), tagged with its CPU ID.
  The opcode is formatted as a small integer (`0x%x`) and compared against the table's 8-bit
  `opcode` field, consistent with it arriving in a register / shared decode slot rather than
  being parsed from a byte stream by the pool core itself. `[MED]`

**Conclusion:** the opcode SOURCE = handed to the pool core by the SEQ front-end (register /
decode slot); the pool core then runs the software `kernel_info_table` scan. HW-decode vs Sunda
(software)-decode mode is selected on the SEQ side. *(direction HIGH; the exact MMIO/register
name is not pinned by the available images → MED.)*

---

## 6. The opcode → kernel dispatch MAP (the per-kernel cross-link table)

This is the table the per-kernel pages (#688–#748) cross-link back to. Each row is the
byte-exact `(opcode, spec) → funcVA` from the CAYMAN `kernel_info_table` (re-decoded this
session, §3), annotated with the resolved kernel / routing target. The **table itself is
HIGH/OBSERVED** (every byte re-read); the **kernel-name resolution** carries its own confidence
per row (EXACT `.xt.prop` match = HIGH; trampoline route = MED; SUNDA op-number corroboration is
CARRIED from the cross-image opcode JSON the backing reports cite).

| idx | opcode | spec | funcVA | kernel / routing target | name conf |
|---|---|---|---|---|---|
| 0 | `0x7e` (126) | 0 | `0x01000080` | `pool_iota` (Iota; impls `iota_impl<t/f>`) | HIGH |
| 1 | `0x7c` (124) | 0 | `0x010003f8` | `pool_cross_lane_reduce_arith` *(EXACT `.xt.prop`)* | HIGH |
| 2 | `0x7d` (125) | 0 | `0x01000410` | `pool_cross_lane_reduce_bitvec` *(EXACT `.xt.prop`)* | HIGH |
| 3 | `0x45` (69) | 0 | `0x01000b90` | `decode_pool` entry *(state `0x02000458`; → `decode_pool`)* | MED |
| 4 | `0x51` (81) | 0 | `0x0100105c` | `decode_tensor_tensor_arith` entry | MED |
| 5 | `0x41` (65) | 0 | `0x01000f1c` | `pool_tensor_tensor_arith_op` *(state `0x0200045c`; SUNDA op65)* | HIGH(op)/MED(route) |
| 6 | `0xf0` (240) | 0 | `0x01003370` | extended-inst, spec 0 → `dispatch_extended_inst` *(near-empty stub)* | MED |
| 7 | `0xf0` (240) | 1 | `0x01003380` | `pool_extended_inst_copy` *(EXACT `.xt.prop`)* = `ExtendedInstCopy` | HIGH |
| 8 | `0xf0` (240) | 2 | `0x01003484` | `decode_extended_inst_tensor_tensor_arith` *(state `0x02000468`)* = `ExtendedInstTensorTensorArith` | HIGH |
| 9 | `0xf0` (240) | 4 | `0x010037a8` | extended-inst spec 4 *(`entry a1,48`; routes `decode_pool` path)* | MED |
| 10 | `0xf0` (240) | 3 | `0x01003a60` | extended-inst spec 3 *(routes `decode_pool` path)* | MED |
| 11 | `0x52` (82) | 0 | `0x01003b40` | op `0x52` dispatch *(CAYMAN-specific; no SUNDA/DEBUG name)* | LOW |
| 12 | `0x46` (70) | 0 | `0x010040c0` | `pool_copy` *(SUNDA op70; DEBUG "Copy : num_chans")* | HIGH(op)/MED(route) |
| 13 | `0x47` (71) | 0 | `0x01004160` | `pool_cast` *(SUNDA op71; DEBUG "Cast : num_chans")* | HIGH(op)/MED(route) |
| 14 | `0xbe` (190) | 0 | `0x01004204` | `get_sequence_bounds_impl` entry *(trampoline `0x01004204` → body `0x01004284`)* | HIGH |
| 15 | `0xf2` (242) | 0 | `0x0100484c` | `nonzero_with_count_impl<int/float>` entry *(trampoline `0x0100484c` → dtype-branch → bodies `0x01004b80`/`0x01004940`; state `0x0200047c`)* | HIGH |
| 16 | `0x7b` (123) | 0 | `0x01004dc4` | `decode_tensor_dequantize` entry *(state `0x02000480`)* | HIGH(route) |

> **NOTE — name resolution sources and what is grounded here.** The **funcVA / opcode / spec
> columns are fully byte-grounded** (re-decoded from `internal_CAYMAN_0.so` `kernel_info_table`
> this session). Kernel *names* combine: (a) EXACT `.xt.prop.<mangled>` function-start matches
> (entries 1, 2, 7 — funcVA == a named worker start) → HIGH; (b) trampoline routes recovered
> across the desync (entries 3, 8, 16) → HIGH/MED; (c) DEBUG `'P%i:'` per-kernel run/decode log
> strings (`"P%i: Iota"`, `"P%i: Copy : num_chans"`, `"P%i: Cast : num_chans"`,
> `"P%i: TensorDequantize: decode"`); and (d) SUNDA cross-image opcode→pool-fn corroboration for
> op numbers `0x41/0x46/0x47/0x7c/0x7d/0x7e` (CARRIED from the backing reports' SUNDA JSON —
> that map is not in this archive). CAYMAN reuses the SUNDA opcode numbering. `[HIGH for the
> table; per-row name conf as marked]`

> **CORRECTION — idx14 (`0xbe`) and idx15 (`0xf2`) resolved names (off-by-neighbor).** Earlier
> drafts left idx14 `0xbe` as an unresolved "CAYMAN-specific dispatch" (LOW) and mis-attributed
> idx15 `0xf2` to "`get_sequence_bounds` / `tensor_dequantize`" (MED) — a one-slot shift that
> collided `0xbe`'s name onto the `0xf2` row and the neighbouring `0x7b` (idx16) dequantize name
> onto it too. The **funcVA / opcode / spec bytes were always correct**; only the names were off.
> Byte-grounded against the dedicated kernel pages: idx14 `0xbe` → trampoline `0x01004204` →
> body `get_sequence_bounds_impl@0x01004284` (see
> [Get Sequence Bounds](../kernels/get-sequence-bounds.md), KIT idx14 `{0xbe → 0x01004204}`);
> idx15 `0xf2` → trampoline `0x0100484c` → `in_dtype`-branch →
> `nonzero_with_count_impl<int>@0x01004b80` / `<float>@0x01004940` (see
> [Nonzero With Count](../kernels/nonzero-with-count.md), KIT idx15 `{0xf2 → 0x0100484c}`). Both
> rows upgraded to HIGH. `[CORRECTION folded in place]`

> **GOTCHA — this is the per-IMAGE kernel set, not the universe of POOL opcodes.** The 17-entry
> CAYMAN table lists only the kernels **this image implements**. The DEBUG build references
> further decode handlers (`decode_embedding_update`, `decode_sb2sb_collective`, `decode_sort`,
> `dma_memcopy_impl`, `run_indirect_copy`, `decode_extended_inst_sb2sb`, `tensor_scalar_addr`)
> that route through **other** per-generation tables, not CAYMAN's 17-entry table; and newer
> SUNDA images add opcodes (`memset 0x49`, `embedding_update 0x79`, `gather 0x68`,
> `affine_select 0x92`, `dma_memcopy 0xb8`, …). The full cross-engine opcode inventory is the
> [Opcode Catalog Ledger](../kernels/opcode-catalog-ledger.md); per-kernel detail lives
> on the individual kernel pages. `[HIGH/OBSERVED for the per-image set; CARRIED for the wider
> opcode space]`

---

## 7. Two-level dispatch — opcode `0xf0` = extended-instruction sub-dispatch

Opcode `0xf0` has **five** `kernel_info_table` entries (idx 6–10) differing only in the spec
byte (`spec 0,1,2,4,3`). The DEBUG strings prove a **second-level** dispatcher routed to by the
`0xf0` rows:

```
"P%i: dispatch_extended_inst(%d) : num_chans = %0d"     ; the extended sub-dispatcher (%d = sub-code)
"P%i: UNKNOWN EXTENDED OPCODE=%d"                        ; its own miss path
```

and the per-variant Decode + run log lines confirm the sub-variants (all re-grepped this
session from `dbg_dram.bin`):

| spec | variant (DEBUG string) | resolution | conf |
|---|---|---|---|
| 1 | `ExtendedInstCopy` | funcVA `0x01003380` = `pool_extended_inst_copy` (EXACT `.xt.prop`) | HIGH |
| 2 | `ExtendedInstTensorTensorArith` | funcVA `0x01003484` = `decode_extended_inst_tensor_tensor_arith` | HIGH |
| 0/3/4 | `ExtendedInstRandGetState` / `ExtendedInstRandSetState` / `ExtendedInstCptcDecode` / `ExtendedInstEngineNop` | map to specs 0/3/4 (exact spec↔variant pairing not pinned) | MED |

So: the top-level scan matches opcode `0xf0` → routes into `dispatch_extended_inst`, which
sub-selects on the spec byte. This is exactly why the table groups five `0xf0` rows by spec. The
mechanism, the per-spec funcVAs, and the spec-1/spec-2 names are HIGH; the spec-0/3/4 variant
pairing is MED. Full treatment: [POOL Extended-Opcode (0xF0) Dispatch](pool-ext-0xf0.md).

> **NOTE — the `0xf0` spec byte is the only place spec ≠ 0 in this table.** Every other row has
> `spec = 0`; only `0xf0` multiplexes on spec. That is why the key is `(opcode<<24)\|(spec<<16)`
> and not just the opcode: the spec field exists to disambiguate the five `0xf0` variants. A
> reimplementation must include `spec` in the compare key even though 12 of 17 rows leave it
> zero. `[HIGH/OBSERVED]`

---

## 8. CPU-ID gating — `get_cpu_id()`

The device API is declared in the shipped header
`extracted/…/gpsimd/custom_op/neuron/neuron-utils.hpp`:

```c
/* Return the cpu_id of the processor caller is on, id is in range [0, get_cpu_count()) */
uint32_t get_cpu_id();
/* Return the total number of cpu available */
uint32_t get_cpu_count();
```

The dispatcher's trace `"P%i: In dispatch, CPU ID: %0d, got opcode 0x%x."` prints `get_cpu_id()`
as `%0d` and the per-core `'P%i'` index. Its role is a **work-partition gate**, not a
skip-dispatch gate: `get_cpu_id()` identifies *which* of the SPMD pool cores is running so each
core partitions the kernel work across its share of channels — every core runs the **same** scan
loop, tagged by its CPU ID, and each kernel slices its channel range by `(cpu_id, cpu_count)`.
The channel/pool partitioning shows up in the per-kernel traces: `"P%i: num_chans = %0d"`,
`"P%i: pool_num = %0d"`, `"P%i: TSA : pool_num = %0d"`, `"P%i: SB2SB_Collective: num_chans=%0d,
tpb_idx=%u"`, and an engine-identity probe `"P%i: engine_base_addr=%llx tpb_base_addr=%llx ->
is_tpb=%u is_die_0=%u engine_idx=%u"`.

> **GOTCHA — `get_cpu_id()` does NOT gate *whether* a core dispatches.** All pool cores execute
> the identical `kernel_info_table` scan for the handed opcode; the CPU ID selects the **data
> partition** (channel/pool subrange), not a code path. A reimplementation that branches "if
> `cpu_id != 0` skip dispatch" is wrong — every core runs the kernel on its slice. *(The API +
> the trace are HIGH/OBSERVED; the exact in-loop branch on `cpu_id` sits in the FLIX-desynced
> body → MED.)* `[HIGH api / MED in-loop use]`

---

## 9. The miss path — UNKNOWN OPCODE

Two string-anchored miss paths exist, each appearing **exactly once** as a record in
`dbg_dram.bin` (re-counted this session):

```
top-level scan exhausted        →  "P%i: UNKNOWN OPCODE=0x%x"          (1 record)
0xf0 extended sub-dispatch miss →  "P%i: UNKNOWN EXTENDED OPCODE=%d"   (1 record)
```

The top-level path formats the opcode as **hex** (`=0x%x`), consistent with the 8-bit `opcode`
field of the table; the extended-sub path formats the sub-code as **decimal** (`=%d`),
consistent with the small spec selector. The SEQ-side analogue is `"S: ErrorHandler : Bad
Opcode(0x%x)"`, which on SEQ routes to a hard-fault infinite spin
([dispatch-hub.md §7](../seq/dispatch-hub.md#7-the-default--error-path-bad-opcode--highobserved)).

> **CORRECTION — the POOL miss is string-anchored, but its *downstream policy* (log-and-continue
> vs hard-fault) is NOT byte-pinned here.** Unlike the SEQ engine — whose 123 default slots
> route to a single literal `0x3198` → `ErrorHandler` → `j 0x13e14` infinite self-loop, fully
> OBSERVED — the POOL miss handler's continuation sits in the FLIX-desynced dispatch body. What
> is HIGH/OBSERVED is **the miss string is emitted on a scan-exhaust**; whether the POOL core
> then spins, returns to the SEQ front-end, or continues to the next opcode is **MED/INFERRED**
> (it has its *own* POOL-local error path, distinct from the SEQ `0x3198`/`ErrorHandler` model).
> Do not assume the SEQ hard-fault policy applies on the POOL side. `[miss string HIGH/OBSERVED;
> continuation MED/INFERRED]`

---

## 10. Cross-image stability — CARRIED

The MARIANA_0 and MARIANA_PLUS_0 EXTISA_0 images carry the **same** 17-entry `kernel_info_table`
at the **same** VMA `0x02000380`, byte-identical `(opcode, spec)` key column; their `funcVA`s
differ only by the small build-offset delta. The dispatch routine (entry `0x01005610`, getters
`0x55f8`/`0x5607`, count compute `0x1641`–`0x1650`) is part of the same `0xa260` EXTISA_0 image
shared across CAYMAN / MARIANA / MARIANA_PLUS, so the loop flow described here is **stable across
those three generations**. The smaller per-engine sub-images (CAYMAN_1/2/3) each embed their own
*smaller* `kernel_info_table` (1 / 2 / 9 entries) at their own VMAs (`0x02000048` /
`0x02000070` / `0x020008c8`), with the same 8-byte format and the same dispatch mechanism. Full
layout detail: [kernel_info_table Binary Layout](kernel-info-table.md). `[HIGH/CARRIED
from SX-FW-14/§11, SX-FW-18 cross-image section]`

---

## 11. Reimplementation checklist

To rebuild the POOL per-core dispatch loop:

1. **Table**: a 17-record array of 8-byte entries
   `{u8 0; u8 0; u8 spec; u8 opcode; u32_le funcVA}` at VMA `0x02000380`, ending at
   `0x02000408` (`.globstruct` base). No sentinel — bound by `count = (end−base)>>3 = 17`.
   `funcVA` is `R_XTENSA_RELATIVE` (add the load bias). (§3, §6.)
2. **Key**: build `key = (opcode<<24) | (spec<<16)` — the native-LE u32 of the entry's first 4
   bytes (`opcode @ +3`, `spec @ +2`). Include `spec` even though only `0xf0` uses it (§7).
3. **Lookup**: **LINEAR SCAN** in registration order, comparing the packed key. The table is
   **not** sorted — do not binary-search, do not direct-index by opcode (§4). O(17).
4. **Hit**: read `funcVA` at `entry+4`, **`callx8 funcVA`** — one hop to the per-opcode kernel
   entry trampoline (`entry a1,32`, or `a1,48` for `0xf0` spec 4). The clean trampolines load a
   per-kernel `.bss` state-object pointer (`0x020004NN`) before running the kernel (§4a/§4b).
5. **CPU-ID**: every SPMD pool core runs the same loop; `get_cpu_id()` (`[0, get_cpu_count())`)
   selects the **channel/pool data partition**, never whether to dispatch (§8).
6. **Two-level `0xf0`**: route opcode `0xf0` into `dispatch_extended_inst`, sub-select on the
   spec byte (`1=Copy`, `2=TensorTensorArith`, `0/3/4=Rand/Cptc/EngineNop band`); sub-miss →
   `"P%i: UNKNOWN EXTENDED OPCODE=%d"` (§7).
7. **Miss**: scan exhaust → log `"P%i: UNKNOWN OPCODE=0x%x"`; the POOL-local continuation policy
   is its own (do **not** assume the SEQ `0x3198`/`ErrorHandler` hard-fault) (§9).
8. **Source**: the opcode is **handed in by the SEQ front-end** (register/decode slot); the POOL
   core does not self-fetch a ucode stream (§5). The DEBUG build keeps the `'P%i:'` strings; the
   PERF build strips all logging but runs the identical scan.

---

## 12. Adversarial self-verification

Five strongest claims, each re-challenged against the re-carved image this session (carve SHAs
match: PERF `internal_CAYMAN_0.so 910d41c3…`, DEBUG `dbg_dram.bin 226f4254…`):

| # | Claim | Challenge | Verdict |
|---|---|---|---|
| 1 | **Carved POOL object** = `.rodata` of `img_CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_contents.c.o`, sha256 `910d41c3…`, `e_entry 0x01005610` | re-carve `dd skip=0x60 len=0xa260`; `sha256sum`; `readelf -h` | **HOLDS.** sha256 `910d41c3ededce67…b4b55527`; entry `0x1005610`; Xtensa ELF32 LE EXEC. OBSERVED. |
| 2 | **`kernel_info_table` = 17 entries, 8-byte stride, @VMA `0x02000380`** (three independent ways) | `readelf -S` (`0x88/8`); `readelf -r` (17 RELATIVE at `base+8*i+4`); `(end−base)>>3` | **HOLDS.** `0x88/8 = 17`; **17** RELATIVE relocs, all at `base+8*i+4` for i=0..16; ends at `.globstruct 0x02000408`. OBSERVED. |
| 3 | **Key formula `(opcode<<24)\|(spec<<16)` == native-LE u32 of entry[0..4]** for all 17 | `struct.unpack('<I', entry[:4])` vs `(op<<24)\|(spec<<16)` | **HOLDS.** All 17 match (incl. the five `0xf0` rows: `0xf0000000, 0xf0010000, 0xf0020000, 0xf0040000, 0xf0030000`). OBSERVED. |
| 4 | **All 17 funcVAs land on `entry a1,N`** (16× `a1,32`, 1× `a1,48` @ entry 9) | read `text[funcVA]` first 3 bytes for all 17 | **HOLDS.** 16× `36 41 00` (`entry a1,32`); **1×** `36 61 00` (`entry a1,48`) at entry 9 (`0xf0` spec 4); 0 bad. OBSERVED. |
| 5 | **Miss path strings present**: `"P%i: UNKNOWN OPCODE=0x%x"` (top) / `"P%i: UNKNOWN EXTENDED OPCODE=%d"` (0xf0 sub) | `strings dbg_dram.bin \| rg 'P%i: UNKNOWN'` + record count | **HOLDS.** Both present, **1 record each**; the `'P%i:'` census is **156** records (`rg -c` == literal count, no multi-per-record packing). OBSERVED. |

> **CORRECTIONS folded in during self-verify.** (a) The backing report's stated route immediate
> for funcVA[3] (`const16 a2,0xbc0`) is the *deeper* decode-call; the byte-exact first constant
> is the **`.bss` state pointer `0x02000458`** (§4b CORRECTION). (b) The report's ARTIFACTS line
> labelled the SEQ `'S:'` image `dbg_dram.bin` (28,448 B); the authoritative `'P%i:'` POOL DEBUG
> payload is the `.rodata` of `img_CAYMAN_Q7_POOL_DEBUG_DRAM_contents.c.o` (89,344 B,
> `226f4254…`) — the string *content* matches the report exactly; only the file-offset frame
> differs. (c) The expected string `"P%i: Performing dynamic dispatch for 0x%x"` does **not**
> exist (re-grepped → 0 hits); the real per-opcode trace is
> `"P%i: In dispatch, CPU ID: %0d, got opcode 0x%x."` — the firmware never prints the word
> "dynamic".

---

## 13. Honesty ledger

**HIGH / OBSERVED (re-run this session):**

- Carve reproduced: PERF `internal_CAYMAN_0.so` 41,568 B / sha256 `910d41c3…b4b55527`, entry
  `0x01005610` (`entry a1,32`, bytes `36 41 00`); DEBUG `dbg_dram.bin` 89,344 B / sha256
  `226f4254…f6f0128e`.
- `kernel_info_table` geometry: VMA `0x02000380`, file off `0x7400`, size `0x88`, 8-byte stride,
  17 entries, ending at `.globstruct 0x02000408`; no sentinel. Count confirmed three ways
  (size/stride, 17 RELATIVE relocs at `base+8*i+4`, `(end−base)>>3`).
- Record format `{u8 0; u8 0; u8 spec(+2); u8 opcode(+3); u32_le funcVA(+4)}`; key
  `(opcode<<24)\|(spec<<16)` verified == native-LE u32 of entry[0..4] for all 17.
- All 17 funcVAs validated as real `entry a1,N` prologues (16× `a1,32`, 1× `a1,48`).
- Five trampolines decode cleanly to `entry a1,32 ; const16 a2,0x200 ; const16 a2,0x4NN` → `.bss`
  state pointers (`0x458/0x45c/0x468/0x47c/0x480`, all in `.bss 0x02000450..0x0200048c`).
- DEBUG `'P%i:'` strings: 156 records; `"P%i: Entering Dispatch"`, `"P%i: Exiting Dispatch"`,
  `"P%i: In dispatch, CPU ID: %0d, got opcode 0x%x."`, `"P%i: dispatch : modify_pool_config"`,
  `"P%i: dispatch_extended_inst(%d) : num_chans = %0d"`, `"P%i: UNKNOWN OPCODE=0x%x"`,
  `"P%i: UNKNOWN EXTENDED OPCODE=%d"`, `dispatch` + `dispatch.hpp` — each present (counts in §1).
- `get_cpu_id()` / `get_cpu_count()` declared in shipped `neuron-utils.hpp` (`[0,
  get_cpu_count())`).
- Two-level `0xf0`: five spec-keyed rows; DEBUG `ExtendedInst{Copy,TensorTensorArith,
  RandGetState,RandSetState,CptcDecode,EngineNop}` strings; spec-1/spec-2 funcVAs name-resolved.

**MED / INFERRED:**

- The exact per-entry compare-and-branch and the `callx8`-on-hit bundle are in a property-
  uncovered, literal-pool-interleaved FLIX span (`~0x01005653..0x010056f6`) that the linear
  sweep desyncs (proven: an embedded ELF-magic literal mis-renders as a bundle; spurious call
  targets fall outside `.text`). The loop *structure* (linear scan + `callx8`) is HIGH; the
  byte-exact loop encoding is MED.
- The opcode SOURCE register/slot (SEQ→POOL handoff direction is HIGH; the exact MMIO/register
  name is not pinned).
- The POOL miss continuation policy (string is HIGH; spin-vs-return-vs-continue is INFERRED).
- `0xf0` spec-0/3/4 variant↔name pairing; the deeper trampoline route targets past the desync;
  the `.bss` state-object boot construction.

**LOW / UNRECOVERED:**

- op `0x52` (82): CAYMAN-specific, no SUNDA/DEBUG name pin — funcVA OBSERVED, kernel name LOW.
  *(op `0xbe` (190), formerly listed here, is now resolved to `get_sequence_bounds_impl` — see §6
  CORRECTION and [Get Sequence Bounds](../kernels/get-sequence-bounds.md).)*

---

## See also

- [SEQ Decode / Dispatch Hub](../seq/dispatch-hub.md) — the contrasting SEQ front-end dispatch
  (178-entry direct-indexed table → `Handler::execute()`); §9 there pins the SEQ↔POOL contrast.
- [kernel_info_table Binary Layout](kernel-info-table.md) — the
  POOL table's full binary layout (record fields, relocations, cross-image VMAs).
- [POOL Extended-Opcode (0xF0) Dispatch](pool-ext-0xf0.md) — the
  two-level `0xf0` extended-instruction sub-dispatcher (§7).
- [The Opcode Catalog Ledger](../kernels/opcode-catalog-ledger.md) —
  the cross-engine opcode inventory the §6 map feeds.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the
  OBSERVED/INFERRED/CARRIED × HIGH/MED/LOW tagging used throughout.
