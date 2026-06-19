# CAYMAN × PE image

The **CAYMAN × PE** firmware image is the baseline **Processing-Element** (`engine_idx = 0`,
systolic matmul / TensorE) program for the NC-v3 generation. It is one compiled flavor of the
shared `cayman/seq/` NX-class SEQ-dispatch chassis — the *same* engine binary structure as
[CAYMAN × ACT](./cayman-act.md), [× DVE](./cayman-dve.md), [× POOL](./cayman-pool.md) and
[× SP](./cayman-sp.md) — recompiled with the **systolic-matmul handler subset** linked in. This
page carves all 14 image variants byte-exact from `libnrtucode_internal.so`, characterizes the
NX matmul sequencer (boot → dispatch → the 5 PE-specific weight-load/matmul handlers), pins the
DEBUG-vs-PERF(release)-vs-TEST delta, resolves cross-engine code sharing, and establishes the
engine-model class. The Mariana / Mariana+ / Maverick PE images ([mariana-pe.md](./mariana-pe.md)
and the forward Part-6 pages) reference this page as their diff baseline.

The decisive CAYMAN-specific result: **`PeManageSeed` (0x08), `LdweightsMX` (0x09),
`MatmulMX` (0x0A) and `ConvLutLoad` (0xe4) do NOT exist in any CAYMAN PE variant** — they first
ship in the MARIANA (v4) PE image. CAYMAN PE's compute subset is exactly the five micro-ops
`{Ldweights, Matmul, MatmulSparse, LdTags, PeRegWrite}`. The full per-gen micro-op semantics,
operand structs and PSUM datapath are documented in
[PE Matrix-Multiply Path](../firmware/kernels/pe-matmul.md); this page is the **CAYMAN image-level
ground truth** that page diffs against.

Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every device fact is byte-pinned to a carve from
`libnrtucode_internal.so` (sha256 `b7c67e89…`) and decoded with the shipped `ncore2gp`
`xtensa-elf-objdump`; the SEQ-engine and PSUM/SBUF model facts are CARRIED from the engine pages
cited inline.

