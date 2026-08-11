# StreamTranspose (DVE datapath transpose)

**StreamTranspose** (opcode **`0x6b`**, `NEURON_ISA_TPB_OPCODE_STREAM_TRANSPOSE`) is the
GPSIMD **datapath / compute transpose**: a *"32 element × 32 channel transpose of the data"*
(verbatim, `aws_neuron_isa_tpb_s4d4_tr.h:22`) executed by the **DVE (Decoupled Vector Engine)** as a
register-resident lane permutation, with the data streaming through SBUF/PSUM in the compute
pipeline. It is the **distinct counterpart** to the *descriptor-level* transpose of the
[DGE reshape engine](../dge/dge-reshape.md): the DGE transpose is a stride-permutation realized by the
**DMA crossbar HW** during a transfer (a `DmaGatherTranspose` descriptor, 16×128 xbar tiles); the
StreamTranspose is a **lane-permute** realized by the **vector datapath** (the `sel`/`dsel`
butterfly), 32×32 tiles, no HBM round-trip. Two different transpose mechanisms, at two different
layers, on two different engines.

StreamTranspose is one of the **eleven opcodes** that share the **`NEURON_ISA_TPB_S4D4_TR_STRUCT`**
64-byte operand struct (the same struct the [Tensor-Reduce](tensor-reduce.md §6),
[Copy/Cast](cast-copy.md), [TensorCumulative](tensorcumulative.md) and StreamShuffle families bind
to). For a transpose, the `op@36` ALU-op field — which *is* the reduce/scalar operator for those
siblings — is pinned to **`Bypass(0x00)`**: StreamTranspose is a **pure data-movement op**, no ALU,
no cast, position-only.

Everything below is derived from static analysis of the shipped GPSIMD device images (the
`img_*_NX_DVE_*` members of `libnrtucode.a`, mirrored into `libnrtucode_internal.so`), the shipped
clean C arch-isa headers (`aws_neuron_isa_tpb_s4d4_tr.h`, `aws_neuron_isa_tpb_common.h`,
`instruction_mapping.json`), the shipped Cadence `xtensa-elf-objdump`/`readelf`
(`XTENSA_CORE=ncore2gp`) and stock binutils/`gcc`. Every claim carries a confidence tag
`HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`.

> **Audience.** Senior C++/LLVM engineer reconstructing a Vision-Q7 *Cairo* (`ncore2gp`) GPSIMD DVE.
> See the [confidence model](../../reference/confidence-model.md) for the tag semantics.

---

## 1. Executive summary — the verdict up front

* **WHAT IT IS** — opcode **`0x6b`** (`'k'`), a **DVE-engine compute/datapath** instruction performing
  a *"32 element × 32 channel transpose of the data"* (`s4d4_tr.h:22`, OBSERVED verbatim). It swaps a
  tensor's **partition (channel) axis** with its **free (element) axis** in **32×32 tiles**, streamed
  over a 4-D strided access pattern. `[HIGH/OBSERVED — header + opcode byte]`

* **WHERE IT RUNS** — **device-side, in the DVE engine exclusively.** The decode-type labels
  `S: Stream-Transpose` / `S: Tensor-Cumulative/Copy/Cast/Stream-Shuffle` appear **only** in the DVE
  NX image (`img_*_NX_DVE_*`) and are **absent** from the other four NX engine images
  (ACT/PE/POOL/SP). The DGE descriptor transpose (`tensor_reshape_transpose`, see
  [dge-reshape.md](../dge/dge-reshape.md)) is a *shared* firmware label across all five engines and is
  executed on the Q7/POOL cores — a different mechanism at a different layer (§6). `[HIGH/OBSERVED —
  grep over all 5 NX engines]`

* **THE OPERAND STRUCT** — `NEURON_ISA_TPB_S4D4_TR_STRUCT`, **64 bytes, compile-verified**
  (`gcc`: `sizeof==64`, every offset measured). One `TENSOR4D` src + one `TENSOR4D` dst +
  `in_dtype`/`out_dtype` + `num_active_channels` + `negated` + **`op:ALU_OP@36`** + `op_dim@37` +
  `mask_enable@38` + `reserved1[5]@39`. For StreamTranspose the **`op@36` field is the transpose
  *mode* selector pinned to `Bypass`** — when `op@36 != Bypass` the same struct/decoder runs a *reduce*
  or *scalar* op instead (§3, §4). `[HIGH/OBSERVED — compile-verified]`

* **THE ALGORITHM** — a **tiled 32×32 lane transpose** composed from the DVE **two-source lane-permute
  network** (`ivp_dsel*` / `ivp_sel*` — the [B21 select/shuffle batch](../../isa/ref/b21-select-shuffle.md)),
  **not** a dedicated HW transpose instruction (no `ivp_*transp*` mnemonic exists in the shipped
  toolchain — OBSERVED absence). A 32×32 in-register transpose is the textbook
  **log₂(32) = 5-stage `dsel` butterfly**; the `2NX8/NX16/N_2X32` width form is keyed to the dtype byte
  width. `[geometry HIGH/OBSERVED; lane-permute composition INFERRED-HIGH; per-instruction body NOT
  byte-recoverable — FLIX desync, §5]`

* **DATAPATH-vs-DESCRIPTOR boundary** — the explicit boundary this page nails down. DVE StreamTranspose:
  **compute opcode** on the DVE engine, **32×32 lane tiles**, all **≤32-bit** dtypes,
  **SBUF/PSUM↔SBUF/PSUM** in-pipe, transpose = **lane permute** in the vector registers. DGE
  `DmaGatherTranspose`: **descriptor-gen** on Q7/POOL, **16×128 xbar tiles**, **2B** dtypes,
  **HBM↔SBUF**, transpose = **stride permutation** realized by the DMA xbar HW. Distinct mechanisms,
  engines, tile geometry (§6).

* **PER-GEN PRESENCE** — the ISA (opcode `0x6b` + the `S4D4_TR` struct + `is_valid_stream_transpose`
  predicates) is present **SUNDA(v3) → MARIANA_PLUS(v4+)** (struct byte-identical, opcode stable). The
  verbose DVE decode-**trace** arm (the `S: Stream-Transpose` label + per-type print dispatch) is
  **CAYMAN+** only — SUNDA's DVE DEBUG image carries no `S:` decode-trace table. v5/MAVERICK is
  header-OBSERVED only; interiors INFERRED (§7).

---

## 2. The dispatch chain — DVE decode arm, byte-exact

### 2.1 Where StreamTranspose lives — the DVE engine

The GPSIMD has five NX compute engines (ACT, DVE, PE, POOL, SP), each shipped as IRAM/DRAM blobs
inside `libnrtucode.a` (`img_<GEN>_NX_<ENG>_<BUILD>_<SEG>_contents.c.o`, whose x86 `.rodata` *is* the
raw device image). Carving the CAYMAN NX DEBUG DRAM for each engine and grepping the transpose label
isolates StreamTranspose to the DVE alone:

| NX engine | `Stream-Transpose` | `Stream-Shuffle` | `tensor_reshape_transpose` (DGE label) |
|---|:---:|:---:|:---:|
| ACT | 0 | 0 | 1 |
| PE | 0 | 0 | 1 |
| POOL | 0 | 0 | 1 (+ `S: TensorGather`, `tensor_reshape_indirect_transpose`) |
| SP | 0 | 0 | 1 |
| **DVE** | **1** (`0x1ed2`) | **1** (`0x1ea3`) | 1 |

