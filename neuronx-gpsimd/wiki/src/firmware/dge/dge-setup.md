# DGE Setup + Context Init

This page reconstructs the **device-side setup / context-init path of the DGE
(Descriptor-Generation Engine)** in the Q7/POOL GPSIMD firmware — the layer that turns a
high-level custom-op tensor-copy / collective descriptor (src/dst base, 4-dim shape, dtype,
cast, indirection, reshape kind) into the low-level **SDMA descriptor rings** the RDM/UDMA
hardware executes. It opens the DGE descriptor-generation sub-lane: this page covers the
context allocation/binding and the per-descriptor *decode* (`dge_decode_fast`), then hands
off, at a named call site, to the [3-backend selector](dge-backend-selector.md). The
reshape, emit, and error stages are documented on their own pages.

Everything here is **byte-pinned to shipped artifacts disassembled this session** — the
DEBUG POOL firmware images carved out of `libnrtucode.a` and decoded with the native
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, `ConfigName=Xm_ncore2gp`, uarch "Cairo",
`TargetHWVersion=NX1.1.4`, `IsaMaxInstructionSize=32` FLIX/VLIW). Every log line is resolved
against the firmware's own DRAM string image; every symbol/field name is recovered from the
DEBUG build's own embedded format strings (`S:` / `P%i:`), its `__FILE__` literals
(`dge_decode_fast.cpp`, `dge_backend_rtl.cpp`, `dge_reshape.cpp`), and the
`.xt.prop.<mangled>` section names of the sibling EXTISA kernels. Where a sibling note or an
earlier reading disagrees with the disassembly, **the binary wins**, and an in-place
**CORRECTION** says so.

Confidence tags follow the [project model](../../reference/confidence-model.md): `OBSERVED`
= a byte/string/instruction read from a shipped artifact this session; `INFERRED` = reasoned
over OBSERVED facts; `CARRIED` = consolidated from a cited cross-page anchor; crossed with
`HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE** (orientation).

> **NOTE — DGE vs. the legacy iDMA path.** This page documents the *modern descriptor-program
> engine*. The Q7/POOL firmware also carries the **legacy single-transfer DMA** — the
> `DramRingDMA` allocator (string `S: DramRingDMA::allocate(%zu) submit=%llu comp=%llu
> first_slot=%zu min_comp=%llu`, byte-read in the `CAYMAN_NX_POOL` DEBUG DRAM) and the iDMA
> channel layer (`P%i: iDMA channel %d failed to initialize!`, `P%i: iDMA buffer error: …
> descriptor: %d, src addr … dst addr …`). That path moves **one** transfer through one ring
> slot; it is documented in [iDMA / Legacy DMA](idma-legacy-dma.md). The DGE, by contrast,
> *generates a program* of descriptors (GENERATE / DIMPUSH / REGWRITE pushes) into a 4-deep
> SDMA BD ring and rings a doorbell. Keep the contrast in mind: **iDMA = single BD; DGE =
> descriptor program.** `[HIGH/OBSERVED]` for both string sets; the "single vs program"
> framing is `[HIGH/INFERRED]`.

---

## 0. The setup → decode spine in one diagram

```
  ┌──────────────────── DGE setup + per-descriptor decode (Q7/POOL) ───────────────────┐
  │                                                                                     │
  │  setup_DGE_context()                       (dge_decode_fast.cpp)                    │
  │    ├─ "S: Setting up DGE"                                                           │
  │    ├─ allocate / zero per-context state in BSS  (SRAM/EXTRAM carve == empty)        │
  │    ├─ "S: DGE contexts 0x%x/0x%x init sw=%d engine=%u queue_idx=%u queue_num=%u"    │
  │    │        └─ BIND a (tx-ctx, rx-ctx) pair → engine + UDMA queue slot, sw/hw flag  │
  │    └─ "S: rx.base=0x%llx, tx.base=0x%llx"  ; the S2M(rx)+M2S(tx) ring bases         │
  │            │                                                                        │
  │            ▼                                                                        │
  │  dge_decode_fast(kind)        entry a1,48  @IRAM 0x3394   (Q7 image)                │
  │    ├─ rsr.prid → "P%i:" prefix arg                                                  │
  │    ├─ computed-goto dispatch:  const16 a4,0x87c ; addx4 a3 ; l32i.n a3 ; jx a3      │
  │    │    (+ beq a2,a3 case-chain), one arm per decode KIND:                          │
  │    │      ├─ DIRECT2D  @0x3538  → log "P%i: DGE DIRECT2D"  → call8 0x135d4 (builder) │
  │    │      ├─ GATHER T. @0x3566  → log "DGE GATHER TRANSPOSE" → call8 0x13c5c         │
  │    │      └─ INDIRECT  @0x36a3  → log "DGE INDIRECT"        → call8 0x13c4c          │
  │    ├─ each builder fills dge_shape[i] = { num[4], step[4] }  (the 4-dim IR)         │
  │    └─ j 0x3713  (common exit)                                                       │
  │            │                                                                        │
  │            ▼   reshape / spray  (analyze_tensor_reshape, gen_spray_info)            │
  │            ▼   *** hand-off → SELECT BACKEND  (Pool | RTL | software | none) ***     │
  │                  const16 a4,0x5fe0 ; l32i.n a3,a4,0 ; beqz a3 → next arm            │
  │                  → "S: DGE: Select backend Pool" / "… RTL" / "… NO BACKEND FOUND"   │
  └─────────────────────────────────────────────────────────────────────────────────────┘