> **NOTE — the objects used.** Container:
> `…/custom_op/c10/lib/libnrtucode_internal.so` (sha256
> `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, ELF64 x86-64 DYN, **not
> stripped**). The first R `LOAD` is the identity map (`off 0x0 == vaddr 0x0`, `filesz 0x9af194`),
> so each `<NAME>.data` accessor address is **simultaneously** the `.rodata` VA and the file
> offset of its blob — carve = `so[ptr : ptr+size]`. The PE image blobs are the device-side
> `.rodata` payloads of the `img_CAYMAN_NX_PE_*` / `hwdecode_CAYMAN_NX_PE_PROF_*` members. **IRAM
> file-offset == device IRAM VA** (reset vector at byte 0); **DRAM string-file-offset == device
> DRAM VA − `0x80000`**. Disassembler:
> `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump`
> (GNU Binutils 2.34.20200201, `XTENSA_CORE=ncore2gp`, ConfigName `Xm_ncore2gp`, uarch Cairo,
> Xtensa24, RI-2022.9, `TargetHWVersion=NX1.1.4`, `IsaMaxInstructionSize=32` FLIX/VLIW). All carve
> sha256, the reset vector, the dispatch table and the handler `const16` xrefs were reproduced
> this session (exit 0, empty stderr). `[HIGH/OBSERVED]`

---

## 1. The headline

1. **CAYMAN × PE is the same `cayman/seq/` SEQ-dispatch engine as ACT/DVE/POOL/SP**, compiled
   with the PE matmul handler subset. Decisive: the carved IRAM reset vector is the
   byte-identical `06 76 00 00` (`j 0x1dc`) shared by all five engines; the DRAM carries the
   same `S: BEGIN on cayman`, `S: Dispatch opcode=0x%x`, I$-cache / PC-bounds / DMA / shape infra;
   the DEBUG dispatch table base is the same DRAM `0x80814`; the source tree is the same
   `/opt/workspace/NeuronUcode/cayman/seq/src/…`. `[HIGH/OBSERVED]`
2. **PE = the matmul SEQUENCER** (`engine_idx = 0`, corpus CSR enum `PE=0 ACT=1 POOL=2 DVE=3
   TPB_SP=4`). Its distinguishing kernel set is the five systolic-array micro-ops `Ldweights`,
   `Matmul`, `MatmulSparse`, `LdTags`, `PeRegWrite` — present in PE and **absent from all of**
   ACT/DVE/POOL/SP (apples-to-apples `S:`-handler set-diff on all five CAYMAN DEBUG DRAMs).
   `[HIGH/OBSERVED]`
3. **PE is the LEANEST compute engine of the five.** Its **24** distinct handlers = the 18-handler
   shared SEQ control/move core (byte-identical in all 5 engines) + `EngineNop` + the 5 PE matmul
   handlers. PE carries **no** Tensor-Tensor/Scalar/Reduce/Pool primitives (POOL/DVE have them) and
   **no** activation handlers (ACT has them). Handler tally across the wave: DVE 53 \| POOL 41 \|
   ACT 26 \| **PE 24** \| SP 18. `[HIGH/OBSERVED]`
4. **14 image getters; 8 carry real bytes, 6 are zero-size boundary cursors** — DEBUG/PERF/TEST ×
   {IRAM, DRAM} = 6 flat firmware segments + PROF {CAM, TABLE} = 2 HW-decode profiling tables;
   DEBUG/PERF/TEST × {SRAM, EXTRAM} = 6 empty cursors (PE runs entirely out of IRAM+DRAM on
   CAYMAN). All 8 real carves are byte-identical (sha256) to the matching `libnrtucode.a` member
   `.rodata`. `[HIGH/OBSERVED]`
5. **`PeManageSeed`/MX/`ConvLutLoad` are NOT in CAYMAN PE.** `S:`-string grep over DEBUG/PERF/TEST
   DRAM returns **0** hits for `ManageSeed`, `MX`, `seed`, `xorwow`/`lfsr`, `ConvLut`. The PSUM
   fp32→bf16 stochastic-rounding seed manager (`PeManageSeed` 0x08, `PE_SEED_MODE` NONE=0 /
   LOAD_SEED=1 / SAVE_SEED=2) first ships in **MARIANA (v4)**; this is the CAYMAN→MARIANA
   per-gen step the [pe-matmul.md §11](../firmware/kernels/pe-matmul.md) table records.
   `[HIGH/OBSERVED — absence exhaustively grep-confirmed across all 3 variants]`

> **CORRECTION — do NOT carry `PeManageSeed` into the CAYMAN roster.** The committed opcode set
> in [pe-matmul.md §2](../firmware/kernels/pe-matmul.md) (`0x01..0x0A` + `0xe4`) is the **MARIANA**
> dispatch chain — that is the page where the byte-decoded `bnei a2,8 → PeManageSeed` arm lives.
> The CAYMAN image's dispatch routes only `{0x01,0x02,0x03,0x06,0x07}` to compute stubs; the
> `0x08 PeManageSeed`, `0x09 LdweightsMX`, `0x0A MatmulMX`, `0xe4 ConvLutLoad` opcodes are **+4
> over CAYMAN**, added at v4. A reimplementer targeting NC-v3 must not allocate those four
> opcodes. `[HIGH/OBSERVED]`

---

## 2. The 14 image getters (instruction-exact)

Each getter is the 4-instruction `(img-ptr, size)` stub
(`lea <blob>(%rip),%rax ; mov %rax,(%rdi) ; movq $<size>,(%rsi) ; ret`) disassembled from
`.text 0x9b3120..0x9b3c51`. `CLS=NX, ENG=PE`. Every `.data` address below was read this session
from the host-side symbol table (local `r`/`t` symbols — use plain `nm`, not `nm -D`); all 14
agree with the catalog ([image-catalog-index.md](./image-catalog-index.md), CAYMAN NX_PE rows).
`[HIGH/OBSERVED]`

| VARIANT | REGION | ACCESSOR (.text VA) | IMG-PTR (.rodata VA = file off) | SIZE | STATUS |
|---|---|---|---|---:|---|
| DEBUG | IRAM | `0x9b3620` | `0x192080` | `0x19180` | REAL (code) |
| DEBUG | DRAM | `0x9b3640` | `0x1ab200` | `0x06220` | REAL (data + `S:` log) |
| DEBUG | SRAM | `0x9b3660` | `0x1b1420` (=POOL cursor) | `0` | EMPTY (boundary) |
| DEBUG | EXTRAM | `0x9b3680` | `0x1b1420` (=POOL cursor) | `0` | EMPTY (boundary) |
| PERF | IRAM | `0x9b3120` | `0x0881e0` | `0x159e0` | REAL (code) |
| PERF | DRAM | `0x9b3140` | `0x09dbc0` | `0x02a40` | REAL (data, no `S:`) |
| PERF | SRAM | `0x9b3160` | `0x0a0600` (=POOL cursor) | `0` | EMPTY (boundary) |
| PERF | EXTRAM | `0x9b3180` | `0x0a0600` (=POOL cursor) | `0` | EMPTY (boundary) |
| TEST | IRAM | `0x9b33a0` | `0x1048e0` | `0x152c0` | REAL (code) |
| TEST | DRAM | `0x9b33c0` | `0x119ba0` | `0x02d80` | REAL (data, no `S:`) |
| TEST | SRAM | `0x9b33e0` | `0x11c920` (=POOL cursor) | `0` | EMPTY (boundary) |
| TEST | EXTRAM | `0x9b3400` | `0x11c920` (=POOL cursor) | `0` | EMPTY (boundary) |
| PROF | CAM | `0x9b3c20` | `0x3070a0` | `0x00400` | REAL (HW-decode CAM) |
| PROF | TABLE | `0x9b3c40` | `0x3074a0` | `0x02000` | REAL (profile table) |

The six zero-size SRAM/EXTRAM getters all execute `movq $0x0,(%rsi)` and return their pointer at
the **contiguous-layout cursor** = the start of the next engine's IRAM blob
(`CAYMAN_NX_POOL_<MODE>_IRAM_get.data`: PERF `0xa0600`, DEBUG `0x1b1420`, TEST `0x11c920`).
`objdump` aliases the symbol to the POOL blob because PE is the last NX engine before POOL in the
`.rodata` layout (ACT cursors → DVE, DVE → PE, PE → POOL). **PE uses no SRAM/EXTRAM on CAYMAN.**
`[HIGH/OBSERVED]`

### 2.1 Carve provenance + 3-source byte-identity

Carve rule (identity map): `blob = so[IMG-PTR : IMG-PTR+SIZE]`. The 8 real carves and their
sha256 (reproduced this session; **8/8 identical** to the `libnrtucode.a` member `.rodata`, via
`ar x` + `objcopy -O binary --only-section=.rodata` + `cmp`): `[HIGH/OBSERVED]`

| IMAGE | FILE-OFF | SIZE | sha256 (full) |
|---|---|---:|---|
| `PE_DEBUG_IRAM` | `0x192080` | `0x19180` | `e1f1268c5358679bd74289cad7ecdfebd9f8e809644f40448ea0638cb2b344a4` |
| `PE_DEBUG_DRAM` | `0x1ab200` | `0x6220` | `400910dde8dd73b6bea916b718761535b0ffaee573cb04918074d1ffa72eefa9` |
| `PE_PERF_IRAM` | `0x881e0` | `0x159e0` | `13ba396929a0e144dd464812f7052d01a9159a0349c556b2988d74bd25198b25` |
| `PE_PERF_DRAM` | `0x9dbc0` | `0x2a40` | `1355773bfecf9e907dcf8002760ce8c82a638e7a305e82999405d7ff7de05d2c` |
| `PE_TEST_IRAM` | `0x1048e0` | `0x152c0` | `4f9cbece3896ca3682ae82ffa956b8c81cb6d8d6c34e5fb53fd9537361671740` |
| `PE_TEST_DRAM` | `0x119ba0` | `0x2d80` | `3561e4bd0c01b923cef75d91c6c70ae231438ac0d5d03bcc61c9f4dd8a607301` |
| `PE_PROF_CAM` | `0x3070a0` | `0x400` | `8fd7e422bd07881a525bf28aa489793f1b0b1f721b33a8414db234d19aa95f31` |
| `PE_PROF_TABLE` | `0x3074a0` | `0x2000` | `ce761f81d075658e9e4705528d4401bca1f91702734c0507a4a016c15e821cf1` |

The **PE PERF_IRAM `13ba3969`** is the engine-distinguishing fingerprint used in the cross-engine
matrix (§7). The archive ships exactly 14 `CAYMAN_NX_PE` members (12 `img_*_contents.c.o` + 2
`hwdecode_*_PROF_{CAM,TABLE}_contents.c.o`); the `internal.so` getter blob equals the `.a` member
`.rodata` for all 8. `[HIGH/OBSERVED]`

---

## 3. Flat-image geometry + boot

None of the 8 carves is an ELF (head ≠ `\x7fELF`) — they are **flat device-memory segments**, the
device-side `.rodata` payload of the `img_*`/`hwdecode_*` members. (Contrast the Q7 POOL EXTISA
blobs, which *are* `EM_XTENSA` ELFs.) Heads read this session: `[HIGH/OBSERVED]`

```text
IRAM (all 3 variants):  06 76 00 00 00 00  86 77 00 00 00 00   ; j 0x1dc / j 0x1e8
DRAM (DEBUG):           34 cb 99 60                            ; header word 0x6099cb34
PROF_CAM:               01 00 00 00 ff 00 00 00 01 00 00 00    ; CAM record 0: {0x01, 0xff, 1, …}
PROF_TABLE:             01 02 00 00 10 00 00 26 60 61 63 6f …  ; header 0x00000201, then 0x26000010
```

**Reset vector** (byte-identical across all 3 IRAM variants AND across all 5 engines): `[HIGH/OBSERVED]`

```text
0x000:  06 76 00   j 0x1dc        ; primary reset vector -> boot path
0x006:  86 77 00   j 0x1e8        ; secondary vector -> halt trap
0x1dc:  const16 a0,0 ; const16 a0,0x90 ; jx a0   ; jump to C enter_run @ 0x90
0x1e8:  005200    halt 0                          ; 2nd vector = HALT trap
```

`enter_run @ 0x90` was verified as the prologue the boot `jx a0` targets. The boot bodies **past
byte 6** are engine-specific (different literal pools / `enter_run` bodies); the **first 12 bytes**
are shared by all five engines. The same flat binary can be loaded on any engine slot: the
DRAM carries `S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u
engine_idx=%u`, so `engine_idx` (= 0 for PE) is derived at boot from `engine_base_addr` vs
`tpb_base_addr` — the binary is **not** hard-wired to PE. `[HIGH/OBSERVED — string; runtime-compute
INFERRED from the string + boot path]`

Disassembled with the shipped `ncore2gp` objdump (exit 0, empty stderr), every IRAM decodes to
real Q7/NX windowed-ABI (`entry a1,N` / `retw`, register-window spill) + dense Cadence Vision IVP
vector ops — i.e. the PE NX core carries a full vector compute datapath (the int-quantized MAC math
runs on it; §6). Per-IRAM census this session: `[HIGH/OBSERVED]`

| variant | entry | retw | distinct IVP |
|---|---:|---:|---:|
| DEBUG | 525 | 730 | 179 (FLIX-desync lowers the distinct count) |
| PERF | 150 | 187 | **307** |
| TEST | 133 | 191 | 316 |

---

## 4. The PE matmul-sequencer dispatch

PE uses the SEQ dispatch model: an opcode-byte decode, the `S: Dispatch opcode=0x%x` log, and an
opcode-indexed jump into a DRAM-resident trampoline table feeding the C++ handlers. Confirmed three
ways this session: `[HIGH base / MED exhaustive per-opcode table]`

* **(a) DEBUG dispatch sub-table @ DRAM `0x80814` (file `0x814`).** The word run there holds IRAM
  trampoline targets — read this session: `0x3c44, 0x3c5c, 0x3c92, 0x3cc2, 0x3caa, 0x3c74, …`
  (17 in-range IRAM targets in the first 24 words). Same base as the SEQ engine and ACT.
  `[HIGH/OBSERVED]`
* **(b) The per-fetch log** `S: Dispatch opcode=0x%x` lives at DRAM VA `0x80868` (file `0x868`),
  emitted at three IRAM sites (one per NX-mode path) before the decode:
  `const16 a10,8 ; const16 a10,0x868 ; call8 0x154d4 ; l32i.n a2,[a1+8] ; bnei a2,1,…`
  (the DEBUG **compare-chain** begins here). `[HIGH/OBSERVED]`
* **(c) PERF clean indexed dispatch.** PERF relocates the table to DRAM `0x80218` (file `0x218`)
  and replaces the compare-chain with one bounds-checked `addx4`/`l32i`/`jx`:
  `bltu a4,a2,0x5e0c (default/Bad-Opcode arm) ; const16 a4,0x218 (table base) ; addx4 a2,a2,a4 ;
  l32i.n a2,[a2] ; jx a2`. The PERF table holds 162 non-default in-range IRAM targets.
  `[HIGH/OBSERVED — the addx4/l32i/jx core; MED per-opcode rows due to FLIX bundle interleaving]`

The DEBUG-segmented vs PERF-clean split is the same form ACT documents: DEBUG unrolls into an
`bnei`/`movi;bne;j` opcode-compare chain + the 17-entry sub-table; PERF emits the single `addx4`
table. The HIGH facts are the log, the DEBUG base `0x80814`, the PERF base `0x80218` and the
default arm `0x5e0c`; the byte-exact full per-opcode list is MED (the documented FLIX/literal
desync). `[HIGH base / MED table]`

> **GOTCHA — the CAYMAN dispatch maps FIVE compute opcodes, not nine.** Cross-referencing the
> MARIANA byte-decoded chain in [pe-matmul.md §2.1](../firmware/kernels/pe-matmul.md)
> (`bnei a2,1/2/3/6/7/8 ; movi a3,9 ; bnei a2,10 ; movi a3,228`), the CAYMAN PE image routes only
> `{0x01 Ldweights, 0x02 Matmul, 0x03 PeRegWrite, 0x06 LdTags, 0x07 MatmulSparse}` to compute
> stubs plus the shared SEQ-core control opcodes. The `0x08/0x09/0x0A/0xe4` arms are **not
> present** in the CAYMAN compare-chain (their handler strings and stubs are absent — §5).
> `[HIGH/OBSERVED — handler-name absence; INFERRED-HIGH for the chain-arm absence given the missing
> stubs]`

### 4.1 Annotated dispatch + weight-load/matmul flow

The sequencer mechanism reproduced as C pseudocode. Symbols are byte-pinned (CAYMAN opcodes /
DRAM offsets observed this session; the PSUM/SBUF dataflow is CARRIED from
[pe-matmul.md §4](../firmware/kernels/pe-matmul.md) + the hardware map):

```c
// ---------------------------------------------------------------------------
// CAYMAN PE NX matmul sequencer — boot -> main fetch/dispatch -> handler.
// The PE firmware ORCHESTRATES; the systolic array, the per-cell MAC, and the
// FP32 PSUM banks are silicon (no netlist ships).
//   Opcodes (CAYMAN, observed): LDWEIGHTS=0x01 MATMUL=0x02 PE_REG_WRITE=0x03
//                               LDTAGS=0x06 MATMUL_SPARSE=0x07 (=S4D3_MM).
//   NOT present on CAYMAN: PE_MANAGE_SEED=0x08, LDWEIGHTS_MX=0x09,
//                          MATMUL_MX=0x0A, CONV_LUT_LOAD=0xe4  (first ship v4).
// ---------------------------------------------------------------------------

void pe_enter_run(void) {                          // boot jx a0 lands @ IRAM 0x90
    pe_compute_engine_idx();                       // engine_base_addr vs tpb_base_addr -> 0 (PE)
    // "S: NX in HW Decode mode" | "S: NX in Sunda mode: HW decode disabled"
    bool hw_decode = nx_mode_select();             // dual-mode SEQ feature (not PE-specific)

    for (;;) {                                      // "S: Sunda seq Loop" main loop
        instr_t *ins = seq_fetch(hw_decode);       // I$-backed fetch (cache + PC-bounds machinery)
        uint8_t  op  = ins->opcode;                // raw opcode byte (no addi-normalisation)
        log("S: Dispatch opcode=0x%x", op);        // DRAM 0x80868 (DEBUG only)

        // DEBUG: segmented compare-chain; PERF: addx4 table @ DRAM 0x80218.
        switch (op) {
        // ---- the 5 PE-specific matmul micro-ops (CAYMAN compute subset) ----
        case 0x01: h_ldweights(ins);     break;    // "S: Ldweights"    DRAM-str 0x81a60
        case 0x02: h_matmul(ins);        break;    // "S: Matmul"       DRAM-str 0x81a80
        case 0x03: h_pe_reg_write(ins);  break;    // "S: PeRegWrite"   DRAM-str 0x81aa0
        case 0x06: h_ldtags(ins);        break;    // "S: LdTags"       DRAM-str 0x82350
        case 0x07: h_matmul_sparse(ins); break;    // "S: MatmulSparse" DRAM-str 0x82370 (S4D3_MM)
        // ---- shared 18-handler SEQ control/move core (all 5 engines) -------
        default:   seq_core_dispatch(ins, op);     // AluOp/BRANCH/MOVE/NOTIFY/TensorLoad/... EngineNop
                                                   // miss -> ErrorHandler "Bad Opcode(0x%x)"
        }
    }
}

// ---- the systolic weight-load -> matmul-accumulate flow the handlers drive ----
// (operand structs read from the decoded-instruction param block; CAYMAN uses the
//  v3 TENSOR3D source — no MX union, no seed_mode/flags byte; see pe-matmul.md §3/§4)
void h_ldweights(instr_t *ins) {                   // 0x01  S3_LW_STRUCT (v3 layout)
    // load the STATIONARY weight tile from SBUF into the systolic array as the XR
    // weight bus; SBUF-only (AllowedInPSUM=False); load_order LAST_COL_FIRST default.
    pe_load_weights(ins);
}

void h_matmul(instr_t *ins) {                      // 0x02  S3D3_MM_STRUCT (v3: no flags byte)
    // stream the MOVING activation tile (from SBUF) through the loaded array;
    // each PE cell multiply-accumulates; partial sums accumulate down the columns
    // into the FP32 PSUM banks.  dst = stationary.T @ moving.  Internal acc = FP32.
    // out_dtype on CAYMAN(v3) = FP32 only (BF16 drain + its SR-RNG seeds are v4+).
    // psum_accumulate_flags byte selects SINGLE vs MULTI_{START,MID,END} group;
    // psum_zero_region clears 1..8 banks on the first write of a group.
    pe_stream_matmul(ins);
    // The PE PSUM is later read by the ACT engine's Activate op (its TENSOR3D src is
    // AllowedInPSUM=True): Activate drains PSUM, applies affine+PWL, writes SBUF.
    // (NOT 0x24 ActivationReadAccumulator — that reads ACT's own fp32 reduction acc.)
}
```

> **QUIRK — no seed/BF16 path in the CAYMAN matmul handler.** On CAYMAN the `S3_LW`/`S3D3_MM`
> operand structs carry **no `flags`/`seed_mode` byte** (the v3 `TENSOR3D` source, separate
> `transpose_mode`/`perf_opt`/`row_grp`/`col_grp` fields). The PSUM-drain `out_dtype` is FP32 only;
> there is no fp32→bf16 stochastic-rounding stage and therefore no `PeManageSeed`. The `flags`
> byte (with `seed_mode:2`) and the BF16 `out_dtype` are added at MARIANA (v4). `[HIGH/OBSERVED —
> CAYMAN absence; the struct-byte detail CARRIED from pe-matmul.md §3.2/§4]`

---

## 5. The PE-specific matmul/weight-load kernel set

Method (same handler-diff as the sibling pages): extract every single-token `S: <OpName>` from
each engine's CAYMAN DEBUG DRAM (regex `^S: [A-Za-z][\w/-]*$`) and set-diff. The PE DEBUG DRAM
yields exactly **24** distinct handler names this session. Per-engine counts:
DVE 53 \| POOL 41 \| ACT 26 \| **PE 24** \| SP 18 (PE has the second-smallest set; only the
pure-control SP is leaner). `[HIGH/OBSERVED]`

Each PE-specific handler was verified to be a real handler: its `S:` string DRAM offset is loaded
by a `const16 a10,<low16>` in the IRAM and a real `entry`-prologue handler logs it via the shared
log helper `call8 0x154d4` (itself verified an `entry` function at byte `0x154d4`). The five
`const16` xrefs were re-disassembled this session and agree exactly with the DRAM string offsets:
`[HIGH/OBSERVED]`

| handler | opcode | DRAM str VA (file off) | IRAM `const16` xref (this session) |
|---|---|---|---|
| `Ldweights` | `0x01` | `0x81a60` (`0x1a60`) | `9698: const16 a10, 0x1a60` |
| `Matmul` | `0x02` | `0x81a80` (`0x1a80`) | `96c0: const16 a10, 0x1a80` |
| `PeRegWrite` | `0x03` | `0x81aa0` (`0x1aa0`) | `96e8: const16 a10, 0x1aa0` |
| `LdTags` | `0x06` | `0x82350` (`0x2350`) | `bbfc: const16 a10, 0x2350` |
| `MatmulSparse` | `0x07` | `0x82370` (`0x2370`) | `bc24: const16 a10, 0x2370` |

(`Ldweights`/`Matmul`/`PeRegWrite` are contiguous in DRAM @ `0x1a60`/`0x1a80`/`0x1aa0`;
`LdTags`/`MatmulSparse` form a second cluster @ `0x2350`/`0x2370`.) The roles
([pe-matmul.md](../firmware/kernels/pe-matmul.md) for byte-exact operand structs):

* **`Ldweights` (0x01)** — load the stationary weight tile from SBUF into the PE array (the `x` /
  stationary operand of `nc_matmul`); operand `S3_LW_STRUCT` (v3 `TENSOR3D`).
* **`Matmul` (0x02)** — stream the moving activation tile, MAC down the columns into the FP32 PSUM
  banks; operand `S3D3_MM_STRUCT` (v3).
* **`MatmulSparse` (0x07 = `MATMUL_SPARSE`)** — fine-grain-sparse matmul with a **4-D source**
  struct **`S4D3_MM_STRUCT`** (the 4th dim = the 2/3/4 sparse fmaps per partition), fed by `LdTags`
  + `Ldweights` `tag_weight_mode`. (`Matmul` uses `S3D3_MM`; only `MatmulSparse` uses `S4D3_MM`.)
* **`LdTags` (0x06)** — load the per-column/per-pass sparsity-tag metadata (`S3_LT_STRUCT`); the
  consumer of the [SparsityCompress(Tag) producer](../firmware/kernels/sparsity-compress-tag.md).
* **`PeRegWrite` (0x03)** — write a PE-array configuration register (`queue_cfg.bypass_peregwrite_
  instr` lets it skip the SBUF fetch as a reg-immediate; [pe-matmul.md §10](../firmware/kernels/pe-matmul.md)).

### 5.1 The shared core + the set-diff

* **18-handler shared SEQ control/move core** (byte-for-name identical in ACT/DVE/PE/POOL/SP,
  5-way intersection confirmed this session): `AluOp`, `BRANCH`, `BranchPrefetchHint`,
  `Event_Semaphore`, `EXT_BREAK`, `Halt`, `INS_BREAK`, `INS_FL`, `MOVE`, `NOP`, `NOTIFY`,
  `POLL_SEM`, `Redirect`, `SET_OM`, `STRONG_ORDER`, `TensorLoad`, `TensorStore`, `WRITE`.
* **PE's one shared compute handler:** `EngineNop` (shared with POOL/DVE, not SP).
* **The 5 PE-ONLY handlers** (each found in NONE of ACT/DVE/POOL/SP — 5/5 PE-exclusive):
  `Ldweights`, `Matmul`, `MatmulSparse`, `LdTags`, `PeRegWrite`.

So **PE = 18 shared + `EngineNop` + 5 PE-only = 24**. The engines are the same `cayman/seq/`
firmware with disjoint compute subsets: ACT contributes `Activate`/`…ReadAccumulator`/`…TableLoad`/
`Cast`/`Copy`/`TensorScalar`; DVE the 28 batch-norm/predicated/scan/dropout data-vector handlers;
POOL pool/reduce/gather/sort/dequant/conv-lut; SP no compute (pure 18-handler core). PE's subset
is **just the systolic-matmul pipeline**. `[HIGH/OBSERVED]`

> **CORRECTION — `PeManageSeed` is NOT a CAYMAN PE handler.** Grep over all three CAYMAN PE DRAM
> variants returns **0** hits for `ManageSeed`, `MX`, `seed`, `xorwow`/`lfsr`, `ConvLut` (verified
> this session). The PSUM stochastic-rounding seed manager `PeManageSeed` (0x08; `PE_SEED_MODE`
> NONE=0 / LOAD_SEED=1 / SAVE_SEED=2; struct `S2S1D2_PE_SEED_STRUCT`) **first ships in MARIANA
> (v4)** — it is one of the +4 v4 opcodes (`0x08`/`0x09`/`0x0A`/`0xe4`). It is **distinct** from
> the GpSimd/Vector PRNG managers `rand_get_state` (0x77) / `rand_set_state` (0x78), which live on
> POOL/Vector, not PE. Do not carry any seed/RNG handler into the CAYMAN PE roster.
> `[HIGH/OBSERVED — absence; the v4-first-ship CARRIED from pe-matmul.md §11]`

The decode handlers share the source tree across all builds: the DRAMs carry the assertion paths
`…/cayman/seq/src/handlers/exception_handler.hpp`, `…/src/decode/alu_op.cpp`,
`…/src/decode/move.cpp`, `…/src/decode/branch.cpp`, `…/src/handlers/signal_handler.cpp`, with
DTYPE constants `NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}`. `[HIGH/OBSERVED]`

---

## 6. The inner MAC datapath (PERF IRAM)

The PE NX core carries the full **IVP widening-MAC** vector datapath (the int-quantized / assist
path the firmware also runs). Census re-read this session from the **CAYMAN PE PERF IRAM**
(`0x881e0`, decoded `ncore2gp`, exit 0) — 307 distinct IVP ops; the MAC-family leaders:
`[HIGH/OBSERVED census this session]`

| mnemonic (count, CAYMAN PERF IRAM) | role ([B04](../isa/ref/b04-mac-integer.md) / [B05](../isa/ref/b05-mac-mixed.md)) |
|---|---|
| `ivp_mul4t2n8xr8` (73) | 4-TERM dot, i8 × XR8 — the densest, the i8 matmul core |
| `ivp_mulusp2n8xr16` (37) | u×s PAIR, i8 × XR16 ([B05](../isa/ref/b05-mac-mixed.md) mixed-sign) |
| `ivp_mul4tn16xr8` (32) | 4-TERM dot, i16 × XR8 |
| `ivp_muluspn16xr16` (30) | u×s PAIR-accumulate, i16 × XR16 |
| `ivp_mul4ta2n8xr8` (21) | 4-TERM accumulate, i8 × XR8 → 1536-bit `wvec` |
| `ivp_mulpan16xr16` (16) | PAIR-accumulate MAC (2×-FMAC) |
| `ivp_mulus4t2n8xr8` (12) | u×s 4-TERM, i8 × XR8 |

The `*XR8`/`*XR16` suffix is the XR reduce-register **weight** broadcast (loaded by `Ldweights`);
the other factor is the **moving activation** lane. The integer accumulator is the 4-entry,
1536-bit `wvec` regfile (widening i8→24 / i16→48 / i32→96 per lane); the **TensorE float**
accumulator is the FP32 PSUM banks. The MAC family detail (4T/PAIR/dual-QUAD K-folding, the
`packvr*` drain, per-instruction semantics) is in
[pe-matmul.md §6](../firmware/kernels/pe-matmul.md) and the
[B04](../isa/ref/b04-mac-integer.md)/[B05](../isa/ref/b05-mac-mixed.md) MAC ISA pages. The
dedicated systolic array does the bulk float MAC in HW (no netlist ships); the exact HW/FW labour
split is `[INFERRED-MED]` — the array RTL is out of corpus. `[HIGH/OBSERVED census]`

---

## 7. DRAM + PROF tables; cross-engine sharing

**DRAM image** (device VA `0x80000`; DEBUG layout): header word `0x6099cb34` @ `0x0`; dispatch
sub-table @ `0x814` (17 in-range IRAM trampolines); then the `S:` log/format pool + the matmul
handler-name strings (`Ldweights` `0x81a60`, `Matmul` `0x81a80`, `PeRegWrite` `0x81aa0`,
`LdTags` `0x82350`, `MatmulSparse` `0x82370`) + the assertion source-paths. PERF/TEST relocate the
dispatch table to `0x218` and drop the `S:` strings (§8). The DRAM also carries the generic SEQ
runtime infra (I$ Hit/Miss / query addr-tag / replace victim / `wait_for_dma_fill` / DMA enqueue /
`is_pc_in_bounds` / `Seq Loop iter` / `R[%d]` register-trace / `shape[].step`/`.num` /
`dge_shape[].step`), confirming the same cache/PC-bounds/DMA/shape machinery as the other NX
engines. `[HIGH geometry / MED exhaustive per-table decode]`

> **NOTE — no resident weight table in the PE firmware.** The PE DRAM carries **no** standalone
> weight/coefficient table. Weights are loaded at runtime by `Ldweights`/`MatmulSparse` from SBUF
> (host/DMA-supplied), not baked into the image. `[HIGH/OBSERVED]`

**PROF_CAM** (`0x400` = 1 KiB) — the HW-decode profiling CAM. 16-byte fixed-stride records, 64
slots, **47 populated** (verified this session); record = `{ u32 opcode_id ; u32 mask(=0xff) ;
u32 enable(=1) ; u32 reserved(=0) }`. First opcode_ids: `0x01, 0x06, 0x02, 0x07, 0x03, 0xa1,
0xa4, 0xa7, …` — the generic NX opcode set, **not** a PE-specific list (see below). `[HIGH/OBSERVED]`

**PROF_TABLE** (`0x2000` = 8 KiB) — the profile counter/event descriptor table. Header word
`0x00000201`, then `0x26000010` + a small descriptor blob (the `` `acofglm`` fragment), then
mostly zero — a preallocated table the HW-decode profiler fills at run time. Exact counter/event
schema not decoded. `[HIGH provenance + cross-engine identity / MED schema]`

