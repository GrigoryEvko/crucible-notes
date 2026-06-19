# CAYMAN × ACT image

The baseline firmware image for the **Activation engine** (ACT) on CAYMAN (NeuronCore
v3 / NC-v3). This is the page the Mariana / Mariana+ / Maverick ACT diffs reference,
so it is documented byte-true: every size, sha256, opcode, and string below is read
directly from `libnrtucode_internal.so`
(sha256 `b7c67e89…632fc329b`) and its 14 `CAYMAN_NX_ACT_*_get` accessors, with the
shipped Cadence Vision-Q7 `ncore2gp` disassembler decoding the carved blobs.

> **The verdict up front.** CAYMAN × ACT is **not** a standalone activation-kernel
> core and **not** a POOL-style `kernel_info_table` compute engine. It is an
> **NX-class SEQ-style ASCII-opcode dispatch engine** — the *same* `cayman/seq/`
> firmware codebase that the NX POOL / PE / DVE / SP sequencers run — compiled with
> the activation engine's handler subset. The activation *function* (relu / gelu /
> sigmoid / …) is **not a code kernel**: it is a host-loaded LUT applied by the
> `Activate` opcode. [HIGH/OBSERVED]

Related pages: [Firmware-Image Accessor Index](./image-catalog-index.md) ·
[PROF_CAM / PROF_TABLE Blob Formats](./prof-cam-table-formats.md) ·
[Activate + the PWL Application Mechanism](../firmware/kernels/activate-pwl.md) ·
[MARIANA × ACT image (cross-gen diff vs Cayman)](./mariana-act.md) ·
[CAYMAN × POOL image](./cayman-pool.md) · [CAYMAN × DVE image](./cayman-dve.md) ·
[Per-Engine Firmware Depth](../uarch/per-engine-depth.md).

---

## 1. The 14 ACT image getters

The ACT firmware ships as device-memory blobs wrapped in the host `.rodata` of
`libnrtucode_internal.so`, each exposed by a 4-instruction accessor stub of the form:

```c
// CAYMAN_NX_ACT_<VARIANT>_<REGION>_get  — one per (variant, region)
void getter(void **img_ptr /*rdi*/, size_t *img_size /*rsi*/) {
    *img_ptr  = &blob;   // lea <blob>(%rip),%rax ; mov %rax,(%rdi)
    *img_size = SIZE;    // movq $SIZE,(%rsi)
}                        // ret
```

There are exactly **14** ACT getters (`nm … | rg -c 'CAYMAN_NX_ACT_[A-Z]+_[A-Z]+_get$'`
→ 14; the IDA functions sidecar independently lists 14). `CLS=NX` (NX-core instruction
sequencer), `ENG=ACT`. 8 carry real bytes; 6 are zero-size boundary cursors.
[HIGH/OBSERVED]

| VARIANT | REGION | ACCESSOR (.text VA) | IMG-PTR (.rodata VA == file off) | SIZE | STATUS |
|---|---|---|---|---|---|
| DEBUG | IRAM | `0x9b3520` | `0x150220` | `0x191e0` | REAL (code) |
| DEBUG | DRAM | `0x9b3540` | `0x169400` | `0x06260` | REAL (data + `S:` log) |
| DEBUG | SRAM | `0x9b3560` | `0x16f660` (DVE cursor) | `0x0` | EMPTY (boundary) |
| DEBUG | EXTRAM | `0x9b3580` | `0x16f660` (DVE cursor) | `0x0` | EMPTY (boundary) |
| PERF | IRAM | `0x9b3020` | `0x058f40` | `0x13dc0` | REAL (code) |
| PERF | DRAM | `0x9b3040` | `0x06cd00` | `0x02900` | REAL (data, no `S:`) |
| PERF | SRAM | `0x9b3060` | `0x06f600` (DVE cursor) | `0x0` | EMPTY (boundary) |
| PERF | EXTRAM | `0x9b3080` | `0x06f600` (DVE cursor) | `0x0` | EMPTY (boundary) |
| TEST | IRAM | `0x9b32a0` | `0x0d58a0` | `0x13940` | REAL (code) |
| TEST | DRAM | `0x9b32c0` | `0x0e91e0` | `0x02c00` | REAL (data, no `S:`) |
| TEST | SRAM | `0x9b32e0` | `0x0ebde0` (DVE cursor) | `0x0` | EMPTY (boundary) |
| TEST | EXTRAM | `0x9b3300` | `0x0ebde0` (DVE cursor) | `0x0` | EMPTY (boundary) |
| PROF | CAM | `0x9b3ba0` | `0x3028a0` | `0x00400` | REAL (HW-decode CAM) |
| PROF | TABLE | `0x9b3bc0` | `0x302ca0` | `0x02000` | REAL (profile table) |