```

One-line verdict: setup **binds a context-pair to an engine + UDMA queue and records the
RX/TX ring bases**; `dge_decode_fast` then **classifies each descriptor into one of three
KINDs**, lowers it into the engine's internal 4-dim `dge_shape` representation, and dispatches
into a backend selector. The selector body and the descriptor-emit ops are downstream pages;
this page nails the setup, the decode, and the exact hand-off point.

---

## 1. Image identification + addressing model

The DGE is **not** in the per-opcode EXTISA micro-kernel ELFs. Those are the POOL *compute*
kernels: a census of the `CAYMAN_Q7_POOL_PERF_EXTISA_0` SO (carved this session, `.rodata`
`.bin` sha256 `910d41c3…`, **41568 B**, a 32-bit Tensilica-Xtensa ELF) shows **21**
`.xt.prop.<mangled>` property sections, all compute symbols and **zero** `dge_*`:

| `.xt.prop` section | demangled (`c++filt`) |
|---|---|
| `.xt.prop._Z9iota_implILb1EEvv` | `void iota_impl<true>()` |
| `.xt.prop._Z28pool_cross_lane_reduce_arithv` | `pool_cross_lane_reduce_arith()` |
| `.xt.prop._Z11decode_poolb` | `decode_pool(bool)` |
| `.xt.prop._Z26decode_tensor_tensor_arithj` | `decode_tensor_tensor_arith(unsigned int)` |

> **NOTE — `.xt.prop` is a citeable, binary-derived name source.** The EXTISA ELFs retain
> their Tensilica **property tables** (`readelf -SW … | rg '\.xt\.prop\.'`), whose section
> names embed the *mangled C++ symbol* of each shipped function. Demangling them recovers the
> real source-level names without any external reference — this is how "no DGE symbol lives in
> the EXTISA kernels" is a *positive, byte-exact* finding (21 sections, 0 `dge_`), and how the
> compute-kernel names above are grounded. `[HIGH/OBSERVED]`

The DGE lives in the **main per-engine POOL firmware images** inside the static archive

```
extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/
  gpsimd/custom_op/c10/lib/libnrtucode.a