**Cross-engine code-sharing** (this session, all 5 CAYMAN NX engines): `[HIGH/OBSERVED]`

| engine | PERF_IRAM sha256 | PERF_IRAM size | PROF_CAM | PROF_TABLE |
|---|---|---:|---|---|
| ACT | `5ef2a351…` | `0x13dc0` | `8fd7e422…` | `ce761f81…` |
| DVE | `9fa066f4…` | `0x15c20` | `8fd7e422…` | `ce761f81…` |
| **PE** | **`13ba3969…`** | **`0x159e0`** | `8fd7e422…` | `ce761f81…` |
| POOL | `9049bf8c…` | `0x17280` | `8fd7e422…` | `ce761f81…` |
| SP | `5a6f6eaa…` | `0x182c0` | (no PROF) | (no PROF) |

* **CODE/DATA (IRAM/DRAM): PE shares NO bytes with ACT/DVE/POOL/SP.** Each engine's PERF_IRAM has
  a distinct sha256 and size — each is a separately-compiled `cayman/seq/` build with its own
  handler subset linked in. The sharing is at the **source/structure** level (identical reset
  vector, dispatch model, control/move core, source tree), not at the linked-byte level.
* **PROFILING TABLES: byte-identical across all 4 NX engines.** PROF_CAM (`8fd7e422`, 47 records)
  and PROF_TABLE (`ce761f81`, 8 KiB) are a generic shipped resource (carved + sha-compared for
  ACT/DVE/PE/POOL this session — **4/4 identical** each). SP ships no PROF tables. The 47
  opcode_ids are the **generic** NX opcode set, not PE-specific. See
  [prof-cam-table-formats.md](./prof-cam-table-formats.md). `[HIGH/OBSERVED — sha256 4/4]`