The 6 SRAM/EXTRAM getters all emit `movq $0x0,(%rsi)` and point their `lea` at the
**contiguous-layout cursor** — the start of the *next* engine's blob (DVE IRAM) — so
`objdump` resolves the symbol to `CAYMAN_NX_DVE_<v>_IRAM_get.data`. They ship **no
bytes**; ACT runs entirely out of IRAM (code) + DRAM (data) on CAYMAN. (Counted
distinct `.data` addresses for the 14 getters = 11: 8 real + 3 shared DVE cursors,
one per variant.) [HIGH/OBSERVED]

> **GOTCHA — read the getter STUBS via `objdump`, not raw `dd`.** `.rodata` is
> identity-mapped (VA == file offset; `readelf -SW` shows `.rodata` VA `0x46b0` ==
> file `0x46b0`), which is why the *carves* below are byte-exact. But `.text` is
> **not** identity-mapped: VA `0x9b01a0` vs file `0x9af1a0`, a **`0x2000`** delta.
> A naive `dd skip=<stub-VA>` reads the wrong instruction bytes (off by `0x2000`)
> and yields bogus ptr/size literals. Resolve the stub via `objdump -d` (VA-aware)
> or subtract `0x2000` from the VA before `dd`. The carve `IMG-PTR`s, being
> `.rodata` VAs, need no adjustment. [HIGH/OBSERVED — CORRECTION caught this task]

---

## 2. Carve provenance + byte-identity reconciliation

Carve rule (`.rodata` identity map): `blob = internal.so[IMG-PTR : IMG-PTR+SIZE]`
(`dd bs=1`). All 8 real carves reproduce the report's sha256 exactly: [HIGH/OBSERVED]

| IMAGE | FILE-OFF | SIZE | sha256 |
|---|---|---|---|
| ACT_DEBUG_IRAM | `0x150220` | `0x191e0` | `ffbb78fea7ad956d…0809aaa58d2330fc` |
| ACT_DEBUG_DRAM | `0x169400` | `0x6260` | `f6c5136e99c09f04…2732c81bf733bbc` |
| ACT_PERF_IRAM | `0x058f40` | `0x13dc0` | `5ef2a351cef7e1da…e2e36d911b078de0` |
| ACT_PERF_DRAM | `0x06cd00` | `0x2900` | `12a4713e33a8ada6…950246d58117d9` |
| ACT_TEST_IRAM | `0x0d58a0` | `0x13940` | `bc38eddeab6d8885…1a3ddc66062a2ef2` |
| ACT_TEST_DRAM | `0x0e91e0` | `0x2c00` | `b6bf4736c8768f27…303bc3cad7d3978` |
| ACT_PROF_CAM | `0x3028a0` | `0x400` | `8fd7e422bd07881a…4db234d19aa95f31` |
| ACT_PROF_TABLE | `0x302ca0` | `0x2000` | `ce761f81d075658e…a016c15e821cf1` |

**3-source reconciliation.** The static archive `libnrtucode.a` ships exactly 14
`CAYMAN_NX_ACT` members: 12 `img_CAYMAN_NX_ACT_<MODE>_<SEG>_contents.c.o` (the base
IRAM/DRAM/SRAM/EXTRAM segments) + 2
`hwdecode_CAYMAN_NX_ACT_PROF_{CAM,TABLE}_contents.c.o`. Each `img_` member is an
x86-64 relocatable defining both `<NAME>_get` (T) and `<NAME>_get.data` (r), wrapping
the device blob as its `.rodata`. Extracting each member's `.rodata`
(`ar x` → `objcopy -O binary --only-section=.rodata`) and comparing gives **8/8
IDENTICAL** — internal.so getter blob == `.a` member `.rodata`. One firmware corpus,
two packaging views. [HIGH/OBSERVED]

---

## 3. Flat-image geometry + reset vector

None of the 8 carves is an ELF (head bytes ≠ `\x7fELF`). They are **flat
device-memory segments**, the device-side `.rodata` payload of the `*_contents.c.o`
members — *not* the `EM_XTENSA` ELFs of the Q7 POOL EXTISA blobs. The "ELF
confirmation" for a flat image is the geometry confirmation: reset vector at byte 0,
DRAM dispatch table at `0x814`, and the `ncore2gp` disassembler decoding it to real
Q7/NX windowed-ABI + FLIX-VLIW code. [HIGH/OBSERVED]