```

as members `img_<GEN>_<NX|Q7>_POOL_<MODE>_<SEG>_contents.c.o`. Each `.c.o` is an **x86-64 ELF
relocatable** whose single `.rodata` PROGBITS holds the raw device IRAM/DRAM image; the carve
is `ar p <member> | objcopy -O binary --only-section=.rodata`. Three variants carry the DGE,
with **complementary** string sets; all three were re-carved and sha-confirmed this session:

| Member (DEBUG) | Seg | `.rodata` size | sha256 (prefix) | Role here |
|---|---|---|---|---|
| `img_CAYMAN_Q7_POOL_DEBUG_IRAM` | IRAM | 125504 B (`0x1ea00`) | `513a8a22d94b08c2` | **trace image**: `dge_decode_fast` 3-kind decode + RDM tail-ptr |
| `img_CAYMAN_Q7_POOL_DEBUG_DRAM` | DRAM | 89344 B | `226f4254d4751903` | Q7 string image (`P%i:` corpus) |
| `img_MARIANA_PLUS_NX_POOL_DEBUG_IRAM` | IRAM | 119616 B | `9b514bb6d45a7363` | cleanest backend-select arm; carries `dge_decode_fast.cpp` `__FILE__` |
| `img_MARIANA_PLUS_NX_POOL_DEBUG_DRAM` | DRAM | 29024 B | `d2e1552a13f1efe3` | NX string image + the `dge_decode_fast.cpp` literal |
| `img_CAYMAN_NX_POOL_DEBUG_IRAM` | IRAM | 116768 B | `8e4412b99201f62d` | `dge_shape` dump anchor (`@0xac81`) |
| `img_CAYMAN_NX_POOL_DEBUG_DRAM` | DRAM | 28448 B | `7bdf6ed7ccd27b37` | NX setup/context string corpus |

**Chosen trace image for the decode path: `CAYMAN_Q7_POOL_DEBUG_IRAM`** (sha `513a8a22…`); the
`CAYMAN_NX_POOL` DEBUG pair supplies the setup/context strings and the `dge_shape` dump anchor;
`MARIANA_PLUS_NX_POOL` supplies the cleanest backend-select arm and the `__FILE__` proof. All
sha256/size figures above are **re-confirmed** (`objcopy` carve → `stat -c%s` → `sha256sum`).
`[HIGH/OBSERVED]`

**Addressing rules.** IRAM loads at device VA `0x0` (reset at byte 0); IRAM addresses below
are file-offset == VA. The DRAM image loads at device VA `0x80000`, so **DRAM-string VA =
file-offset + 0x80000** (every string VA below uses this). `[HIGH/OBSERVED, carried from the
SEQ boot addressing model]`

> **NOTE — `SRAM`/`EXTRAM` members carve to zero bytes.** In all three variants the
> `SRAM`/`EXTRAM` segment members hold an empty `.rodata` (BSS, runtime-zeroed). The DGE
> *context object* and the working `dge_shape` state are therefore **allocated and zeroed in
> device SRAM/dataram at boot/first-use**, not present in any shipped image. The struct field
> offsets below are read from the code that *touches* that runtime object (loads/stores at
> fixed displacements), not from a static initializer. `[HIGH/OBSERVED]` for the BSS emptiness;
> `[HIGH/INFERRED]` for "context lives in runtime BSS".

> **GOTCHA — the FLIX/VLIW desync ceiling.** These are linked, densely-scheduled FLIX bundles
> (up to 32-byte) with literal pools interleaved in `.text` and **no `.symtab`** (unlike the
> EXTISA ELFs). `objdump`'s linear sweep decodes recognisable bundles and scalar runs
> correctly but **loses bundle sync across literal / selector-byte boundaries**, rendering
> those spans as `.byte` with bogus `l32r` targets. Consequently: the **format-string corpus
> is ground truth** (read byte-exact from `.rodata`); function **`entry aN,N` prologues are
> reliable anchors**; the case idioms and the tail-ptr writeback **decode cleanly at their
> anchors** (forced `--start-address=`); but **deep struct-field offsets inside desynced bodies
> are MED/LOW**. This is the corpus-wide MED ceiling — see
> [the confidence model](../../reference/confidence-model.md). It is documented, not hidden.

---

## 2. The DGE format-string corpus — the structural ground truth

The DEBUG `.rodata` strings *encode* the struct fields, the algorithm stages, and the SDMA
descriptor layout. They are the single most reliable artifact. All VAs below are
byte-confirmed this session (`dd … | tr -c '[:print:]'`).

**Setup / context (NX variant, VA = DRAM off + 0x80000):**

| VA | String (byte-exact) |
|---|---|
| `0x8388e` | `S: Setting up DGE` |
| `0x834b0` | `S: DGE contexts 0x%x/0x%x init sw=%d engine=%u queue_idx=%u queue_num=%u` |
| `0x834fa` | `S: rx.base=0x%llx, tx.base=0x%llx` |
| `0x8351d` | `dge_ctx_num` (assert/`__FILE__`-adjacent tag) |
| `0x834d3`\* | `dge_decode_fast.cpp` (\*`MARIANA_PLUS_NX` off `0x34d3`; the init `__FILE__`) |

**Decode KINDs (Q7 variant):**

| VA | String |
|---|---|
| `0x80e0c` | `P%i: DGE DIRECT2D` |
| `0x80e1f` | `P%i: DGE INDIRECT` |
| `0x80e32` | `P%i: DGE GATHER TRANSPOSE` |

**Internal representation (NX variant):**

| VA | String |
|---|---|
| `0x82455` | `S: dge_shape[%i].step = [ 0x%04x, 0x%04x, 0x%04x, 0x%04x ]` |
| `0x82491` | `S: dge_shape[%i].num  = [ 0x%04x, 0x%04x, 0x%04x, 0x%04x ]` |

**Selector + emit (NX variant), shown here only to mark the boundary (bodies are downstream):**

| VA | String |
|---|---|
| `0x83157` | `S: DGE: Select backend Pool` (`MARIANA_PLUS` VA `0x83197`) |
| `0x83173` | `S: DGE: Select backend RTL` |
| `0x8312f` | `S: DGE: NO BACKEND FOUND, doing nothing` |
| `0x837ea` | `S: push DIMPUSH to DMA[%d]: [%u,%u][%d,%d]` |
| `0x83821` | `S: push GENERATE to DMA[%d]: %s : addr=0x%llx, elem_size=%d, sem_num=%i` |
| `0x83870` | `S: push REGWRITE to DMA[%d]` |

The `%s` direction tag in `push GENERATE` resolves to the `RD` / `WR` literals sitting
immediately after the format string in `.rodata` — the read (M2S) vs write (S2M) direction of
the generated BD. `[HIGH/OBSERVED]`

> **NOTE — string-prefix engine identity.** The NX/SEQ-engine DGE strings are prefixed `S:`;
> the Q7/POOL-engine DGE strings are prefixed `P%i:` (the `%i` is the processor id, set from
> `rsr.prid`). The two are complementary views of the *same* engine: counted by
> `strings -a <DRAM>.bin | rg -c`, the `CAYMAN_NX_POOL` DEBUG DRAM holds **187** `S:` lines
> and **0** `P%i:`; the `CAYMAN_Q7_POOL` DEBUG DRAM holds **156** `P%i:` lines and **0** `S:`;
> the NX DRAM carries **8** `dge_` strings. (Metric: `grep -c` over `strings -a` output.)
> `[HIGH/OBSERVED]`

---

## 3. The DGE context-init — what setup builds

### 3.1 What `init` binds (annotated reconstruction)

The init log line is the spec. From `S: DGE contexts 0x%x/0x%x init sw=%d engine=%u
queue_idx=%u queue_num=%u` immediately followed by `S: rx.base=0x%llx, tx.base=0x%llx`, the
setup binds **a context *pair*** to an engine + a UDMA queue slot, and records the two ring
bases. Reconstructed as annotated C pseudocode (names from the format strings; offsets where a
clean scalar run gives them, else marked):

```c
/* dge_decode_fast.cpp : setup_DGE_context()  — reconstructed from the init log line     */
/* The object lives in runtime BSS (SRAM/EXTRAM carve == empty); offsets below are from   */
/* the code that touches it, not a static initializer.                                    */