---

## 8. DEBUG vs PERF(release) vs TEST

| variant | IRAM size | DRAM size | `S:` strings | total strings | IRAM distinct-IVP |
|---|---:|---:|---:|---:|---:|
| **DEBUG** | `0x19180` | `0x6220` | **148** | 247 | 179 (FLIX-desync, lower) |
| **PERF** | `0x159e0` | `0x2a40` | 0 | 15 | 307 |
| **TEST** | `0x152c0` | `0x2d80` | 0 | 58 | 316 |

* **DEBUG** is the largest and the **only** build carrying the 148 `S:` runtime log strings (the RE
  substrate: every handler self-names via `S: <OpName>`, including the 5 matmul handlers). 148 vs
  ACT's 150 / DVE's 182 — PE's leaner handler set yields fewer logs.
* **PERF** (the production/release flavor; default when no `NEURON_UCODE_FLAVOR` override) strips
  ALL `S:` logs: DRAM shrinks to `0x2a40`, only 15 strings survive — all assertion source-paths
  (`…/exception_handler.hpp:82/84/86`, `…/decode/{alu_op,branch,move}.cpp`, `…/signal_handler.cpp`)
  + the `ok_to_evict` WARNING + `Assertion failure!`. The log call-sites are gone, so distinct-IVP
  **rises** to 307 (PERF inlines/schedules more vector compute).