`[HIGH/OBSERVED — `rg -a -c` over all five carved CAYMAN NX DEBUG DRAMs]`

> **CORRECTION — the DGE-transpose discriminator.** FW-59 §1b implied the POOL
> image is distinguished by carrying *"GATHER TRANSPOSE"*. In fact **no literal
> `"GATHER TRANSPOSE"` string exists** in any engine. The DGE descriptor-transpose label
> `tensor_reshape_transpose` is present in **all five** NX engine DRAMs (shared firmware string), so it
> is **not** a POOL discriminator. The POOL-specific DGE strings are `S: TensorGather` and
> `tensor_reshape_indirect_transpose` (the DGE gather/reshape engine — [dge-reshape.md](../dge/dge-reshape.md)).
> What **is** DVE-exclusive — and the actual discriminator — is `S: Stream-Transpose` / `S: Stream-Shuffle`:
> only the DVE DRAM carries them (`1/1`; all other engines `0/0`). The StreamTranspose **opcode** lives
> only in the DVE. `[HIGH/OBSERVED]`

The DVE engine identity is independently witnessed by its DVE-only strings in the same DRAM:
`S: DVE perf mode support = %d` (`0x122f`), `S: DveReadAccumulator` (`0x28c0`),
`S: DveReadIndices` (`0x2ea0`), the dispatch banner `S: Dispatch opcode=0x%x` (`0x0de0`),
`S: NX in HW Decode mode` (`0x0e36`), and the `seq/src/uarch.hpp` source-path label. *DVE* =
*Decoupled Vector Engine*, the GPSIMD NX vector compute engine; StreamTranspose runs in **this**
engine's datapath.

> **GOTCHA — there are two GPSIMD "transposes" and they are NOT the same instruction.** A
> reimplementer scanning the firmware for a transpose will find `tensor_reshape_transpose` (the DGE
> descriptor builder) and may wrongly conclude *that* is "the GPSIMD transpose". It is not the
> StreamTranspose opcode — it is the DMA-xbar **descriptor** transpose (§6), and its label string is a
> **shared firmware string present in all five NX engine images** (not a POOL discriminator).
> StreamTranspose `0x6b`, by contrast, is **DVE-decoded only** — its `S: Stream-Transpose` arm exists
> in no other engine. The opcode lives in the DVE; the descriptor builder is a Q7/POOL helper. The two
> are distinct mechanisms at distinct layers. `[HIGH/OBSERVED]`

### 2.2 The DVE decode-type table

The DVE NX sequencer (the HW-decode-mode instruction decoder, `seq/src/uarch.hpp`) decodes each
instruction word and, **per instruction type**, routes to a decode arm that emits a debug type-label.
The label table (CAYMAN DVE DEBUG DRAM file-offsets; device VA = offset + `0x80000`, OBSERVED
byte-exact):

```
0x1e6c "S: TensorLoad"            0x1e96 "S: POLL_SEM"
0x1ea3 "S: Tensor-Cumulative/Copy/Cast/Stream-Shuffle"   <- the shared S4D4_TR arm
0x1ed2 "S: Stream-Transpose"                              <- StreamTranspose's OWN arm
0x1ee7 "S: Dropout"              0x1ef3 "S: Tensor-Scalar"
0x1f1b "S: Tensor-Reduce"        0x1f2d "S: MEMSET/RNG"
0x1ff8 "S: Pool"                 0x2060 "S: Tensor-Tensor"
0x23b0 "S: MOVE"                 0x2715 "S: MoveShape(...)"
0x28c0 "S: DveReadAccumulator"   0x2ea0 "S: DveReadIndices"   ...
```

> **NOTE — StreamTranspose has its own decode arm; StreamShuffle does not.** `StreamShuffle(0x6a)`
> shares the `Tensor-Cumulative/Copy/Cast/Stream-Shuffle` arm (label `0x1ea3`): all of those are
> `op=Bypass`, `same_src_dst_count`, no transpose — they use the **common** `S4D4_TR` decode template.
> `StreamTranspose(0x6b)` is **split into its own arm** (label `0x1ed2`) because its validity adds the
> **transpose-specific** preconditions — the 32-channel multiple, the 32-element src count, and the
> `same_or_fewer` dst count (§4) — that the common path does not enforce. The split is the cleanest
> single proof that StreamTranspose is decoded *differently* from its struct-siblings.
> `[HIGH/OBSERVED — strings]`

### 2.3 The StreamTranspose decode arm