struct dge_context {                    /* [HIGH strings / MED layout] */
    /* ---- the (tx,rx) context-pair identity ---- */
    uint32_t  rx_ctx_id;                /* "0x%x/0x%x" first  — RX = S2M / write (HBM<-local) */
    uint32_t  tx_ctx_id;                /*            second  — TX = M2S / read  (local<-HBM)  */

    /* ---- engine + UDMA queue binding ---- */
    int32_t   sw;                       /* sw=%d   : software-walk vs hardware-DMA backend flag */
    uint32_t  engine;                   /* engine=%u : which DMA engine instance this binds to   */
    uint32_t  queue_idx;               /* queue_idx=%u : this ctx's slot ...                     */
    uint32_t  queue_num;               /* queue_num=%u : ... within a pool of queue_num queues   */

    /* ---- the two descriptor-ring bases (printed as u64) ---- */
    uint64_t  rx_base;                 /* "rx.base=0x%llx" : S2M (write) BD-ring base */
    uint64_t  tx_base;                 /* "tx.base=0x%llx" : M2S (read)  BD-ring base */

    /* ---- working state ---- */
    uint8_t   n_dge_shape;             /* per-context shape count, at struct +0xd (see §5)   */
    /* dge_shape[n] = { num[4], step[4] } ... (the 4-dim IR, filled by the decode, §5)    */
};

void setup_DGE_context(dge_context *c, /* binding params */)
{
    LOG("S: Setting up DGE");
    memset(c, 0, sizeof *c);                          /* zero the BSS-resident object   */
    /* bind the pair to an engine + a UDMA queue slot, record the sw/hw flag */
    c->rx_ctx_id = ...; c->tx_ctx_id = ...;
    c->sw = ...; c->engine = ...; c->queue_idx = ...; c->queue_num = ...;
    LOG("S: DGE contexts 0x%x/0x%x init sw=%d engine=%u queue_idx=%u queue_num=%u",
        c->rx_ctx_id, c->tx_ctx_id, c->sw, c->engine, c->queue_idx, c->queue_num);
    /* record the RX(S2M) and TX(M2S) ring bases the context owns */
    c->rx_base = ...; c->tx_base = ...;
    LOG("S: rx.base=0x%llx, tx.base=0x%llx", c->rx_base, c->tx_base);
}
```

Field-by-field, with grounding:

| Field | From | Meaning | Tag |
|---|---|---|---|
| `0x%x/0x%x` (pair) | init str | TWO context ids — a context **pair**; the `rx.base`/`tx.base` pairing in the next line identifies them as RX (S2M / write) and TX (M2S / read). | id pair `[HIGH/OBSERVED]`; rx/tx labelling `[HIGH/INFERRED]` |
| `sw=%d` | init str | software-vs-hardware backend flag: does the DGE walk the copy itself (sw) or program the RTL/Pool HW path (hw)? | `[HIGH/INFERRED]` |
| `engine=%u` | init str | which DMA engine instance this context binds to. | `[HIGH/OBSERVED]` |
| `queue_idx` / `queue_num` | init str | the UDMA **queue binding**: `queue_idx` is this context's slot within a pool of `queue_num` queues. | `[HIGH/OBSERVED]` |
| `rx.base` / `tx.base` (u64) | rx/tx str | SoC base addresses of the RX (S2M) and TX (M2S) descriptor rings the context owns. | `[HIGH/OBSERVED]` |

> **QUIRK — one *pair* of contexts per descriptor stream, not one.** The init prints **two**
> context ids and then **two** ring bases. A single tensor copy needs both a *read* path (M2S:
> pull source out of HBM into local) and a *write* path (S2M: push result from local to HBM),
> so the DGE always sets up a `(tx-ctx, rx-ctx)` pair, each owning its own BD ring. The TX/RX
> two-party `left_pop`/`right_push` handshake in the RDM start stage (§7) is the runtime
> consequence of this pairing. `[HIGH/INFERRED]`

### 3.2 Reconciliation with the SDMA staging context (the ring it manages)

The DGE context is the **higher-level wrapper** around the per-direction SDMA staging context
(the `_dma_ctx_t` built in the local dataram window). The mapping, from the field semantics:

- `rx_base` / `tx_base` → the S2M / M2S **BD-ring bases** of the staging context.
- `engine` + `queue_idx`/`queue_num` → the per-queue **tail-increment MMIO doorbells** (M2S /
  S2M tail-inc registers) the staging context owns.
- the **4-deep ring + wrap-at-4 cursor** of the staging context is the same ring the DGE fills
  via `push GENERATE` (§7) and bumps via the tail-ptr store (§7).

`[HIGH/INFERRED]` — the field-set lines up one-to-one with the SDMA staging layout; the exact
binary identity of those staging offsets is a cross-page anchor, not re-decoded on this page.

### 3.3 Host-side configuration of the context (orientation)

Before the device DGE runs, the **host runtime** configures it through the `nrtucode_core_t`
API (x86-64 functions in the host twin library), writing into the per-Q7-core control DRAM
window: a **priority-class map** (up to a small array of u32 class values, one per
descriptor-gen class — the device DGE consults it to arbitrate competing descriptor-gen
streams so a higher-priority custom-op DMA is serviced first) and a **4×u32 mailbox** for
in-band host↔Q7 DGE control signalling. These are host-side control structures in the control
DRAM window — **not** in the device firmware images carved here — so this page only orients;
the `engine`/`queue` binding above is the *device-side* consequence of that host config.
`[HIGH/CARRIED]` from the host-side analysis.

---

## 4. `dge_decode_fast` — the 3-KIND descriptor decode

This is the per-descriptor decode: it reads the high-level descriptor, classifies its **KIND**,
logs it through the common `P%i:` logger, and dispatches to the kind-specific descriptor
builder. The function is **`entry a1, 48` @IRAM `0x3394`** in the trace image (byte-confirmed
this session). The `__FILE__` literal `dge_decode_fast.cpp` (byte-read in the
`MARIANA_PLUS_NX` DRAM at off `0x34d3`) is the source file. `[HIGH/OBSERVED]`

### 4.1 The dispatch mechanism

After building the `P%i:` prefix arg (`rsr.prid`), `dge_decode_fast` selects the case through a
**computed-goto jump table** — a clean scalar run at `0x340a` (byte-confirmed):

```asm
; dge_decode_fast dispatch — CAYMAN_Q7_POOL_DEBUG IRAM, byte-exact
3394:  366100   entry   a1, 48            ; function prologue (the reliable anchor)
3397:  240900   const16 a2, 9             ; (decode-kind / tag setup; FLIX-adjacent)
 ...