* **TEST** sits between: 0 `S:` logs but 58 strings — keeps function-name / file symbols for assert
  context (`fetch_cache_line`, `get_window_addr`, `enter_run`, `sunda_handle_surprises`,
  `sunda_redirect`, `soc_window_manager.hpp`, `push_unallocated_window`, …) — a symbol/assert build.
* **The dispatch mechanism is invariant** across all three: same reset vector (`06 76 00 00`),
  same DEBUG table @ `0x80814` / PERF table @ `0x80218`, same `S: Dispatch opcode` log, same
  ErrorHandler arms (`Bad Opcode`/`Illegal Instruction`/`FP Error`/`Int Div Zero`), same
  `cayman/seq/` codebase. **A DEBUG→RELEASE(PERF) swap is a pure observability change**, not a
  functional/dispatch change. `[HIGH/OBSERVED]`

---

## 9. PE ↔ PSUM / SBUF interaction

The PE engine is the NeuronCore systolic matrix-multiply array. Its 5 PE-specific handlers map
onto the classic **stationary-weight** systolic dataflow (the PE cluster Q7 cores + SBUF; PSUM =
the matmul accumulator banks):

```text
PE:   SBUF weights      --Ldweights (0x01)-->  systolic array (stationary tile)
      SBUF activations  --Matmul    (0x02)-->  array  --accumulate down columns-->  FP32 PSUM
      tag metadata      --LdTags    (0x06)-->  array  (per-column/per-pass tags)
      array control CSRs --PeRegWrite(0x03)-->  PE-array config (pass count, dtype, accum, geometry)
      sparse-weight pass --MatmulSparse(0x07)-> array  (S4D3_MM: 2/3/4 fmaps per partition)
ACT:  PSUM  --Activate (src TENSOR3D, AllowedInPSUM=True)-->  affine+PWL  -->  SBUF / output
```