The decode arm is the canonical Vision-Q7 decode-handler shape (entry, emit type-label, read the
64-byte struct's mem-patterns, run the body), at CAYMAN DVE IRAM `0x95ec`:

```
95ec: 36 c1 00     entry   a1, 96         ; 96-byte frame (spill for the 4-D src+dst patterns)
95ef: c6 ff ff     j       0x95f2
95f2: a4 08 00     const16 a10, 0x0008    ; hi half of the string VA
95f5: a4 d2 1e     const16 a10, 0x1ed2    ; a10 = 0x00081ed2 = "S: Stream-Transpose"
95f8: 65 a1 0e     call8   0x18010        ; the DVE debug-log fn
95fb: 46 00 00     j       0x9600
9600..: read the S4D4_TR operand struct — const16 a2, 0x00082160 (decode-metadata table) ;
        l32i.n ×4 spilling the 4 mem-pattern words (s32i.n a1,32/36/40/44) ; call8 0xeff0 ;
        then the streaming transpose body begins at 0x9620 with an `8f` F-format FLIX selector byte.
```

`[HIGH/OBSERVED — through 0x961e]`. The two `const16 a10` halves build the 32-bit string VA
`0x00081ed2` (hi `0x0008` then lo `0x1ed2`) — the Xtensa idiom for loading a wide immediate; the
target is exactly the `S: Stream-Transpose` label from §2.2.

### 2.4 Cross-gen re-decode — identical arm shape

The same arm re-decodes identically across CAYMAN/MARIANA/MARIANA_PLUS — only the string VA and the
print-fn VA shift with the image layout:

| gen | IRAM entry | arm |
|---|---|---|
| CAYMAN | `0x95ec` | `entry a1,96 ; const16 a10,0x00081ed2 ; call8 0x18010` |
| MARIANA | `0x99f0` | `entry a1,96 ; const16 a10,0x00081fb3 ; call8 0x188a4` |
| MARIANA_PLUS | `0x99f4` | `entry a1,96 ; const16 a10,0x00081fb8 ; call8 0x19a94` |

There is **one** `const16`-lo reference to the Stream-Transpose string per image — one decode arm.
`[HIGH/OBSERVED — bytes]`

### 2.5 Honest disassembly limitation — the FLIX desync

> **GOTCHA — the transpose *body* is FLIX-desynced and NOT byte-recoverable.** The DVE IRAM is a
> linked, hand-scheduled FLIX/VLIW image (bundles up to 32 B) with literal pools interleaved into
> `.text` and **no `.symtab`**. The **decode arm** (entry + type-print + struct-mem-pattern read,
> §2.3 through `0x961e`) decodes cleanly under a linear sweep and is HIGH/OBSERVED. **Past `0x9620`**
> the streaming-transpose body is a hand-scheduled FLIX span whose `8f`/`0f` F-format lead bytes
> render as `.byte` + bogus targets under stock `objdump`'s linear sweep — its per-instruction
> lane-permute schedule is **not** byte-recoverable. The algorithm (§5) is therefore recovered
> **structurally** from the header geometry (HIGH), the DVE IRAM's surviving IVP lane-permute
> vocabulary (OBSERVED), and the [B21 select/shuffle model](../../isa/ref/b21-select-shuffle.md)
> (reconciled) — tagged INFERRED-HIGH, never fabricated. The body's surviving constants
> (`movi.n a9,-32` ; `movi a2,240` ; `movi a8,-32` ; `movi a8,127` ; `movi a8,-128`) corroborate
> 32-lane processing + the 7-bit (`0x7f=127`) `dsel`/`sel` lane indices of B21, but no clean
> instruction trace is asserted.

---

## 3. The instruction family — `S4D4_TR` (eleven opcodes, one struct)

`instruction_mapping.json` (`struct2opcode`, OBSERVED) binds `NEURON_ISA_TPB_S4D4_TR_STRUCT` to
**eleven** opcodes — read byte-exact:

```
NEURON_ISA_TPB_S4D4_TR_STRUCT -> [
  TENSOR_REDUCE_ARITH_OP(0x42),  TRANSPOSE_TENSOR_REDUCE_ARITH_OP(0x83),
  TENSOR_REDUCE_BITVEC_OP(0x52), TRANSPOSE_TENSOR_REDUCE_BITVEC_OP(0x84),
  TENSOR_CUMULATIVE_ARITH_OP(0x4E), TENSOR_CUMULATIVE_BITVEC_OP(0x5E),
  COPY(0x46), CAST(0x47), RECIPROCAL(0x48),
  STREAM_SHUFFLE(0x6a), STREAM_TRANSPOSE(0x6b) ]
```

The reverse query — *which struct backs `STREAM_TRANSPOSE`* — returns exactly
`NEURON_ISA_TPB_S4D4_TR_STRUCT` (sole owner). `[HIGH/OBSERVED — `jq` over `instruction_mapping.json`]`

The header docstring (`s4d4_tr.h:13-22`) enumerates the **eight logical instruction families** the
struct serves, verbatim:

| family | header docstring | opcodes |
|---|---|---|
| `TensorReduce[Arith/Bitvec]` | *"reduction … along a given axis"* (incl. `CRC32` poly-reduce) | `0x42`/`0x52` |
| `TransposeTensorReduce[A/B]` | *"same as TensorReduceOp, but **preceeded by a 32 element x 32 channel transpose**"* | `0x83`/`0x84` |
| `TensorCumulative[A/B]` | *"cumulative … along an axis"* (prefix scan) | `0x4E`/`0x5E` |
| `Copy` | *"copies data … with optional reshape"* | `0x46` |
| `Cast` | *"casts the data type"* | `0x47` |
| `Reciprocal` | *"element-wise reciprocal"* | `0x48` |
| `StreamShuffle` | *"cross-channel data movement within a set of 32 channels"* | `0x6a` |
| **`StreamTranspose`** | ***"performs a 32 element x 32 channel transpose of the data"*** | **`0x6b`** |

> **The 32×32 transpose appears in TWO forms.** Standalone as **StreamTranspose `0x6b`**, and **fused**
> as the prefix of **`TransposeTensorReduce` `0x83`/`0x84`** — *"same as TensorReduceOp, but preceeded
> by a 32 element x 32 channel transpose"* (`s4d4_tr.h:16`). The opcode (`0x42` vs `0x83`) tells the
> decoder whether to prepend the transpose before the cross-axis reduce. The DVE decode-trace folds the
> fused form under `S: Tensor-Reduce` (`0x1f1b`); the standalone form gets its own `S: Stream-Transpose`
> arm (§2.2). The transpose-tensor-reduce path is the [cross-lane-reduce](cross-lane-reduce.md) /
> [tensor-reduce](tensor-reduce.md) sibling that re-uses **this** 32×32 network.
> `[HIGH/OBSERVED — header text + opcode]`

### 3.1 The `op@36` field — transpose-mode selector vs reduce/scalar op

This is the byte-identical struct the committed [tensor-reduce.md §6](tensor-reduce.md) and
[cast-copy.md](cast-copy.md) decode. The field layout is **byte-identical** across all those families;
they diverge only in **how `op@36` is interpreted and validated**:

| family | `negated@35` | **`op@36` (`ALU_OP`)** | `op_dim@37` | `mask_enable@38` |
|---|---|---|---|---|
| `TensorReduce` (`0x42`/`0x52`) | reduce-negate flag | **the reduce ALU op** (`ADD`/`MAX`/… — 60-op space) | the free axis to fold | `0` |
| `TensorCumulative` (`0x4E`/`0x5E`) | `0` | **the scan ALU op** | the scan axis | `0` |
| `Copy`/`Cast`/`Reciprocal` (`0x46`/`0x47`/`0x48`) | `0` | **`Bypass`** | `0`-subdim | `0` |
| `StreamShuffle` (`0x6a`) | `0` | **`Bypass`** | `0`-subdim | `0` |
| **`StreamTranspose` (`0x6b`)** | **`0`** | **`Bypass(0x00)`** | **`0`-subdim** | **`0` or `1`** |

> **The `op@36` ALU-op field IS the "transpose mode" selector — pinned to `Bypass` for a transpose.**
> `aws_neuron_isa_tpb_common.h:940` defines `NEURON_ISA_TPB_ALU_OP_BYPASS = 0x00`. For the *reduce*
> siblings `op@36` carries the reduction operator (the full 60-entry `ALU_OP` space — `ADD=0x04`,
> `MAX=0x08`, `MIN=0x09`, `BITWISE_AND=0x0A`, `BITWISE_OR=0x0B`, …); for StreamTranspose the
> `is_valid_stream_transpose` predicate `s4d4_tr_op_bypass(i)` (`s4d4_tr.h:336`, OBSERVED) **forces**
> `op@36 == Bypass`. So StreamTranspose is the `op=Bypass` *position-only* mode of the shared decoder:
> the same `op@36` byte that selects the reduce arithmetic for `0x42` here selects "no arithmetic, move
> the bytes through the transpose network only". A reimplementer must read `op@36` and **reject**
> StreamTranspose if it is not `Bypass`. `[HIGH/OBSERVED — header predicate + `ALU_OP` enum]`

> **CORRECTION.** FW-59 §0/§4 (and tensor-reduce.md §6) describe `op@36` as
> *"`op:ALU_OP`"*. That is correct as the **field type**, but for StreamTranspose specifically the
> field is **not** a free ALU op — it is the validity-pinned transpose-mode byte that *must* equal
> `Bypass`. This page makes that explicit: `op@36` is shared real estate whose meaning is opcode-keyed,
> and for `0x6b` it is a degenerate (constant-`Bypass`) selector. There is **no** separate
> "transpose-mode" byte beyond `op@36 = Bypass` + `mask_enable@38`. `[HIGH/OBSERVED]`

---

## 4. The operand struct — `NEURON_ISA_TPB_S4D4_TR_STRUCT` (64 B, compile-verified)

Source: `aws_neuron_isa_tpb_s4d4_tr.h:28` (CAYMAN, *"ISA header for NC-v3"*). Reproducing the header's
`typedef struct` and compiling with `gcc -I <cayman tpb>`, the `ISA_STATIC_ASSERT(sizeof == 64)` holds
and every offset is measured (`op@36`, `op_dim@37`, `mask_enable@38`, `dst@44`):

```c
typedef struct NEURON_ISA_TPB_S4D4_TR_STRUCT {
    NEURON_ISA_TPB_HEADER        header;               //  4   ( 0 -  3)  {opcode=0x6b, inst_word_len, debug_cmd, debug_hint}
    NEURON_ISA_TPB_EVENTS        events;               //  8   ( 4 - 11)  wait/update semaphore sync
    NEURON_ISA_TPB_TENSOR4D      src_mem_pattern;      // 20   (12 - 31)  INPUT  4-D strided pattern
    NEURON_ISA_TPB_DTYPE         in_dtype;             //  1   (32     )  source element dtype
    NEURON_ISA_TPB_DTYPE         out_dtype;            //  1   (33     )  dest   element dtype (== in for ST)
    uint8_t                      num_active_channels;  //  1   (34     )  partition/channel count (∈ {32,64,96,128})
    uint8_t                      negated;              //  1   (35     )  ST: MUST be 0
    NEURON_ISA_TPB_ALU_OP        op;                   //  1   (36     )  ST: MUST be Bypass(0x00) — the mode byte (§3.1)
    NEURON_ISA_TPB_TENSOR_SUBDIM op_dim;               //  1   (37     )  ST: MUST be zero-subdim ({UNUSED=0, X=2})
    uint8_t                      mask_enable;          //  1   (38     )  ST: 0 or 1 (partial-tile mask)
    uint8_t                      reserved1[5];         //  5   (39 - 43)  MUST be all zero
    NEURON_ISA_TPB_TENSOR4D      dst_mem_pattern;      // 20   (44 - 63)  OUTPUT 4-D strided pattern
} NEURON_ISA_TPB_S4D4_TR_STRUCT;     // 4+8+20+1+1+1+1+1+1+1+5+20 = 64
```

`gcc` reports: `sizeof(S4D4_TR)==64`, `sizeof(TENSOR4D)==20`, `sizeof(ALU_OP)==sizeof(SUBDIM)==
sizeof(DTYPE)==1`, offsets `header@0 events@4 src@12 in_dtype@32 out_dtype@33 num_active_channels@34
negated@35 op@36 op_dim@37 mask_enable@38 reserved1@39 dst@44`. `[HIGH/OBSERVED — compile-verified]`

> **The field layout is BYTE-IDENTICAL to the committed [tensor-reduce.md §6](tensor-reduce.md) and
> [cast-copy.md](cast-copy.md).** StreamTranspose **uses**: `header`, `events`, `src_mem_pattern`,
> `in_dtype`, `out_dtype`, `num_active_channels`, `op@36` (= `Bypass`), `op_dim@37` (= `0`-subdim),
> `mask_enable@38`, `dst_mem_pattern`. It **leaves unused / forces-zero**: `negated@35` (reduce-only),
> `reserved1[5]@39`. `mask_enable@38` is the one field StreamTranspose **uses non-trivially that the
> Copy/Cast/Reduce siblings force to zero** — for them `mask_enable_zero` is asserted; for
> StreamTranspose `mask_enable_valid` permits `{0,1}` (§4.1).

### 4.1 `NEURON_ISA_TPB_TENSOR4D` (20 B)

```c
typedef struct NEURON_ISA_TPB_TENSOR4D {
    NEURON_ISA_TPB_ADDR4 start_addr;    //  4  (union: addr-immediate | addr-from-register; markers
                                        //      SHAPE_REG=0x40, ADDR_REG=0x80, ADDR_SHAPE_REG=0xC0, INDIRECT_IMM=0x20)
    int16_t              step_elem[4];  //  8  per-dim SIGNED element stride (negative = reverse-axis walk)
    uint16_t             num_elem[4];   //  8  per-dim element count (loop bound)
} NEURON_ISA_TPB_TENSOR4D;             // = 4 + 8 + 8 = 20  (compile-verified)
```

The src `start_addr` `ADDR4` carries the SBUF/PSUM partition-offset encoding; the 4-D
`(num_elem, step_elem)` walk strides over the active partitions × free elements. For StreamTranspose
the **dst walk order is the channel/element-swapped image of the src walk** — the transpose
manifests as the reordering of the dst's `(num_elem, step_elem)` axes relative to the src
(the "transpose = permute the strides" lowering, exactly as the DGE reshape engine realizes it at the
descriptor level — [dge-reshape.md §4.5](../dge/dge-reshape.md)). `[HIGH/OBSERVED struct; the
axis-swap reading INFERRED-HIGH]`

### 4.2 Validity predicates — `is_valid_stream_transpose` (OBSERVED verbatim)

The header's assertion (`s4d4_tr.h:320-340`) is the exact precondition set — every conjunct is a hard
constraint a reimplementer's decoder must enforce:

```c
// fn is_valid_stream_transpose(i: Inst) -> bool {
//       has_valid_neuron_header(i)
//    && has_valid_neuron_events(i)
//    && has_stream_transpose_opcode(i)                                  // opcode == 0x6b
//    && s4d4_tr_reserved_zero(i)                                        // reserved1[0..4] == 0
//    && mask_enable_valid(i)                                            // mask_enable ∈ {0, 1}   <-- NOT _zero
//    && is_valid_dtype(i.s4d4_tr.in_dtype,  DtypeAllowFP32R::False)     // src: NO fp32r, NO u64/i64
//    && is_valid_dtype(i.s4d4_tr.out_dtype, DtypeAllowFP32R::True)      // dst: fp32r OK (but pinned by next line)
//    && s4d4_tr_same_src_dst_type(i)                                    // out_dtype == in_dtype  (NO CAST)
//    && is_zero_subdim(i.s4d4_tr.op_dim)                                // op_dim ∈ {UNUSED(0), X(2)}
//    && has_valid_active_channel_range(i.s4d4_tr.num_active_channels, POOLING_NUM_CHANNELS)   // <= 128
//    && start_addr_active_channels(i.s4d4_tr.src_mem_pattern.start_addr, i.s4d4_tr.num_active_channels)
//    && start_addr_active_channels(i.s4d4_tr.dst_mem_pattern.start_addr, i.s4d4_tr.num_active_channels)
//    && tensor4d_valid(src, in,  WriteTensor::False, AllowedInPSUM::True, AllowedInSBUF::True)  // src RO, SBUF/PSUM
//    && tensor4d_valid(dst, out, WriteTensor::True,  AllowedInPSUM::True, AllowedInSBUF::True)  // dst W,  SBUF/PSUM
//    && s4d4_tr_transpose_src_dst_count(i)                              // same_or_fewer: count(dst) <= count(src)
//    && s4d4_tr_op_bypass(i)                                            // op@36 == Bypass(0x00)  (the mode byte, §3.1)
//    && has_multiple_32_channels(i.s4d4_tr.num_active_channels)         // channels ∈ {32, 64, 96, 128}
//    && has_transpose_src_element_count_t4d(i.s4d4_tr.src_mem_pattern)  // prod(src.num_elem[0..3]) % 32 == 0
//    && has_zero_negated_field(i)                                       // negated == 0
// }
```

The supporting predicates (`common.h`, OBSERVED verbatim):

```c
// fn is_zero_subdim(subdim) -> bool { subdim == UNUSED(0) || subdim == X(0x02) }            // common.h:1410
// fn has_multiple_32_channels(c) -> bool { c==32 || c==64 || c==96 || c==128 }              // common.h:1767  (CLOSED set)
// fn has_transpose_src_element_count_t4d(t) -> bool { (t.num_elem[0]*[1]*[2]*[3]) % 32 == 0 } // common.h:1759
// fn same_or_fewer_element_count_t4d(dst, src) -> bool {                                     // common.h:1862
//       shape_from_register(dst.start_addr) || shape_from_register(src.start_addr)
//    || t4d_element_count(dst) <= t4d_element_count(src) }
// fn mask_enable_valid(i) -> bool { mask_enable == 0 || mask_enable == 1 }                   // s4d4_tr.h:381
// fn s4d4_tr_op_bypass(i) -> bool { op == Bypass }   ;  fn s4d4_tr_same_src_dst_type(i) -> bool { out == in }
// const POOLING_NUM_CHANNELS = 128                                                           // common.h:35
```

> **GOTCHA — `mask_enable` is the one StreamTranspose-specific switch.** For every other `S4D4_TR`
> family (Copy/Cast/Reciprocal/Reduce/Cumulative/StreamShuffle) the validator asserts
> `mask_enable_zero` (mask forced **off**). StreamTranspose alone uses `mask_enable_valid` — it permits
> `mask_enable == 1`, the **partial-tile mask** that lets a transpose drop padding lanes when the live
> tile is smaller than 32×32. This is *why* StreamTranspose got its own decode arm (§2.2): the common
> arm has no place for a `mask_enable=1`. `[HIGH/OBSERVED — header predicate]`

> **CORRECTION — StreamTranspose's dtype set is `is_valid_dtype` (NOT
> `is_valid_dtype_64`).** FW-59 §4a noted *"NO U64/I64"*; this is grounded here: the
> `is_valid_stream_transpose` predicate calls **`is_valid_dtype`**, which internally invokes
> `dtype_uint64_illegal_check(.., DtypeAllowU64::False)` and `dtype_int64_illegal_check(..,
> DtypeAllowI64::False)` (`common.h:1312-1317`) — **U64/I64 are excluded**. The *TensorReduce* family
> on the same struct instead calls `is_valid_dtype_64(.., AllowU64::True, AllowI64::True)` and *does*
> admit 64-bit. So the DVE datapath transpose is strictly **≤32-bit**; the reduce sibling is not. A
> reimplementer who lets a 64-bit dtype reach StreamTranspose accepts an instruction the hardware
> decoder rejects. `[HIGH/OBSERVED]`