340a:  447c08   const16 a4, 0x87c         ; a4 = jump-table base seed
340d:  4033a0   addx4   a3, a3, a4        ; a3 = base + 4*index   (scaled by 4)
3410:  3803     l32i.n  a3, a3, 0         ; a3 = table[index]     (the case target)
3412:  a00300   jx      a3                ; *** computed goto to the KIND builder ***
```

> **CORRECTION — the decode-fast dispatcher is a jump *table*, with a `beq` chain as the
> secondary path.** A naive reading (and an earlier note) described the KIND selection purely
> as a chained `beq a2,a3` ladder. The disassembly shows the **primary** mechanism is the
> `addx4`/`l32i.n`/`jx a3` computed-goto table at `0x340a` (the `beq a2,a3` comparisons at
> `0x34e3`/`0x3560`/`0x369d` route the *tail* cases). Both coexist. The table **base** is *not*
> cleanly byte-recoverable: `const16 a4,0x87c` loads `0x87c` as a 16-bit immediate, but the
> 8 words at file-offset `0x87c` are all-zero (`dd … | od -tx4`), so the effective base is
> formed across a FLIX-desynced span (a high-half `const16` or an added pointer). The **jump
> mechanism is OBSERVED**; the **literal table base and the per-case index constants are MED**
> (FLIX desync) — exactly the corpus-wide ceiling. `[HIGH/OBSERVED]` for the `jx a3` idiom;
> `[MED/INFERRED]` for the resolved table base.

### 4.2 The three case idioms (byte-exact)

Each KIND arm is the **identical** idiom: stage the `P%i:` prefix (`const16 a10,8`; `rsr.prid
a11`), `const16` the KIND format-string id, `call8` the common logger `@0x18a2c`, `call8` the
kind-specific builder, then `j 0x3713` (common exit). All three decode cleanly at their anchors
and were re-disassembled this session:

```asm
; --- DIRECT2D case @0x3538 ---
3538:  a40800   const16 a10, 8
353b:  b0eb03   rsr.prid a11               ; a11 = processor id  (-> %i)
353e:  a40c0e   const16 a10, 0xe0c         ; -> VA 0x80e0c "P%i: DGE DIRECT2D"
3541:  a54e15   call8   0x18a2c            ; the P%i: logger
3544:  e50810   call8   0x135d4            ; *** DIRECT2D descriptor builder ***
3547:  067200   j       0x3713            ; common exit