* **`Ldweights` ← SBUF**: load the stationary weight matrix into the PE cells (SBUF-only,
  `AllowedInPSUM=False`).
* **`Matmul` → PSUM**: stream the moving activation matrix (from SBUF) through the loaded array;
  each cell multiply-accumulates; partial sums accumulate down the columns into the FP32 PSUM
  banks. Internal accumulation is FP32; CAYMAN `out_dtype` is FP32 only (BF16 drain is v4+).
* **`MatmulSparse` → PSUM**: the same pass with a sparse/pruned weight set (4-D source).
* **`PeRegWrite`**: program the PE-array control CSRs.
* **The drain**: the **ACT** engine's `Activate` op reads PSUM directly (its `TENSOR3D` src is
  `AllowedInPSUM=True`), applies the affine + PWL, and writes SBUF.

> **CORRECTION — PSUM is drained by ACT's `Activate`, not by `0x24 ActivationReadAccumulator`.**
> `Activate`'s `TENSOR3D` source is `AllowedInPSUM=True`, so it reads the PE PSUM directly. The
> `0x24 ActivationReadAccumulator` op reads the **ACT engine's own** per-lane fp32 reduction
> accumulator — a *different* register, not the PE PSUM. (Earlier surveys conflated the two.)
> `[HIGH/OBSERVED enum + CARRIED from pe-matmul.md §4.4 / activate-pwl.md]`