> **GOTCHA — `op_dim ∈ {UNUSED(0), X(2)}`, not "X only".** `is_zero_subdim` (`common.h:1410`) accepts
> **both** `UNUSED(0)` and `X(0x02)` (`TENSOR_SUBDIM: UNUSED=0, X=0x02, XY=0x03, XYZ=0x04, XYZW=0x05`).
> StreamTranspose carries no free-axis reduce, so `op_dim` is a don't-care within `{0, 2}` — the
> non-zero `X(2)` legal value is a quirk of the shared `is_zero_subdim` helper, not a transpose
> semantic. `[HIGH/OBSERVED]`

> **NOTE — `out_dtype` declares `fp32r`-allowed but is then pinned to `in_dtype`.** The generic struct
> rule reads `out_dtype` with `DtypeAllowFP32R::True`, but `s4d4_tr_same_src_dst_type` immediately
> requires `out_dtype == in_dtype`, and `in_dtype` forbids `fp32r`. So in practice **one** dtype is
> shared src/dst, never `fp32r`, never cast. The transpose is **pure position movement** — `op=Bypass`
> + `in==out`.

---

## 5. The in-datapath transpose algorithm — tiled 32×32 lane permute

### 5.1 The geometry (HIGH/OBSERVED from §4.2 predicates + docstring)