- **IRAM** = code at device VA `0x0` (reset vector at byte 0).
- **DRAM** = data at device VA `0x80000`, so **DRAM string offset = device VA −
  `0x80000`** and the dispatch table at file `0x814` is DRAM VA `0x80814`.

**Reset vector — byte-identical across all 3 IRAM variants** (`od -An -tx1 -N16`):

```
06 76 00 00 | 00 00 | 86 77 00 00 | 00 00 | a0 71 69 80
```

Decoded with the shipped `ncore2gp` `xtensa-elf-objdump` (exit 0, empty stderr):

```text
0x000:  06 76 00     j        0x1dc      ; primary reset vector → boot path
0x006:  86 77 00     j        0x1e8      ; secondary vector → halt trap
0x1dc:  04 00 00     const16  a0, 0
0x1df:  04 90 00     const16  a0, 144    ; a0 = 0x90  (C enter_run prologue)
0x1e2:  a0 00 00     jx       a0         ; jump into the C boot prologue
0x1e8:  00 52 00     halt     0          ; 2nd vector is a HALT trap
```

The `j 0x1dc` (`06 76 00 00`) is **byte-identical** to the NX SEQ-engine reset vector.
ACT computes its **own engine identity at boot** — the DRAM string
`S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u`
is present, so the *same* flat binary can be loaded on any engine slot and the
`engine_idx` is derived at runtime from `engine_base_addr` vs `tpb_base_addr`, not
baked in. [HIGH/OBSERVED for the string; the runtime-compute is INFERRED from the
string + boot path.]

**Vector datapath.** `ncore2gp objdump -D -b binary` decodes every IRAM to real
windowed-ABI (`entry` / `retw.n`, `l32e`/`s32e` window spill — 369 such markers in
PERF IRAM) and a dense Cadence Vision IVP TIE vector ISA: **PERF 299 / TEST 297 /
DEBUG 178** distinct `ivp_*` mnemonics (`ivp_addnx16t`, `ivp_absssubnx16`,
`ivp_addexpmnxf16t`, `ivp_addn_2xf32t`, …). The activation *math* runs on this vector
datapath. [HIGH/OBSERVED]

> **NOTE — why DEBUG decodes to *fewer* IVP ops (178) than PERF (299).** DEBUG is the
> *larger* image yet shows the *smaller* distinct-mnemonic count. Two reasons: (a) the
> linear `objdump` sweep desyncs more across the bigger DEBUG image's FLIX/literal
> boundaries (documented SX-FW-00 limitation — trampoline starts show `.byte 0xf`
> FLIX selectors), and (b) PERF schedules denser FLIX vector bundles. The count is a
> *floor* on the real vector-op inventory, not a ceiling. [MED/INFERRED]

---

## 4. The ACT dispatch loop

The decode/dispatch is the **same SEQ model** as the other NX sequencers, confirmed
three independent ways from the DEBUG IRAM disassembly. The fetched opcode is decoded
through the DRAM `0x80814` table; the table is **direct-indexed** with index =
`opcode − 0x41` (so the SEQ ASCII control opcode `'A'`=`0x41` → slot 0). The
ACT-native compute opcodes occupy the **low byte range `0x21`–`0x25`** and are
dispatched by an unrolled compare chain (§4, QUIRK).

Annotated reconstruction of the per-instruction dispatch (real addresses / symbols):

```c
// ---- ACT main dispatch (cayman/seq) — DEBUG IRAM, real call sites annotated ----
for (;;) {
    uint8_t opcode = fetch_next_opcode();            // SEQ instruction stream

    // (c) per-fetch trace log (DEBUG only): "S: Dispatch opcode=0x%x" @ DRAM 0x80868
    //     IRAM 0x2b2a: const16 a10,8 ; const16 a10,0x868 ; call8 0x15530 (log helper)
    log_fmt("S: Dispatch opcode=0x%x", opcode);

    // ACT-native compute opcodes 0x21..0x25 routed by the unrolled compare chain:
    //   IRAM 0x2b35: movi.n a3,33(=0x21) ; bne a2,a3,.. ; j handle_Activate
    //   0x2b40 a3,34(=0x22)→Quantize  0x2b4b a3,35(=0x23)→TableLoad
    //   0x2b56 a3,36(=0x24)→ReadAccumulator   (then 70,71=0x46,0x47 ; 0x9f..0xb8 block)
    switch (opcode) { case 0x21: return handle_Activate(); /* ... */ }

    // (b) bound check for the table path: 178-entry table, index 0..177
    //     IRAM 0x2c2b: movi a3,177 ; bne a2,a3,...   (the 178-bound)
    uint32_t idx = (uint8_t)(opcode - 0x41);         // ASCII-normalize ('A' → 0)
    if (idx > 177) goto error_handler;               // → "Bad Opcode(0x%x)"

    // (a) table-indexed indirect jump @ DRAM file 0x814 == DRAM VA 0x80814
    //     IRAM 0x399d: const16 a4,0x814 ; addx4 a3,a3,a4 ; l32i.n a3,[a3] ; jx a3
    void (*handler)(void) = dispatch_table[idx];     // 4-byte IRAM target
    handler();                                        // C++ Handler (self-names via S: log)
}
```