**Confidence:** the handler NAMES (`Ldweights`/`Matmul`/`MatmulSparse`/`LdTags`/`PeRegWrite`) are
HIGH/OBSERVED. The SBUF→array→PSUM **binding** is INFERRED-MED: the CAYMAN matmul handlers carry
**no** verbose SBUF/PSUM operand log strings in the image (they read operands from the fetch
cursor + decoded-instruction param block per the SEQ decode interface), so the PSUM/SBUF mapping is
derived from the handler names + the documented NeuronCore systolic-matmul hardware model + the ACT
`Activate` complement, not read directly from a PE firmware string. The byte-exact operand structs,
the `arr_seq` CSR handshake (`matmul_done_last`, `inter_instr_dly`, the per-tile perf banks) and
the PSUM bank geometry (8 banks × 2048 fp32 cells) are in
[pe-matmul.md §4/§5/§10](../firmware/kernels/pe-matmul.md). `[INFERRED-MED binding; HIGH names]`

---

## 10. Engine-model classification

PE is a **SEQ-style ASCII-opcode dispatch engine** (the NX-class sequencer model), the same
`cayman/seq/` firmware as ACT/DVE/POOL/SP, compiled with the matmul/weight-load handler subset.
The "same SEQ engine, different handler subset" hypothesis is **CONFIRMED for PE**.

| property | PE (this image) | SEQ / POOL | ACT / DVE |
|---|---|---|---|
| packaging | flat IRAM/DRAM segments | flat IRAM/DRAM | flat IRAM/DRAM |
| reset vector | `j 0x1dc` (`06 76 00 00`) | `j 0x1dc` | `j 0x1dc` |
| dispatch base | DRAM `0x80814` (DEBUG) / `0x80218` (PERF) | DRAM `0x80814` | DRAM `0x80814` |
| entry size | 4 B (direct IRAM target) | 4 B | 4 B |
| lookup | compare-chain (DEBUG) / addx4 table (PERF) | direct-indexed jump | direct-indexed jump |
| handler form | C++ handler + `S:` log | C++ handler + `S:` log | C++ handler + `S:` log |
| miss policy | ErrorHandler `Bad Opcode` | ErrorHandler `Bad Opcode` | ErrorHandler `Bad Opcode` |
| source tree | `cayman/seq/src/…` | `cayman/seq/src/…` | `cayman/seq/src/…` |
| distinct handlers | **24** (5 PE-specific matmul) | POOL 41 | ACT 26 / DVE 53 |
| compute subset | `Ldweights`/`Matmul`/`MatmulSparse`/`LdTags`/`PeRegWrite` (+`EngineNop`) | pool/reduce/gather/sort/dequant | activation (ACT) / data-vector (DVE) |