StreamTranspose transposes a tensor between its **partition (channel) axis** and its **free (element)
axis**, in **32×32 tiles**:

* the **channel axis** is tiled in 32-channel groups — `has_multiple_32_channels`:
  `num_active_channels ∈ {32, 64, 96, 128}` (up to `POOLING_NUM_CHANNELS = 128`);
* the **free/element axis** is tiled in 32-element groups —
  `has_transpose_src_element_count_t4d`: `prod(src.num_elem[0..3]) % 32 == 0`;
* each **32-channel × 32-element tile** is transposed: `element[e]` of `channel[c]` → `element[c]` of
  `channel[e]` within the tile;
* the dst element count may be **`<=`** the src count (`same_or_fewer` — partial/padded tiles dropped);
* `in_dtype == out_dtype` (no value change); the dtype byte-width sets each transposed cell's size (a
  bf16 transpose moves 2-byte cells, an fp32 transpose 4-byte cells).

The "32 element × 32 channel" tile is exactly **one vector-register tile**: the 512-bit GPSIMD vector
register holds **32 × 16-bit (`NX16`) lanes** ([B21 §1.1](../../isa/ref/b21-select-shuffle.md)), so a
32×32 transpose is precisely a single register-tile transpose, streamed over the 4-D pattern.
`[HIGH/OBSERVED — geometry from header predicates + docstring]`

### 5.2 No dedicated HW transpose — it composes from the lane-permute network

> **No `ivp_*transp*` mnemonic exists in the shipped toolchain.** `nm -D` / `strings` over the
> value/decode/encode libs (`libfiss-base.so`, `libisa-core.so`, `libcas-core.so`) find **no** transpose
> mnemonic and no transpose opcode thunk. The 32×32 in-register transpose is therefore **not** a single
> HW instruction — it is **composed** from the DVE two-source lane-permute datapath. `[HIGH/OBSERVED —
> absence]`

The CAYMAN DVE IRAM's surviving IVP vocabulary (full-image objdump harvest) is dominated by exactly the
**two-source lane-permute / dual-output deal ops** the [B21 select/shuffle batch](../../isa/ref/b21-select-shuffle.md)
identifies as the transpose/gather primitives:

| IVP mnemonic | CAYMAN DVE sites | role | B21 shape |
|---|---:|---|---|
| `ivp_sel2nx8i_s4` | **48** | immediate fixed-permute (slot-pinned, shift-4 form) | `SELi` (slot-pinned immediate crossbar) |
| `ivp_dextrprn_2x32` | **34** | dual-extract / reduce-pair | (reduce band) |
| `ivp_dselnx16t` | **18** | **dual-output predicated** 2-source select (16-bit lanes) | `DSEL.T` (butterfly + `vbool` mask) |
| `ivp_sel2nx8i` | **13** | immediate 2-source select (byte lanes) | `SELi` |
| `ivp_dseln_2x32t` | **7** | dual-output predicated 32-bit select | `DSEL.T` (32-bit) |
| `ivp_dselnx16` / `ivp_dseln_2x32` | 1 ea. | dual-output (non-predicated) butterfly | `DSEL` |

Counts are from the `xtensa-elf-objdump` full-image harvest of cay_dve_iram (`rg -o
'ivp_[a-z0-9_]+' | sort | uniq -c`); the top permute primitive is `ivp_sel2nx8i_s4` (48). SUNDA's DVE
IRAM carries the same roster at higher counts (sel2nx8i_s4=153, dselnx16t=120, dextrprn_2x32=49) —
it performs the SW transpose via these primitives even without the `S:` decode-trace logging.

The **DSEL** op is the key primitive: from [B21 §1](../../isa/ref/b21-select-shuffle.md), `DSEL` is the
*dual-output deal/zip* that writes **two** result vectors (`vu`, `vt`) from the same source pair in one
issue — a **butterfly / de-interleave stage**. Its operand descriptor (B21 §1.2) is 5-wide:
`DSELNX16 = {vu(o), vt(o), vs(i), vr(i), sr(i)}` — two outputs, two source vectors, one control. A
32×32 lane transpose is the textbook **butterfly of two-source lane selects**: `log₂(32) = 5` stages of
`dsel`/`sel` that progressively swap lane-vs-position; the dual-output `dsel` emits two transposed
lane-groups per issue, halving the op count.