; --- GATHER TRANSPOSE case @0x3566 ---
3566:  a40800   const16 a10, 8
3569:  b0eb03   rsr.prid a11
356c:  a4320e   const16 a10, 0xe32         ; -> VA 0x80e32 "P%i: DGE GATHER TRANSPOSE"
356f:  e54b15   call8   0x18a2c
3572:  a56e10   call8   0x13c5c            ; *** GATHER-TRANSPOSE builder ***
3575:  866600   j       0x3713

; --- INDIRECT case @0x36a3 ---
36a3:  a40800   const16 a10, 8
36a6:  b0eb03   rsr.prid a11
36a9:  a41f0e   const16 a10, 0xe1f         ; -> VA 0x80e1f "P%i: DGE INDIRECT"
36ac:  e53715   call8   0x18a2c
36af:  e55910   call8   0x13c4c            ; *** INDIRECT builder ***
36b2:  461700   j       0x3713
```

Each `const16 a10,<id>` target is byte-confirmed against the DRAM string image:
`0xe0c`→`DGE DIRECT2D`, `0xe32`→`DGE GATHER TRANSPOSE`, `0xe1f`→`DGE INDIRECT`. The common
logger `@0x18a2c` is the variadic `P%i:` printf (own `entry` prologue; spills format args and
tail-calls a `vsnprintf` helper). `[HIGH/OBSERVED]`

### 4.3 The three KINDs = the three descriptor shapes

| KIND | Builder | Descriptor shape | Tag |
|---|---|---|---|
| **DIRECT2D** | `call8 0x135d4` | a plain **2-D strided** tensor copy (matches the 2-dim src/dst of the Pool/software backend log). | `[HIGH/INFERRED]` |
| **GATHER TRANSPOSE** | `call8 0x13c5c` | a **gather + on-the-fly transpose** (`tensor_reshape_transpose` + `make_gather_pattern`; the 5-dim RTL descriptor). | `[HIGH/INFERRED]` |
| **INDIRECT** | `call8 0x13c4c` | an **indexed (indirection) copy** (`gather_indices` / `do_indirection`; drives the `indirection_dim` field of the software-backend log). | `[HIGH/INFERRED]` |

> **NOTE — the DGE KINDs are the DMA-descriptor-gen subset.** The Q7 image carries other
> decode paths alongside these (e.g. `Sbuf2Sbuf` / `SB2SB_Collective`, `run_indirect_copy`,
> `ExtendedInstRandGetState`, all byte-read in the `P%i:` corpus). The three KINDs above are
> specifically the ones that *generate SDMA descriptors*; the builders at `0x135d4` / `0x13c5c`
> / `0x13c4c` are the entry points the [backend selector](dge-backend-selector.md) page decodes.

---

## 5. The internal representation — `dge_shape[i] = { num[4], step[4] }`

The builders lower each tensor into the DGE's **4-dimensional** internal shape, dumped by two
DEBUG strings (each printing **four** `0x%04x` → four 16-bit fields):

```
dge_shape[i].num  = [ n0, n1, n2, n3 ]   ; per-dim element counts, u16 each
dge_shape[i].step = [ s0, s1, s2, s3 ]   ; per-dim strides,        u16 each
```

The decode walks the high-level descriptor (src/dst base, shape, strides, dtype) and fills
`dge_shape[src]` and `dge_shape[dst]`; the dtype cast pair travels as `cast:0x%x->0x%x` in the
backend log. The dump code in the `CAYMAN_NX_POOL` IRAM `@0xac81` decodes cleanly
(byte-confirmed this session):

```asm
ac81:  240800   const16 a2, 8
ac84:  24c223   const16 a2, 0x23c2     ; -> VA 0x823c2 "dge_shape"
ac87:  29a1     s32i.n  a2, a1, 40
ac89:  2871     l32i.n  a2, a1, 28     ; a2 = ctx pointer (off +28 on stack)
ac8b:  22020d   l8ui    a2, a2, 13     ; a2 = ctx->n_dge_shape  (struct +0xd)
ac8e:  8ce2     beqz.n  a2, 0xaca0     ; zero shapes -> skip
 ...
ac99:  a65203   blti    a2, 5, 0xaca0  ; bound the loop  < 5
```

So the dump iterates a per-context **count field at struct `+0xd`**, **bounded `< 5`** (up to
~4–5 `dge_shape` slots per context). `[HIGH/OBSERVED]` for the `+0xd` offset and the `< 5`
bound (clean scalar run); `[INFERRED]` for "`< 5` == #shapes-per-context".

> **CORRECTION — the `< 5` bound is the `blti` at `0xac99`, not `0xac8e`.** An earlier reading
> attributed `blti a2,5` to `0xac8e`. The re-disassembly shows `0xac8e` is `beqz.n a2,0xaca0`
> (the zero-shape early-out); the `blti a2,5` is the loop bound at **`0xac99`**. The semantics
> are unchanged (count at `+0xd`, capped below 5); the address attribution is corrected.
> `[HIGH/OBSERVED]`

> **GOTCHA — 16-bit shape fields cap the per-dim extent.** `num[]`/`step[]` are printed as
> `0x%04x`, i.e. **u16** each. A reimplementer must not assume 32-bit dims here: a per-dim
> element count or stride that overflows 16 bits cannot be represented in a single `dge_shape`
> dim and must be tiled/decomposed by the reshape stage (the
> `Min Num Partitions` / `#DMA` split). `[HIGH/OBSERVED]` (the `0x%04x` width); the
> overflow-handling consequence is `[HIGH/INFERRED]`.