PE is the **control/sequencer front-end** for the systolic-matmul array (`engine_idx = 0`): it
decodes the instruction stream and routes weight-load and matmul-pass opcodes to the array, whose
vector datapath (307 IVP ops in PERF) runs the multiply-accumulate. The PE cluster pairs this NX
sequencer core with the systolic-array Q7 cores + the SBUF it reads weights/activations from and
the PSUM accumulator banks it writes. The PE firmware carved here is the program for the PE NX
**sequencer**, not the array datapath itself. PE is the **leanest** of the five compute subsets:
just the matmul pipeline. `[HIGH/OBSERVED]`

---

## 11. Honesty ledger

**HIGH / OBSERVED (this session):**

- 14 `CAYMAN_NX_PE` getters indexed (8 real + 6 zero-size boundary cursors → next-engine POOL IRAM
  `0xa0600`/`0x1b1420`/`0x11c920`); 8 real carves byte-identical (sha256) to the `libnrtucode.a`
  member `.rodata` (8/8). PE PERF_IRAM = `13ba3969`.
- All carves FLAT (no ELF magic); reset vector `06 76 00 00` (`j 0x1dc`) identical across
  DEBUG/PERF/TEST and across all 5 engines; boot `const16 a0,0x90; jx`; 2nd vector `j 0x1e8 → halt
  0`. Per-IRAM entry/retw/IVP census (DEBUG 525/730/179, PERF 150/187/307, TEST 133/191/316).
- Dispatch: DEBUG sub-table @ DRAM `0x80814` (`0x3c44,0x3c5c,0x3c92,…`), PERF clean addx4 table @
  `0x80218` (default arm `0x5e0c`), `S: Dispatch opcode=0x%x` @ `0x80868`, dual HW-Decode/Sunda
  mode, ErrorHandler arms.
- 24 distinct DEBUG handler names; the 5 PE-specific (`Ldweights`/`Matmul`/`MatmulSparse`/`LdTags`/
  `PeRegWrite`) each verified via its IRAM `const16 a10,<low16>`↔DRAM-string xref; the logger
  `0x154d4` an `entry` function. 18-handler shared SEQ core (5-way intersection) + `EngineNop`.
- PROF_CAM 16-byte `{opcode,mask=0xff,en=1,rsvd}` × 47; PROF_CAM (`8fd7e422`) / PROF_TABLE
  (`ce761f81`) byte-identical across ACT/DVE/PE/POOL (4/4); SP no PROF.
- **`PeManageSeed`/MX/`ConvLutLoad` absent from all 3 PE variants** (0 hits for `ManageSeed`/`MX`/
  `seed`/`xorwow`/`ConvLut`). The v4-first-ship is CARRIED from pe-matmul.md §11.
- Engine identity string `engine_base_addr…→engine_idx`; gen anchor `S: BEGIN on cayman`.

**MED / INFERRED:**

- The byte-exact full per-opcode dispatch table (DEBUG compare-chain + 17-entry sub-table; PERF
  addx4 table, FLIX-interleaved) — base/default-arm/log/model are HIGH; the per-opcode list is MED.
- PROF_TABLE field schema (header `0x201`/`0x26000010` + descriptor).
- The PE↔PSUM/SBUF dataflow binding (`Ldweights`←SBUF, `Matmul`→PSUM, ACT `Activate` drains PSUM):
  handler NAMES OBSERVED; the binding INFERRED-MED (no SBUF/PSUM operand string in the PE image).
- The CAYMAN-specific operand-struct byte layout (v3 `TENSOR3D`, no `flags`/`seed_mode` byte) is
  CARRIED from [pe-matmul.md §3.2/§4](../firmware/kernels/pe-matmul.md).

**LOW / NOT CLAIMED:**

- Which silicon part / runtime selects DEBUG vs PERF vs TEST (host driver + `NEURON_UCODE_FLAVOR`).
- The exact per-opcode operand layout of each PE matmul handler (decoded in pe-matmul.md, not here).
- The PE matmul HW cycle cost (systolic fill/drain latency — array timing is HW, out of corpus).

---

## 12. Cross-references

- [Image Catalog Index](./image-catalog-index.md) — the full 266/386-getter map (CAYMAN NX_PE rows).
- [PE Matrix-Multiply Path](../firmware/kernels/pe-matmul.md) — the byte-exact per-gen operand
  structs, `PeManageSeed`/MX semantics, the `arr_seq` CSR handshake, and the CAYMAN→MARIANA→
  MARIANA_PLUS→MAVERICK evolution this image baselines.
- [MARIANA × PE image](./mariana-pe.md) — the v4 diff (adds `PeManageSeed`/`LdweightsMX`/`MatmulMX`/
  `ConvLutLoad`; `S3_LW`/`S3D3_MM` gain the `flags`/`seed_mode` byte + BF16 `out_dtype`).
- [PROF_CAM / PROF_TABLE formats](./prof-cam-table-formats.md) — the generic HW-decode profiling
  CAM/table shared byte-for-byte across the 4 NX engines.
- [ISA Batch 04 — Integer MAC](../isa/ref/b04-mac-integer.md) and
  [Batch 05 — MAC (mixed-sign / wide-acc)](../isa/ref/b05-mac-mixed.md) — the per-instruction
  reference for the `ivp_mul4t*`/`mulpa*`/`mulus*`/`packvr*` widening-MAC family the matmul issues.
- [SparsityCompress / SparsityCompressTag](../firmware/kernels/sparsity-compress-tag.md) — the DVE
  producer of the tags this engine's `MatmulSparse`/`LdTags` consume.
- The sibling CAYMAN engine images: [× ACT](./cayman-act.md), [× DVE](./cayman-dve.md),
  [× POOL](./cayman-pool.md), [× SP](./cayman-sp.md).
- [Confidence & Walls Model](../reference/confidence-model.md) — the tag taxonomy.