The three HIGH anchors, all decoded in DEBUG IRAM this task: [HIGH/OBSERVED]

- **`0x399d`** `const16 a4,0x814 ; addx4 a3,a3,a4 ; l32i.n a3,[a3] ; jx a3` — the
  DRAM `0x80814` table-indexed indirect jump.
- **`0x2c2b`** `movi a3,177` — the 178-entry bound (0-indexed).
- **`0x2b2a`** `const16 a10,8 ; const16 a10,0x868 ; call8 0x15530` — loads
  `S: Dispatch opcode=0x%x` (DRAM VA `0x80868`) and calls the log helper.

> **CORRECTION — the opcode space is two-tier, not a single `−0x41` normalization.**
> The PERF *jump table* at DRAM `0x814` is indexed by `opcode − 0x41` (the SEQ ASCII
> control range). But the **ACT-native compute opcodes are the raw low bytes
> `0x21`–`0x24`** (`Activate`=`0x21`, `ActivateQuantize`=`0x22`,
> `ActivationTableLoad`=`0x23`, `ActivationReadAccumulator`=`0x24`, `Activate2`=`0x25`)
> — these are tested literally by the DEBUG compare chain (`movi.n a3,33/34/35/36` at
> `0x2b35`/`0x2b40`/`0x2b4b`/`0x2b56`), confirmed against the
> [PWL kernel page](../firmware/kernels/activate-pwl.md). The PROF CAM (§6) arms *both*
> tiers: `0x21`–`0x24` (ACT-native) and the `0x41`+ ASCII control opcodes. [HIGH/OBSERVED]

### The clean PERF dispatch table (the reference geometry)

The PERF DRAM carries the **same** 178-entry, 4-byte table at file `0x814`, stripped
of logs — this is the clean reference. Decoded (178 LE words at file `0x814`):
[HIGH/OBSERVED]

- **Default trampoline** (unknown-opcode arm) = `0x9c02`, occupying **8** slots —
  the holes at opcodes `0x6e`…`0x75` (`'n'`…`'u'`).
- **84 populated handler slots**: a dense **ASCII block** `0x41`…`0x7d` (`'A'`…`'}'`,
  53 slots) plus a **binary control block** `0xc5`…`0xf2` (31 slots). The
  remaining slots beyond the live opcode range carry table-tail filler (an ASCII
  string fragment `"us t"` bleeds past the live entries — read as data, not a
  trampoline).
- Index = `opcode − 0x41`, direct-indexed `O(1)` jump — exactly the SEQ scheme.

> **QUIRK — DEBUG uses a compare-chain / multi-table hybrid, not one clean table.**
> The DEBUG ACT IRAM augments the jump table with an **unrolled opcode-compare
> chain** (`movi.n a3,33 ; bne a2,a3,… ; j <handler>` for raw opcodes `0x21`,`0x22`,…
> at `0x2b35`, the `0x46`/`0x47` pair at `0x2b6d`, and a high binary block
> `0x9f`…`0xb8` at `0x2b83`…`0x2bfb`). Three table bases are referenced in DEBUG IRAM
> (`const16 a4,0x814` @`0x399d` for a 17-entry sub-table; `0x584` @`0x12f9b`; `0x6c4`
> @`0x13fc5` for a 101-word run). The HIGH facts (table base `0x80814`, 178-bound,
> the compare-chain opcode constants, the Dispatch log) are byte-exact; the **full
> byte-exact per-opcode DEBUG table is MED** (the FLIX/literal-pool desync). The PERF
> table is the authoritative per-opcode geometry. [MED for the exhaustive DEBUG rows]

### Unknown-opcode / error path

Present byte-for-byte in the ACT DRAM (`strings | rg ErrorHandler`), identical fault
classes to the SEQ engine: [HIGH/OBSERVED]

```
S: ErrorHandler : Bad Opcode(0x%x)
S: ErrorHandler : Illegal Instruction(0x%x)
S: ErrorHandler : FP Error(%d)
S: ErrorHandler : Int Div Zero Error
S: Assertion failure! %s(%s:%u)
```