### 5.3 The transpose network (annotated C pseudocode, INFERRED-HIGH composition)

The standard Tensilica lane-permute transpose idiom, grounded on the B21 `dsel`/`sel` primitives and
the DVE IVP vocabulary. The body is FLIX-desynced so the per-tile schedule is **not** byte-traced — the
selector constants and exact stage interleave sit in the desynced span; the structure below is the
idiom, with the real DVE mnemonics named:

```c
// DVE StreamTranspose (op 0x6b) — the in-datapath 32x32 tile transpose.
// Names the real recovered DVE IVP ops (ivp_dselnx16 / ivp_sel2nx8i_s4) of B21.
// `op@36 == Bypass`, `in_dtype == out_dtype` -> pure position move, no value transform.
//
// One 32x32 tile = 32 vector registers (one per source channel), each holding 32 elements
// across the NX16 SIMD lanes. The transpose makes register[e] (post) hold, in lane[c],
// the value that register[c] (pre) held in lane[e]: a full row<->column swap.

static void dve_stream_transpose_tile_nx16(vec_nx16 r[32] /* in/out: 32 channel registers */)
{
    // log2(32) = 5 butterfly stages. Stage s swaps lane-blocks of width (1 << s)
    // between register pairs separated by (1 << s). Each stage is a sweep of dual-output
    // DSEL ops: one DSEL consumes the pair (r[i], r[j]) and produces the two transposed
    // halves (vu, vt) in one issue -> 16 DSEL issues per stage, 5 stages = the butterfly.
    for (int s = 0; s < 5; ++s) {
        const int stride = 1 << s;                 // register-pair separation this stage
        // CONST_TBL_tab_shflimm-style fixed selector: the 7-bit (0x7f) lane indices that
        // interleave the (1<<s)-wide lane blocks. Immediate, data-independent -> baked into
        // the slot-pinned ivp_sel2nx8i_s4 / the DSEL control register sr.
        const sel_ctrl_t ctrl = transpose_butterfly_ctrl(s);   // selector const (desync-hidden)
        for (int i = 0; i < 32; i += 2 * stride) {
            for (int k = 0; k < stride; ++k) {
                vec_nx16 vu, vt;
                // dual-output deal/zip: writes BOTH transposed lane-groups in one issue.
                // ivp_dselnx16(vu, vt, r[i+k], r[i+k+stride], ctrl)   (B21 DSELNX16, 5-operand)
                ivp_dselnx16(&vu, &vt, r[i + k], r[i + k + stride], ctrl);
                r[i + k]          = vu;
                r[i + k + stride] = vt;
            }
        }
    }
    // After 5 stages r[] is the transposed tile. The dtype-width form is chosen per dtype:
    //   2NX8  (64 lanes, i8)   for 8-bit dtypes  -> ivp_dsel2nx8i  / ivp_sel2nx8i_s4
    //   NX16  (32 lanes, i16)  for 16-bit dtypes -> ivp_dselnx16   (the canonical case above)
    //   N_2X32(16 lanes, i32)  for 32-bit dtypes -> ivp_dseln_2x32
    // (B21 §1.1 lane-grid token; §5.4 below.)
}

// The streaming driver: walk the 4-D src/dst TENSOR4D patterns tile-by-tile.
static void dve_stream_transpose(const S4D4_TR *i)              // op == 0x6b, op@36 == Bypass
{
    const int chan_tiles = i->num_active_channels / 32;        // {32,64,96,128} -> {1,2,3,4}
    const int elem_tiles = t4d_element_count(&i->src_mem_pattern) / 32;  // src count % 32 == 0
    vbool_t mask = i->mask_enable ? partial_tile_mask(i) : VBOOL_ALL_ONES;  // mask_enable@38

    for (int ct = 0; ct < chan_tiles; ++ct) {
        for (int et = 0; et < elem_tiles; ++et) {
            vec_nx16 r[32];
            // (i) strided-load 32 channels x 32 elements via the src TENSOR4D walk (ivp_la*/ivp_lv*)
            load_tile_strided(r, &i->src_mem_pattern, ct, et, i->in_dtype);
            // (ii) transpose in-register via the DSEL butterfly above
            dve_stream_transpose_tile_nx16(r);                  // width form keyed to in_dtype
            // (iii) the predicated _t DSEL forms (ivp_dselnx16t) apply `mask` for partial tiles,
            //       dropping padding lanes when same_or_fewer dropped a partial tile.
            if (i->mask_enable) apply_tile_mask(r, mask);
            // (iv) strided-store with the channel/element-SWAPPED dst walk order
            store_tile_strided(&i->dst_mem_pattern, r, ct, et, i->out_dtype);  // out_dtype == in_dtype
        }
    }
}
```

`[geometry HIGH/OBSERVED; the 5-stage `dsel` butterfly + the streaming loop INFERRED-HIGH from the B21
primitives + the 4-D pattern + the surviving body constants (32/127/128/240); the exact selector
constants and per-tile schedule are FLIX-desync-hidden and NOT byte-traced — §2.5]`

### 5.4 Per-dtype permute-width selection

Because `in_dtype == out_dtype` and `op == Bypass`, the transpose never casts or computes — it moves
bytes, and the **dtype byte-width** is the transposed-cell size. The lane-permute op **width suffix**
matches the dtype width (B21 §1.1 lane-grid token):

| dtype width | B21 width form | SIMD lanes | dtypes |
|---|---|---:|---|
| 8-bit | `2NX8` | 64 | `INT8`/`UINT8`, `FP8_EXP{3,4,5}` |
| 16-bit | `NX16` (canonical) | 32 | `INT16`/`UINT16`, `BFLOAT16`, `FP16` |
| 32-bit | `N_2X32` | 16 | `INT32`/`UINT32`, `FP32` |

So an int8 transpose uses the `2NX8` (64-lane) permute, a bf16/fp16/int16 transpose the `NX16`
(32-lane), an int32/fp32 transpose the `N_2X32` (16-lane). The "32 element × 32 channel" tile is a
*logical* 32×32; the physical SIMD lane count of the permute op is dtype-keyed. `[INFERRED-HIGH from the
`in==out` rule + the B21 width-keyed permute roster]`

---

## 6. The datapath-vs-descriptor boundary (the DGE reshape contrast)

This is the explicit boundary the [DGE reshape engine page](../dge/dge-reshape.md §6.5) flagged from the
other side. There are **two unrelated transpose mechanisms** in the device, at two different layers:

| attribute | **DVE StreamTranspose** (this page) | **DGE `DmaGatherTranspose`** ([dge-reshape.md](../dge/dge-reshape.md)) |
|---|---|---|
| layer | **compute opcode** (executed by the engine) | **descriptor-gen** (builds a 64-B descriptor) |
| engine | **DVE** (Decoupled Vector Engine) | **Q7/POOL** — *"SW-DGE backend with Q7 processors"* |
| opcode / struct | **`0x6b`**, `S4D4_TR_STRUCT` (64 B) | `DMA_GATHER_XPOSE_STRUCT` (64 B; built, not a base opcode) |
| mechanism | **DVE lane-PERMUTE** network (`dsel`/`sel` butterfly) | **DMA XBAR HW** transposes during the transfer |
| transpose realized as | **lane permutation** in the vector registers (in-place tile xpose) | **stride permutation** in the descriptor (`step_elem[]` swap) |
| tile geometry | **32×32** (vector-register tile) | **16×128** (2B-dtype xbar tiles) |
| dtype | **all ≤32-bit** (8/16/32-bit; int/uint/fp8/fp16/bf16/fp32) | **2B only** initially (BF16/FP16), `dtype_lo == dtype_hi` |
| data path | **SBUF/PSUM ↔ SBUF/PSUM** (in-pipe) | **HBM ↔ SBUF** (or SB2SB fast-path) |
| HBM round-trip | **NO** (streams through the datapath) | **yes** (or SB2SB) |
| use case | in-pipeline transpose feeding the next compute op; the fused `TransposeTensorReduce` | bulk reshape/layout (MOE MLP, chunked-prefill attn) |
| presence | ISA SUNDA(v3)+; DVE decode-trace arm CAYMAN+ | reshape full CAYMAN+; ISA V3+ (SUNDA lacks `gather_xpose.h`) |

> **Why two mechanisms exist** `[INFERRED-HIGH, grounded in the header use-cases + the layer split]`. The
> **DGE transpose** is for **moving/reshaping** large tensors across the memory hierarchy — it
> piggybacks the transpose on a DMA that has to happen anyway, amortizing it on the xbar HW. The **DVE
> StreamTranspose** is for **transposing data already in SBUF/PSUM** as a *compute* step — when the next
> op needs the other axis layout (e.g. a reduce along the transposed axis, hence the fused
> `TransposeTensorReduce` `0x83`/`0x84`, §3). Choosing the datapath transpose avoids a DMA descriptor +
> HBM round-trip when the data is small and resident; choosing the DGE transpose avoids occupying the
> compute engine when the move is bulk/memory-bound. The DGE **`sb2sb` fast-path** reshape-transpose
> ([dge-reshape.md §6.4](../dge/dge-reshape.md)) is **still** a *descriptor* transpose (it generates
> SB2SB descriptors) — it is **not** a DVE StreamTranspose. The two are orthogonal and the boundary
> holds exactly. `[HIGH for the mechanism split; the use-case rationale INFERRED-HIGH]`

The descriptor side is documented end-to-end at [dge-reshape.md](../dge/dge-reshape.md); the SB2SB
descriptor ring the `sb2sb` reshape feeds is the
[gather/scatter descriptors](../../dma/gather-scatter-descriptors.md) page.

---

## 7. Per-generation presence

| feature | SUNDA (v3) | CAYMAN | MARIANA (v4) | MARIANA_PLUS (v4+) | MAVERICK (v5) |
|---|:---:|:---:|:---:|:---:|:---:|
| `STREAM_TRANSPOSE` opcode `0x6b` (ISA) | yes | yes | yes | yes | yes¹ |
| `S4D4_TR` struct (64 B, byte-identical) | yes | yes | yes | yes | yes¹ |
| `is_valid_stream_transpose` predicates | yes | yes | yes | yes | yes¹ |
| DVE NX engine image present | yes | yes | yes | yes | —² |
| DVE per-instruction decode-TRACE table (`S:` labels) | **NO**³ | yes | yes | yes | —² |
| StreamTranspose decode ARM in DVE IRAM | n/a³ | `0x95ec` | `0x99f0` | `0x99f4` | —² |

¹ MAVERICK ships its own `arch_isa` dir with `aws_neuron_isa_tpb_s4d4_tr.h`; the `0x6b` opcode + the
64-B struct + the `is_valid_stream_transpose` predicate set are **header-OBSERVED** there. The v5 DVE
**interior** (decode arm, transpose body) was **not** byte-grounded — INFERRED from header + opcode-map
parity. `[HIGH header / INFERRED interior]`

² No MAVERICK `NX_DVE` device image was carved — v5 runtime DVE behaviour is
header-OBSERVED only, not claimed. `[LOW/flagged]`

³ SUNDA's DVE DEBUG DRAM (14 KB vs CAYMAN's 28 KB) carries the `decode.cpp`/`uarch.hpp` **assert**
strings but **not** the per-instruction-type `S:` decode-trace print table — SUNDA's NX used a
minimal/older decode-trace path. The opcode `0x6b` ISA + the `S4D4_TR` struct are fully present in SUNDA
(compile-verified). So the **instruction** exists from v3; the verbose DVE decode-trace machinery is
**CAYMAN+**. The `S4D4_TR` struct is gcc-verified byte-identical across SUNDA/MARIANA/CAYMAN.

> **CORRECTION — engine attribution.** The cross-gen opcode matrix coarsely tags `0x6b` as
> *"NX/POOL"*; the finer evidence (the `S: Stream-Transpose` grep over **all five** NX engines, §2.1)
> shows it is **DVE-exclusive** at runtime. POOL carries only the DGE descriptor transpose. The matrix's
> "NX/POOL" reflects that the `S4D4_TR` struct family is decoded by the NX sequencer family broadly; the
> *StreamTranspose* opcode itself decodes only in the DVE. `[HIGH/OBSERVED]`

---

## 8. Honest limitations / desync flags

* **FLIX-literal desync (the body).** The decode arm (entry + type-print + struct-mem-pattern read,
  §2.3 through `0x961e`) is byte-recoverable and HIGH/OBSERVED. The streaming-transpose body
  (`0x9620 → ~0x9b20`, the `8f`/`0f` F-format bundles) **desyncs** under stock `xtensa-elf-objdump`'s
  linear sweep — the per-instruction lane-permute schedule is **not** byte-recoverable. The algorithm
  (§5) is recovered structurally from the header geometry (HIGH) + the DVE IRAM IVP lane-permute
  vocabulary (OBSERVED) + the [B21](../../isa/ref/b21-select-shuffle.md) lane-permute model (reconciled)
  — INFERRED-HIGH, never fabricated.
* **Selector constants.** The exact butterfly stage count, the immediate-permute selector constants
  (`CONST_TBL_tab_shflimm`-style), and the per-tile streaming schedule sit in the desynced body and are
  reported as the standard Tensilica idiom, not a decoded trace.
* **No HW transpose (OBSERVED absence).** `nm -D`/`strings` over `libfiss-base.so`/`libisa-core.so`/
  `libcas-core.so` find no `ivp_*transp*` — the "no dedicated HW transpose" finding is grounded, not
  assumed.
* **IVP counts.** The `ivp_sel*`/`ivp_dsel*` site counts are taken from the
  `xtensa-elf-objdump` full-image harvest (`rg -o 'ivp_[a-z0-9_]+' | sort | uniq -c`), not the
  decompile; the dominant permute roster is `ivp_sel2nx8i_s4`, `ivp_dselnx16t`, `ivp_sel2nx8i`,
  `ivp_dseln_2x32t` (the exact tallies vary by image).
* **`mask_enable=1` geometry.** The `_t` predicated `dsel` forms are present and implement the
  partial-tile mask; the exact tile-boundary lane-kill geometry is not byte-decoded. `[MED]`
* **v5/MAVERICK.** Header-OBSERVED only; DVE image not carved — interior INFERRED. `[LOW/flagged]`

---

## 9. Adversarial self-verification — five strongest claims re-challenged