The `num[4]`/`step[4]` pairs are exactly what the descriptor-emit `push DIMPUSH to DMA[%d]:
[%u,%u][%d,%d]` op consumes — one DIMPUSH per dimension turns the 4-dim `dge_shape` into the
BD's nested multi-dim walk (see [emit](dge-emit.md) and Part 9 below).

---

## 6. The hand-off to the backend selector

After decode (and reshape), the DGE picks **one** of three backends — or none. This page does
**not** re-derive the selector; it pins the **hand-off point** so the
[backend-selector page](dge-backend-selector.md) starts where this one ends.

The selector entry is a **backend-availability-table read** from a fixed BSS global, decoded
cleanest in the `MARIANA_PLUS_NX_POOL` IRAM `@0xf9b0` (byte-confirmed this session):

```asm
; backend-select — Pool arm — MARIANA_PLUS_NX_POOL_DEBUG IRAM, byte-exact
f9b0:  44e05f   const16 a4, 0x5fe0        ; a4 = &backend-availability table (BSS global)
f9b3:  a49731   const16 a10, 0x3197       ; -> VA 0x83197 "S: DGE: Select backend Pool"
f9b6:  0c05     movi.n  a5, 0
f9b8:  3804     l32i.n  a3, a4, 0         ; a3 = backend[Pool].available
f9ba:  16f307   beqz    a3, 0xfa3d        ; if !available -> next arm (RTL / NO-BACKEND)
f9bd:  25d309   call8   0x196f0           ; (Pool-backend descriptor build path)
```

The selector reads a per-backend availability flag from the BSS table at `0x5fe0`, logs the
chosen backend, and falls through `Pool → RTL → NO BACKEND FOUND, doing nothing` on
unavailability. The three backends, named by the backend log lines:

| Backend | Descriptor | Path |
|---|---|---|
| **Pool** | 2-dim | runs the copy through the POOL engine path (the `DGE $S[…][2dim]` log). |
| **RTL** (`dge_backend_rtl.cpp`) | 5-dim + 2 | programs the hardware SDMA RTL path (the `RTL backend …[5dim\|2]` log); the priority-class-aware HW path. |
| **software** | 4-dim + cast + `indirection_dim` + `reshape_kind` | the firmware walks the copy itself (`sw=1`). |

`NO BACKEND FOUND` → the DGE does nothing and may `S: DGE: Dispatched error notification`
(VA `0x8318e`), guarded by a `S: DGE: Failed bounds check. $S[%i]+=%i.` path (VA `0x8310d`).

> **The hand-off point, stated precisely.** `dge_decode_fast`'s common exit (`j 0x3713`) leads
> — after the reshape/spray stage — into the backend-availability-table read at the
> `const16 a4,0x5fe0` arm. **That `l32i.n a3,a4,0; beqz a3` table probe is the boundary**: the
> decode/setup machinery on *this* page produces the populated `dge_shape` + context binding;
> the selector consumes the availability table and routes to one builder. The
> [backend-selector page](dge-backend-selector.md) decodes the three builders
> (`0x135d4` / `0x13c5c` / `0x13c4c` and the RTL path). `[HIGH/OBSERVED]` for the boundary
> instruction; the cross-stage flow ordering is `[HIGH/INFERRED]`.

---

## 7. The SDMA descriptor-ring target — what the DGE ultimately feeds

The DGE's whole purpose is to fill an **SDMA BD descriptor ring** and ring a doorbell. After a
backend is chosen, the emit ops (`push GENERATE` / `DIMPUSH` / `REGWRITE`, documented on
[emit](dge-emit.md)) write BD entries into the ring, then `rdma_desc_gen → rdma_desc_start`
encode the ring and bump the tail pointer. The full `P%i: Q7: rdma_desc_gen [%s] …` /
`rdma_desc_start [%s] …` corpus is byte-read in the Q7 DRAM (ring_num, tpb_idx, routing_id,
dma_mask, n_active_dmas, semaphore-descriptor pushes — the descriptor-program detail).

**The doorbell is a single MMIO store.** The TX/RX tail-pointer writeback decodes cleanly in
the Q7 IRAM `@0x17388` (byte-confirmed this session):