The assertion source paths name the engine source tree:
`…/cayman/seq/src/handlers/exception_handler.hpp` (with `:82/:84/:86 !ret_val`).

---

## 5. The activation-kernel opcode set

Every ACT handler self-names by logging `S: <OpName>` on entry — that is how the
roster is recovered. The ACT-specific handlers each load their name from DRAM and
call the log helper, e.g. `Activate` at DRAM VA `0x81a41`, `ActivationTableLoad` at
`0x81aa0`. DRAM string offsets (add `0x80000` for the device VA); opcode column
cross-referenced from the [PWL kernel page](../firmware/kernels/activate-pwl.md) and
confirmed against the §4 compare chain: [HIGH/OBSERVED]

| Offset | DRAM VA | opcode | `S:` name | Role |
|---|---|---|---|---|
| `0x1a41` | `0x81a41` | `0x21` | `Activate` | apply the activation function (LUT-driven — §6) |
| `0x1a70` | `0x81a70` | `0x22` | `ActivateQuantize` | fused activation + output quantize |
| `0x1aa0` | `0x81aa0` | `0x23` | `ActivationTableLoad` | load the activation function table / LUT |
| `0x1ad0` | `0x81ad0` | `0x24` | `ActivationReadAccumulator` | read the ACT per-lane fp32 accumulator |
| `0x2390` | `0x82390` | — | `Copy` | tensor copy |
| `0x23b0` | `0x823b0` | — | `Cast` | dtype cast |
| `0x23d0` | `0x823d0` | — | `TensorScalar` | tensor-scalar elementwise (bias/scale) |

**The activation function is a table, not a kernel.** No `relu` / `gelu` / `sigmoid`
/ `exp` / `tanh` / `softmax` / `silu` / `erf` / `elu` **code** strings exist anywhere
in any ACT image (DEBUG / PERF / TEST, IRAM or DRAM — exhaustively grep-verified
absent). The activation function is **data** loaded into an activation table by
`ActivationTableLoad` and applied by `Activate`. The resolvable activation kernel set
is therefore the **4 activation opcodes** (`Activate` / `ActivateQuantize` /
`ActivationTableLoad` / `ActivationReadAccumulator`) plus `Cast` / `Copy` /
`TensorScalar`; the specific functions are run-time, host-supplied table contents.
[HIGH/OBSERVED — absence grep-verified; LUT interpretation INFERRED HIGH from the
`ActivationTableLoad`+`Activate` pair and the absence of named kernels.]

`ActivationReadAccumulator` (opcode `0x24`, also in the PROF CAM set — §6) reads the
ACT engine's per-lane fp32 reduction accumulator, i.e. the PE-array PSUM pulled into
the ACT engine before the activation is applied. The
[Activate + PWL Application Mechanism](../firmware/kernels/activate-pwl.md) page
documents the `S3D3_AC` operand struct (64 B; `activation_func@35` is a raw `u8`
table index — no ISA-named relu/gelu enum; affine `scale·x + bias` is applied **pre**
the function) and the four host-loaded PWL sub-tables that `ActivationTableLoad`
(via the compiler `LoadActFuncSet` pseudo-instruction) DMAs and that `Activate`
evaluates.