1. **"StreamTranspose is opcode `0x6b`, bound to `S4D4_TR`."** Re-challenged against
   `aws_neuron_isa_tpb_common.h:199` (`NEURON_ISA_TPB_OPCODE_STREAM_TRANSPOSE = 0x6b`) and
   `instruction_mapping.json` `struct2opcode`: the reverse query *which struct backs STREAM_TRANSPOSE*
   returns exactly `NEURON_ISA_TPB_S4D4_TR_STRUCT` (sole owner), and `S4D4_TR → 11 opcodes` includes
   `STREAM_TRANSPOSE`. **Holds.** `[HIGH/OBSERVED]`
2. **"The `S4D4_TR` struct is 64 B with `op@36`, `op_dim@37`, `mask_enable@38`, `dst@44`."**
   Re-challenged by **compiling** the header struct: `gcc` reports `sizeof==64` and `header@0 events@4
   src@12 in_dtype@32 out_dtype@33 num_active_channels@34 negated@35 op@36 op_dim@37 mask_enable@38
   reserved1@39 dst@44`, `sizeof(TENSOR4D)==20`. `ISA_STATIC_ASSERT(sizeof==64)` holds. **Byte-identical
   to tensor-reduce.md / cast-copy.md.** `[HIGH/OBSERVED — compile-verified]`
3. **"`op@36` is the transpose-mode field, pinned to `Bypass(0x00)`."** Re-challenged against
   `is_valid_stream_transpose`'s `s4d4_tr_op_bypass(i)` conjunct (`s4d4_tr.h:336`) + `ALU_OP_BYPASS =
   0x00` (`common.h:940`). For the reduce siblings the **same** `op@36` byte carries the 60-op `ALU_OP`
   reduce operator; for StreamTranspose it is validity-forced to `Bypass`. So `op@36` is opcode-keyed
   shared real estate, degenerate-constant for `0x6b`. **Holds.** `[HIGH/OBSERVED]`
4. **"The transpose is a tiled 32×32 lane-permute butterfly, no HW transpose op."** Re-challenged: the
   geometry (`has_multiple_32_channels {32,64,96,128}` + `prod(src.num_elem) % 32 == 0`) is
   header-OBSERVED; the lane-permute composition is grounded on the DVE IVP vocabulary
   (`ivp_dselnx16t`/`ivp_sel2nx8i_s4`, OBSERVED) + the B21 `DSEL` dual-output butterfly model; the
   absence of any `ivp_*transp*` in `libfiss/libisa/libcas` is OBSERVED. The per-instruction body is
   FLIX-desynced (flagged §2.5/§8), so the **5-stage `dsel` butterfly** is the standard idiom, not a
   byte trace. **Holds with the desync flag.** `[geometry HIGH/OBSERVED; composition INFERRED-HIGH]`
5. **"Datapath transpose (DVE, lane-permute, 32×32, in-SBUF) vs descriptor transpose (DGE, xbar,
   16×128, HBM)."** Re-challenged: the DVE-exclusivity grep (§2.1) isolates `S: Stream-Transpose` to the
   DVE image and the DGE `tensor_reshape_transpose` to the POOL image; the DGE header
   (`gather_xpose.h`) states *"SW-DGE backend with Q7 processors"* + 16×128 xbar tiles + 2B dtype; the
   DVE struct is `S4D4_TR`/`0x6b`/≤32-bit. Two distinct structs, engines, tile geometries, data paths.
   **Holds.** `[HIGH/OBSERVED]`

---

## 10. Cross-references

* [DGE Reshape Engine](../dge/dge-reshape.md) — the **descriptor/xbar transpose** (`DmaGatherTranspose`,
  16×128 tiles, DMA-level), the explicit contrast to this page's datapath transpose (§6).
* [Gather/Scatter Descriptors](../../dma/gather-scatter-descriptors.md) — the SB2SB
  descriptor ring the DGE `sb2sb` reshape-transpose feeds; the descriptor-level transpose's downstream.
* [ISA Batch 21 — Select / Shuffle / Compress](../../isa/ref/b21-select-shuffle.md) — the
  `ivp_sel`/`ivp_shfl`/`ivp_dsel` lane-routing crossbar this transpose composes from (the `DSEL`
  dual-output butterfly, the `2NX8/NX16/N_2X32` width forms, the `_t` predicated `vbool` mask).
* [Tensor-Reduce](tensor-reduce.md) — the **same `S4D4_TR` struct** at the *reduce* altitude (`op@36` =
  the reduce `ALU_OP`), and the fused `TransposeTensorReduce` (`0x83`/`0x84`) that prepends **this**
  32×32 transpose before the cross-axis reduce (§3).
* [Cast / Copy](cast-copy.md) — the `op=Bypass` struct-siblings (`0x46`/`0x47`) that share the decode
  template with StreamShuffle (and the common decode arm StreamTranspose splits off from).
* [TensorCumulative](tensorcumulative.md) — the scan twin on the same `S4D4_TR` struct (`0x4E`/`0x5E`).
* [CrossLaneReduce](cross-lane-reduce.md) — the GPSIMD cross-partition reduce; the `ivp_r*` fold the
  `TransposeTensorReduce` fused form feeds after this transpose.
* [Tensor-Tensor](tensor-tensor.md) — the structurally-parallel arith/bitvec opcode split
  (`NEURON_ISA_TPB_ALU_OP`, 60 ops) that the `op@36` field draws from for the non-`Bypass` siblings.

---

*Provenance: the opcode `0x6b` (`STREAM_TRANSPOSE`), the `S4D4_TR` struct + the 11-opcode binding, the
`is_valid_stream_transpose` predicate set, the `ALU_OP`/`TENSOR_SUBDIM`/`DTYPE` enums, the
`has_multiple_32_channels`/`has_transpose_src_element_count_t4d`/`is_zero_subdim`/`same_or_fewer`
helpers, and `POOLING_NUM_CHANNELS=128` are read byte-exact from the CAYMAN/SUNDA/MARIANA/MAVERICK
arch-isa headers (`aws_neuron_isa_tpb_s4d4_tr.h`, `aws_neuron_isa_tpb_common.h`) shipped in the
`aws-neuronx-gpsimd-customop-lib` package, cross-checked against `instruction_mapping.json`
(`struct2opcode`). The `S4D4_TR` `sizeof==64` + field offsets are **compile-verified** with `gcc`.
The DVE-exclusivity, the decode-type label table, the StreamTranspose decode arm bytes (entry +
`const16` + `call8` + struct-read) re-decoded identical CAYMAN/MARIANA/MARIANA_PLUS, the DVE engine
identity, the IVP lane-permute vocabulary, the absence of any HW transpose, and the per-gen presence are
read from the carved `img_<GEN>_NX_DVE_DEBUG_<SEG>` device images (`.rodata` of the `libnrtucode.a`
members, mirrored in `libnrtucode_internal.so`) with `xtensa-elf-objdump`/`readelf`/`strings`
(`XTENSA_CORE=ncore2gp`). `.text`/`.rodata` VMA==file-offset; the DRAM string VA = file-offset +
`0x80000`. The lane-permute butterfly composition (§5) is INFERRED-HIGH from the OBSERVED IVP vocabulary
+ the [B21](../../isa/ref/b21-select-shuffle.md) `DSEL` model; the FLIX-desynced body is reported
structurally, never byte-fabricated. Counts via `nm | rg -c` / objdump harvest. The `extracted/` and
`ida/` trees are gitignored (reached with absolute paths). All prose is derived from static analysis of
the shipped artifacts only.*