```asm
; rdma_desc_start tail-pointer writeback — CAYMAN_Q7_POOL_DEBUG IRAM, byte-exact
17388:  41a408   l32r    a4, <tail-inc reg addr>  ; a4 = M2S/S2M tail-pointer-increment CSR
1738f:  a4654e   const16 a10, 0x4e65   ; -> "[TX] Writing tail pointer increment"
17392:  e00500   callx8  a5            ; logger
17395:  a40800   const16 a10, 8
17398:  a4a34e   const16 a10, 0x4ea3   ; -> "[TX] Tail pointer increment written"
1739b:  3904     s32i.n  a3, a4, 0     ; *** WRITE TAIL-PTR INCREMENT a3 -> [a4]  (TX doorbell) ***
1739d:  060a00   j       0x173c9
 ...
173a3:  a4e14e   const16 a10, 0x4ee1   ; -> "[RX] Writing tail pointer increment"
173a6:  e00500   callx8  a5
173a9:  3904     s32i.n  a3, a4, 0     ; *** RX tail-ptr increment store (RX doorbell) ***
173ab:  a40800   const16 a10, 8
173b1:  a41f4f   const16 a10, 0x4f1f   ; -> "[RX] Signaling TX to proceed (right_push)"
```

The tail-pointer increment is a **single `s32i.n a3,a4,0`** to the per-queue tail-inc register
loaded via `l32r` into `a4`. `a3` is the number of descriptors to advance; the store bumps the
ring tail and **launches the engine**. This is the UDMA doorbell. `[HIGH/OBSERVED]`

> **QUIRK — the "written" log is staged *before* the store fires.** At `0x17398` the firmware
> stages the `[TX] Tail pointer increment written` string pointer into `a10`, and only at
> `0x1739b` does the actual `s32i.n` doorbell write happen — the confirmation arg is set up
> ahead of the store, with the matching `callx8` logger on the fall-through path. Do not read
> "written" as "the store has completed"; the byte order is *stage-arg → store → log*.
> `[HIGH/OBSERVED]`

**Reconciliation with the ring / RDM (orientation):**

- `a4` = the per-queue **M2S (TX) / S2M (RX) tail-pointer-increment CSR** — the UDMA doorbell.
  The `s32i a3` increments the ring tail by `a3` descriptors and launches: "bump tail by N
  descriptors and launch." `[HIGH/INFERRED]`
- The **4-deep BD ring** the GENERATE/DIMPUSH pushes fill (wrap-at-4 cursor) is owned by the
  SDMA staging context; the DGE is the *producer + doorbell*.
- The **RDM** (Ring-Descriptor-Manager) is the *consumer-side* hardware: per queue it holds the
  ring base + a tail-pointer **write-back** AXI address + ring size + ringId injection +
  AXI-B error handling. The DGE writes BDs and bumps the tail-inc CSR; the RDM then performs
  the tail-pointer write-back + ringId injection + completion notification. **DGE = producer /
  doorbell; RDM = tail-writeback / notify** — two halves of one path. `[HIGH/INFERRED]`

> **NOTE — where the descriptor data physically lives.** The DGE-generated SDMA BD rings live
> in the device descriptor RAM (a large pure-memory SoC region) and/or the local dataram
> staging window; `rx.base`/`tx.base` (§3) point into that space. The host-side control
> structures (priority-class map, mailbox) are in the per-core control DRAM window, a *separate*
> region. Whether `rx.base`/`tx.base` index the large descriptor-RAM region vs. the local
> dataram staging window is **not byte-proven here** — the staging context places its rings in
> local dataram, so the DGE likely stages there with the large region backing the wider pool.
> `[LOW/flagged]`

---

## 8. Cross-references

- **[DGE 3-Backend Selector](dge-backend-selector.md)** — the next stage: decodes the
  Pool/RTL/software builders (`0x135d4` / `0x13c5c` / `0x13c4c` and the RTL path) and the
  availability-table arm whose probe (`const16 a4,0x5fe0`) is the §6 hand-off point.
- **[DGE Reshape / Dimension Handling](dge-reshape.md)** — `analyze_tensor_reshape` /
  `gen_spray_info` / `make_gather_pattern`: the Reshape-Strategy / #DMA / partition split that
  sits between decode (§4) and backend-select (§6).
- **[DGE Descriptor Emit](dge-emit.md)** — the `push GENERATE` / `DIMPUSH` / `REGWRITE` ops
  that fill the ring §7 feeds.
- **[iDMA / Legacy DMA](idma-legacy-dma.md)** — the legacy single-transfer `DramRingDMA` path,
  the contrast anchor: one BD vs. the DGE's descriptor program.
- **DGE Builder + QoS** (`../../dma/dge-builder-qos.md`) — **forward link, Part 9, not yet
  authored.** Will decode the priority-class / QoS arbitration that the `engine`/`queue`
  binding (§3) and the host priority-class map feed. *(planned path — NOTE: target file does
  not yet exist.)*
- **DGE Micro-op Encoding** (`../../dma/dge-microop-encoding.md`) — **forward link, Part 9, not
  yet authored.** Will decode the exact BD word0/word1 bitfield packing the GENERATE/DIMPUSH/
  REGWRITE pushes produce. *(planned path — NOTE: target file does not yet exist.)*
- **[The Confidence & Walls Model](../../reference/confidence-model.md)** — the normative
  definition of the `[CONF/PROV]` tags and the FLIX-desync MED ceiling cited throughout.