Annotated activation-apply pipeline (the ACT engine's role, reconstructed):

```c
// ---- ACT activation apply (per Activate / ActivationTableLoad pairing) ----
// Setup (once per activation function): host issues ActivationTableLoad (0x23), which
// DMA-stages the four PWL tables (the relu/gelu/... coefficients) into the activation
// table SRAM — NOT firmware-resident; see activate-pwl.md §5.
void handle_ActivationTableLoad(inst_t *ins) {   // "S: ActivationTableLoad", op 0x23
    load_activation_table(ins->src);             // host-supplied PWL coefficients
}

// Per-tile compute:
void handle_Activate(inst_t *ins) {              // "S: Activate", op 0x21
    acc = read_act_accumulator();                // fp32 PSUM (cf. op 0x24)
    for (lane = 0; lane < NLANES; ++lane) {      // IVP vector datapath
        float y = ins->scale_value * acc[lane] + ins->bias;   // affine PRE func
        out[lane] = pwl_eval(act_table, ins->activation_func, y); // table-driven f(x)
    }
    // ActivateQuantize (0x22) fuses an output-quantize step after pwl_eval().
}
```

### ACT-vs-POOL handler diff (apples-to-apples, same `S:` regex on both DEBUG DRAMs)

Carving `CAYMAN_NX_POOL_DEBUG_DRAM` (ptr `0x1cdc40`, size `0x6f20`) and diffing the
two `S:` rosters: [HIGH/OBSERVED]

- **ACT-ONLY**: `Activate`, `ActivateQuantize`, `ActivationReadAccumulator`,
  `ActivationTableLoad`, `Cast`, `Copy`, `TensorScalar`.
- **POOL-ONLY**: `ConvLutLoad`, `CrossLaneReduce`, `EmbeddingUpdate`,
  `ExtendedInst`, `GetSequenceBounds`, `Indirect Copy`, `Iota`, `LoadPoolArgument`,
  `ModifyPoolConfig`, `Pool`, `Pool Buffer Load`, `RandGetState`, `RandSetState`,
  `SB2SB_Collective`, `Sort`, `TensorDequantize`, `TensorGather`, `TensorScalarAddr`,
  `TensorScalarAffineSelect`.
- **SHARED SEQ control/move core** (identical to POOL): `AluOp`, `BRANCH`,
  `BranchPrefetchHint`, `EngineNop`, `Event_Semaphore`, `INS_FL`, `MOVE`, `NOP`,
  `NOTIFY`, `POLL_SEM`, `SET_OM`, `TensorLoad`, `TensorStore`, `WRITE`.

The engines share an identical SEQ control/move front-end and differ **only** in the
compiled-in compute-handler subset. The decode handlers come from the same source
tree — PERF/TEST/DEBUG DRAM all carry `…/src/decode/alu_op.cpp`,
`…/src/decode/move.cpp`, `…/src/decode/branch.cpp` with DTYPE constants
`NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}`; `alu_op.cpp` has 4 distinct "not
supported op" assert sites (`:141`, `:196`, `:220`, `:262`). [HIGH/OBSERVED]

---

## 6. The DRAM image + PROF tables

**Base DRAM image (device VA `0x80000`).** The 178-entry dispatch jump table sits at
file `0x814` (DRAM VA `0x80814`), immediately followed (file ≥ `0x854` in DEBUG) by
the `S:` log/format-string pool. The DRAM carries **no firmware-resident
activation-coefficient table** — the activation table is loaded at runtime by
`ActivationTableLoad` (host-supplied data). A word-scan of the DEBUG DRAM found 6
dispatch/handler-pointer runs (file ranges `0xcc`–`0x1f8`, `0x584`–`0x6b0`,
`0x6c4`–`0x858`, `0x1ec0`–`0x1fe4`, `0x2f24`–`0x2fa8`, `0x310c`–`0x3200`) — the
per-opcode trampoline tables of the segmented DEBUG dispatch (§4). [HIGH geometry /
MED exhaustive per-table decode]

**PROF_CAM (`0x400` = 1 KiB) — the HW-decode profiling CAM.** 16-byte fixed-stride
records, **64 slots, 47 populated**: [HIGH/OBSERVED]

```c
struct prof_cam_record {       // 16 bytes — the per-engine HW-decode profiler CAM
    uint32_t opcode_id;        // instruction opcode to match
    uint32_t mask;             // 0xff = exact-byte match
    uint32_t enable;           // 1
    uint32_t reserved;         // 0
};                             // 47 populated, 17 zeroed
```

> **GOTCHA — this 16 B profiler CAM is NOT the 32 B activation-LUT CAM.** This
> `PROF_CAM` (`{opcode, mask, enable, rsvd}`, 16 B, keyed on opcode only) is the
> hardware *instruction-decode profiler*. It is structurally distinct from the 32 B
> activation `aws_hal_stpb_act_cam_entry_t` (keyed on `(opcode, func_id)`) that the
> [PWL page](../firmware/kernels/activate-pwl.md) §5 describes for the activation LUT.
> Same word "CAM", two different tables. [HIGH/OBSERVED]

The 47 `opcode_id` values are the ACT engine's ASCII/binary opcodes the HW-decode
profiler is armed to count (mask `0xff`, enable `1`):

```
0x01 0x06 0x02 0x07 0x03  0xa1 0xa4 0xa7 0xa8 0xa9 0xaa 0xab 0xb1
0x21 0x22 0x23 0x24 0x49  0xa6 0xa2 0xa3 0xb2 0xa5 0xa0 0xb8 0xbb 0xbd
0x41 0x51 0x42 0x52 0x43 0x53 0x44 0x54 0x45 0x46 0x47 0x48 0x4a 0x4b 0x4c
0x4e 0x5e 0x67 0x68 0x00
```

(`0x21`–`0x24` are the ACT-native compute opcodes; `0x41`=`'A'` … are the shared SEQ
ASCII control opcodes; `0x24` is `ActivationReadAccumulator`.) [HIGH/OBSERVED bytes;
the "profiler arms these opcodes" reading is INFERRED HIGH from the
`{opcode,mask,enable}` record shape + the hwdecode-table provenance.] See
[PROF_CAM / PROF_TABLE Blob Formats](./prof-cam-table-formats.md) for the cross-engine
CAM/TABLE schema.

**PROF_TABLE (`0x2000` = 8 KiB) — the profile counter/event descriptor table.**
Header word `0x00000201`, then `0x26000010`, then a short embedded
text/descriptor blob, then mostly zero (**150 nonzero words of 2048**). A
preallocated profiling-event/counter table the HW-decode profiler fills at run time;
only the header + descriptor region is non-zero in the shipped image. Exact
field schema not exhaustively decoded. From
`hwdecode_CAYMAN_NX_ACT_PROF_TABLE_contents.c.o` (HIGH provenance). [MED for the
field schema]

---

## 7. DEBUG vs PERF vs TEST diff (the variant delta)

| VARIANT | IRAM sz | DRAM sz | `S:` strings | total strings | IRAM IVP-distinct |
|---|---|---|---|---|---|
| DEBUG | `0x191e0` | `0x6260` | 150 | 249 | 178 |
| PERF | `0x13dc0` | `0x2900` | 0 | 15 | 299 |
| TEST | `0x13940` | `0x2c00` | 0 | 58 | 297 |

- **DEBUG** is the largest and the **only** build carrying the 150 `S:` runtime log
  strings — it is the reverse-engineering substrate (every handler self-names via its
  `S: <OpName>` log). [HIGH/OBSERVED]
- **PERF** (production / release flavor) strips **all** `S:` logs: DRAM shrinks to
  `0x2900`, only **15** strings survive — all assertion source-paths
  (`exception_handler.hpp`, `alu_op.cpp`, `move.cpp`, `branch.cpp`,
  `signal_handler.cpp`) plus the `ok_to_evict` WARNING and the generic
  `Assertion failure!`. IRAM also shrinks, yet the IVP vector-op count *rises* to 299
  (see §3 NOTE). [HIGH/OBSERVED]
- **TEST** sits between: 0 `S:` logs but 58 strings — it keeps FUNCTION-NAME / file
  symbols for assert context (`fetch_cache_line`, `enter_run`,
  `sunda_handle_surprises`, `sunda_redirect`, `soc_window_manager.hpp`,
  `interrupt_handler`, `setup_interrupts`, `push_unallocated_window`, …) — a
  symbol/assert build. [HIGH/OBSERVED]

> **The dispatch mechanism is invariant across all three.** Same reset vector
> (`06 76 00 00`), same DRAM table @ `0x814`, same 178-bound, same ErrorHandler arms,
> same `cayman/seq/` codebase. A DEBUG↔RELEASE swap is a pure **observability**
> change, not a functional/dispatch change. [HIGH/OBSERVED]

---

## 8. Engine-model classification

ACT is a **SEQ-style ASCII-opcode dispatch engine** — the NX-class sequencer model,
*not* a POOL-style `kernel_info_table` compute engine, *not* a standalone
activation-kernel core.

| PROPERTY | ACT (this page) | NX SEQ engine | POOL Q7 kernel |
|---|---|---|---|
| packaging | flat IRAM/DRAM segments | flat IRAM/DRAM | `EM_XTENSA` ELF (.so blob) |
| reset vector | `j 0x1dc` (`06 76 00 00`) | `j 0x1dc` (`06 76 00 00`) | ELF `e_entry 0x010056xx` |
| dispatch table | DRAM `0x80814` | DRAM `0x80814` | `kernel_info_table` @`0x2000380` |
| entry size | 4 B (direct IRAM target) | 4 B | 8 B (key4 + funcVA4) |
| entry count / bound | 178 (idx = byte−`0x41`) | 178 | 17 (CAYMAN_0) |
| key | ASCII opcode byte − `0x41` | ASCII byte − `0x41` | `(opcode<<24)\|(spec<<16)` |
| lookup | direct-indexed jump (`O(1)`) | direct-indexed jump | linear key-scan |
| handler form | C++ Handler + `S:` log | C++ Handler + `S:` log | flat C kernel fn |
| miss policy | `ErrorHandler "Bad Opcode"` | `ErrorHandler "Bad Opcode"` | POOL miss path |
| source tree | `cayman/seq/src/…` | `cayman/seq/src/…` | Q7 pool ucode |
| compute handlers | Activate / Quantize / TableLoad / ReadAccumulator / Cast / Copy | POOL / Tensor / Reduce / … | `pool_iota`/`copy`/`cast`/… |

ACT and the NX POOL / PE / DVE / SP sequencers are the **same `cayman/seq/`
firmware**, differing only in the compiled-in compute-handler subset. ACT is the
control/sequencer **front-end** for the activation engine: it decodes the `S:`
instruction stream and routes to the activation handlers, which run vector math
(≈299 IVP ops) on the NX core's datapath, driven by a host-loaded activation table.
[HIGH/OBSERVED]

**Hardware map.** `TPB_0_ACT` lives at SoC base `0x2802400000` (~`0x200000` span)
with IRAM / NX / LOCAL_REG / profile sub-blocks (the activation-LUT SRAM regions
`TPB_0_ACT_PROFILE_CAM`/`PROFILE_TABLE`/`BUCKET_TABLE`/`CONTROL_TABLE` are documented
on the [PWL page](../firmware/kernels/activate-pwl.md) §5). ACT is one of the five
engines (ACT=engine idx 1, plus PE/POOL/DVE/SP) carrying an Xtensa `LOCAL_REG`
control block; ACT is a **single Xtensa-NX core + small sequencer block** (no Q7_CORE
sub-array — only POOL has the 8-core Q7 array). The firmware carved here is the
program for that single NX sequencer core. [HIGH — cited from the SoC address map and
the per-engine-depth page, not re-derived here]

> **CARRIED forward (Maverick).** On MAVERICK (NC-v5, header-OBSERVED only) the ACT
> engine is **folded into DVE** — there is no standalone ACT engine block; the PWL
> SRAM migrates into `TPB_DVE` and the read-accumulator is re-expressed as the
> DVE-native `DveReadAccumulator` (opcode `0x9b`), with the table format byte-identical
> (per [per-engine-depth](../uarch/per-engine-depth.md) §8.4). The CAYMAN page is the
> byte-true baseline; the [MARIANA × ACT](./mariana-act.md) page tracks the v3→v4
> delta from here, and the Maverick fold diffs against this geometry.

---

## 9. Honesty ledger

**HIGH / OBSERVED (direct byte read or disassembly this task):**

- 14 `CAYMAN_NX_ACT` getters (`nm` count 14, IDA functions sidecar 14); 8 real
  (ptr/size re-read via VA-aware `objdump`) + 6 zero-size DVE-cursor aliases (all six
  `movq $0x0`).
- 8 carves byte-identical (sha256) to the `libnrtucode.a` member `.rodata`, 8/8;
  shas reproduced exactly.
- Flat geometry; reset vector `06 76 00 00` (`j 0x1dc`) identical across DEBUG/PERF/
  TEST; boot path `j 0x1dc → const16 a0,0x90 ; jx a0`; 2nd vector `j 0x1e8 → halt 0`
  (native `ncore2gp` disasm, empty stderr).
- Dispatch: table base DRAM `0x80814` (`const16 a4,0x814 ; addx4 ; l32i.n ; jx`
  @`0x399d`), 178-bound (`movi a3,177` @`0x2c2b`), ACT-native compute opcodes
  `0x21`–`0x24` via compare chain (`movi.n a3,33/34/35/36` @`0x2b35`…`0x2b56`),
  `S: Dispatch opcode=0x%x` log @`0x80868` (call @`0x2b2a`). PERF 178-entry table:
  default `0x9c02` ×8, 84 populated (ASCII `0x41`–`0x7d`, binary `0xc5`–`0xf2`).
- 150 `S:` strings in DEBUG DRAM; 7 ACT-specific handlers named from their own logs;
  ACT-vs-POOL roster diff. ErrorHandler arms present; source tree `cayman/seq/src/…`
  in all 3 variants.
- PROF_CAM 16-byte `{opcode, mask=0xff, enable=1, rsvd}` × 47; PROF_TABLE header
  `0x201` / `0x26000010`, 150/2048 nonzero.
- DEBUG/PERF/TEST size + string-count + IVP-count diff (178/299/297 distinct
  `ivp_*`); 369 windowed-ABI markers in PERF IRAM.

**MED / INFERRED:** exhaustive per-opcode DEBUG dispatch table (compare-chain +
multi-table hybrid, FLIX/literal desync — base/bound/compare-constants/log are HIGH);
PROF_TABLE field schema (structure-level only); "engine_idx computed at boot"
(INFERRED from the OBSERVED `engine_base_addr` string + boot path).

**LOW / NOT CLAIMED:** which silicon part / runtime selects DEBUG vs PERF vs TEST
(host-driver + `NEURON_UCODE_FLAVOR` decision); activation FUNCTION coefficients
(host-supplied table data, exhaustively grep-confirmed absent from the image).
