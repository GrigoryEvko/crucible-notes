# Hierarchical Collective

This page is the reimplementer's reference for the **NCFW hierarchical collective
framework** — the two-level **intra-node-then-inter-node** all-reduce that NCFW runs
when a topology has a cheap inner level (cores within a die / a die-group) and an
expensive outer level (die-to-die fabric). It documents the **two-level decomposition**
(intra reduce-scatter → inter all-reduce → intra all-gather), the **level-config** (the
five per-leg algorithm selections), the **per-step compose sequencing**, and the
**per-arch ×4** schema. It is the firmware-side companion to the host lowering in
[ALL_REDUCE](../ops/all-reduce.md): a logical all-reduce is decomposed by the host NRT
`enc_hier_primitive` into a multi-leg program that the NCFW **management core**
sequences, where each leg reuses the co-resident [ring/kangaring](ring-kangaring.md) or
[mesh](mesh-collective.md) descriptors and the in-SDMA [CCE](../../dma/cce-in-transfer.md)
reduce / [d2d cross-die](../../dma/rdma-cross-die.md) transport.

**The NCFW core is a scalar Xtensa-LX control core, not a Vision-Q7 FLIX engine.** It
has no shipped disassembler, so the device step *schedule* is not directly decodable.
The hierarchical *structure* is recovered from two host binaries: the **`ncfw_log_*`
decoders** in `libncfw.so` (x86-64 pretty-printers that walk the firmware's DRAM-resident
collective structs and therefore mirror the exact field layouts the firmware reads), and
the **`enc_hier_primitive` encoder + DWARF** in `libnrt.so` (which builds the multi-leg
program before it is shipped to firmware). The device *execution substrate* (the
wait/signal/snapshot leaf primitives the case bodies call) is recovered separately by
re-decoding the carved NCFW IRAM under the **scalar-LX length rule** with the native
`xtensa-elf-objdump XTENSA_CORE=ncore2gp` (§8).

The page default is `[HIGH × OBSERVED]` (read directly from a binary/disasm/DWARF/
bytes); claims that depart from it carry an explicit `[CONFIDENCE × PROVENANCE]` tag,
with `INFERRED` (deduced from naming/structure) or `CARRIED` (established on a
committed sibling page).

> **Provenance.** Host decoders + firmware blobs from `libncfw.so`
> (`aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`,
> `opt/aws/neuron/lib/libncfw.so`, BuildID `a98f8e1ca2294582835310c3a1092e0a5e500db5`,
> ELF64 x86-64, **not stripped**, 615 640 bytes — `stat`/`readelf -n` confirm).
> `.text` and `.rodata` are VMA==file-offset (`.text` @`0x10c0`, `.rodata` @`0x65000`);
> `.data` carries a `0x1000` delta (`readelf -SW`). The host `enc_hier_primitive`
> encoder + the `enc_alg_type`/`enc_comm_type`/`enc_op_type` DWARF and the verbatim
> assertion/log strings are from `libnrt.so.2.31.24.0` (same package). The carved
> v3/CAYMAN NCFW IRAM (`v3_ncfw_iram_bin` @VMA `0x79860`, **19 392 = 0x4bc0** bytes,
> SHA-256 `d7bc8b81…e4afd`) is the device-side target.
> [identity OBSERVED HIGH]

---

## 1. TL;DR — what "hierarchical" is, and where it lives

A hierarchical all-reduce is the classic **Rabenseifner** decomposition: do a cheap
collective inside each local group, a smaller collective across the group *leaders*,
then redistribute the result back inside each group. NCFW realizes this as a
two-scope (`INTRA` / `INTER`) program where **each leg is itself a ring, kangaring, or
mesh collective** — the hierarchical algorithm does not carry its own descriptor tape;
it *composes* the co-resident ones.

The framework splits cleanly across the two binaries, and the split is the headline
finding:

| where | what it holds | how recovered |
| ----- | ------------- | ------------- |
| **host build** (`libnrt.so`, `enc_hier_primitive`) | the full two-level plan: the 5 per-leg algo choices, the intra/inter page-table builders, the compose pipeline | DWARF symbols + verbatim strings + disasm — **OBSERVED HIGH** |
| **firmware config** (`libncfw.so`, `algo_configs`) | a **single 8-byte handle** ("`__stub`") into the firmware-DRAM level table | byte-measured region = 8 bytes — **OBSERVED HIGH** |
| **firmware runtime** (`libncfw.so`, `algo_ctx`) | one **`run_state` u8** phase cursor | single-byte deref decoded — **OBSERVED HIGH** |
| **device execution** (NCFW IRAM, scalar-LX) | the per-step wait/signal/snapshot primitives each leg calls | native ncore2gp re-decode — **OBSERVED HIGH for the primitives** |

The crucial structural fact: at the `libncfw` firmware boundary the hierarchical config
collapses to **one u64** and the runtime ctx to **one byte**. The level structure — how
many levels, which algorithm per leg, the group boundaries — is **not** inline in the
firmware config; it is built host-side by `enc_hier_primitive` and reached at runtime
*through* the 8-byte handle. So this page recovers the level-config from the **host
encoder**, and the firmware-side *evidence* that the legs reuse the co-resident ring/mesh
descriptors from the **decoder structure**.

> **NOTE — naming.** `enc_alg_type` value **`1` = `ENC_ALG_HIER`** ("hierarchical
> intra+inter"), from the committed [collective-enums](../ops/collective-enums.md)
> (`enc_alg_type` DIE `<0x61eb3>`). `HIER` is one selectable algorithm among the eleven;
> it is the only one that is itself a *composition* of the others.

---

## 2. The two-level structure — `enc_hier_primitive` (the heart)

The host `enc_hier_primitive` class (in `libnrt.so`) is the complete, fully-symbolized
hierarchical builder. Its member set names the two levels and the three collective
phases explicitly. From `nm -C libnrt.so` (DWARF-named, **OBSERVED HIGH**):

| symbol | @addr | role |
| ------ | ----- | ---- |
| `enc_hier_primitive::__select_algorithms()` | `0x14c620` | choose the 5 per-leg algos |
| `enc_hier_primitive::compose_operation(enc_mr_cache*)` | `0x1a8d20` | top-level driver |
| `enc_hier_primitive::__compose_redsct(enc_mr_cache*)` | `0x1a6d60` | reduce-scatter phase |
| `enc_hier_primitive::__compose_allreduce(enc_mr_cache*)` | `0x1a34c0` | all-reduce phase |
| `enc_hier_primitive::__compose_allgather(enc_mr_cache*)` | `0x1a0f10` | all-gather phase |
| `enc_hier_primitive::__compose_pipeline_redsct(enc_mr_cache*, int)` | `0x17e9f0` | pipelined reduce-scatter |
| `enc_hier_primitive::__compose_pipeline_allreduce(enc_mr_cache*, int)` | `0x17b3f0` | pipelined all-reduce |
| `enc_hier_primitive::__compose_pipeline_allgather(enc_mr_cache*, int)` | `0x1784f0` | pipelined all-gather |
| `enc_hier_primitive::__build_redsct_pgt_intra_rdsc(…)` | `0x14e320` | **INTRA** reduce-scatter page table |
| `enc_hier_primitive::__build_redsct_pgt_inter_rdsc(…)` | `0x14e8d0` | **INTER** reduce-scatter page table |
| `enc_hier_primitive::__build_allgather_pgt_intra_allg(…)` | `0x14de30` | **INTRA** all-gather page table |
| `enc_hier_primitive::__build_allgather_pgt_inter_allg(…)` | `0x14ee00` | **INTER** all-gather page table |
| `enc_hier_primitive::__build_allreduce_pgt_inter_allr(…)` | `0x14ff80` | **INTER** all-reduce page table |
| `enc_hier_primitive::__build_allreduce_pgt_intra_rdsc(…)` | `0x1558f0` | **INTRA** all-reduce (reduce-scatter half) |
| `enc_hier_primitive::__build_allreduce_pgt_intra_allg(…)` | `0x155260` | **INTRA** all-reduce (all-gather half) |

The `_intra_` / `_inter_` suffix pairs are the two levels. The
`redsct` / `allreduce` / `allgather` triple is the three-phase Rabenseifner shape.
Every member is byte-grounded in the symbol table; the two-level decomposition is
therefore **OBSERVED HIGH** from naming, and the *order* in which they fire is
**OBSERVED HIGH** from `compose_operation` (§4).

> **`enc_comm_type` — the two scopes.** From [collective-enums](../ops/collective-enums.md)
> (DIE `<0x34a15>`): `H_COMM_INTRA_ID = 0`, `H_COMM_INTER_ID = 1`, `H_COMM_MAX_ID = 2`.
> `INTRA` = within a node/die-group; `INTER` = across nodes/dies. A hierarchical
> all-reduce runs **both** an INTRA leg and an INTER leg — it is *not* a single
> `enc_comm_type` value. [enum OBSERVED HIGH; "spans both scopes" OBSERVED from the
> `_intra_`/`_inter_` builder pair.]

---

## 3. The level-config — the five per-leg algorithm selections

The "level-config" — which sub-algorithm each leg runs — is **not** a firmware struct.
It is five `enc_alg_type` values chosen host-side by `__select_algorithms` and logged
verbatim. The exact log format string (in `libnrt.so` `.rodata` @file-offset `0x7ed4bd`,
**OBSERVED HIGH** — `strings`/byte-search) is:

```
[nec_dev %d] Hier algorithm selections - alg_intra_allg %d alg_intra_redsct %d
             alg_inter_allr %d alg_inter_allg %d alg_inter_redsct %d
```

So a hierarchical op carries **five** per-leg slots:

| slot | scope · phase | the leg it parameterizes |
| ---- | ------------- | ------------------------ |
| `alg_intra_redsct` | INTRA · reduce-scatter | collapse local data within the group |
| `alg_intra_allg`   | INTRA · all-gather     | redistribute the result within the group |
| `alg_inter_redsct` | INTER · reduce-scatter | cross-group reduce-scatter (BW-opt path) |
| `alg_inter_allr`   | INTER · all-reduce     | reduce across group leaders |
| `alg_inter_allg`   | INTER · all-gather     | cross-group all-gather (BW-opt path) |

The legal `enc_alg_type` per slot is fixed by **verbatim `libnrt` assertion strings**
(`strings libnrt.so`, **OBSERVED HIGH**):

```text
alg_intra_redsct == ENC_ALG_RING || alg_intra_redsct == ENC_ALG_KANGARING
alg_intra_allg   == ENC_ALG_RING || alg_intra_allg   == ENC_ALG_KANGARING
alg_inter_allr   == ENC_ALG_RING || alg_inter_allr   == ENC_ALG_INTER_RDH
                                  || alg_inter_allr   == ENC_ALG_SINGLE_CYCLE_RING
(alg_inter_allr  == ENC_ALG_MESH) || inter_metaring          // mesh alternative
alg_inter_allg   == ENC_ALG_RING || alg_inter_allg   == ENC_ALG_INTER_RDH
(alg_inter_allg  == ENC_ALG_MESH) || inter_metaring
```

So the **level-config legality** is:

* **INTRA** legs (reduce-scatter + all-gather) → `ENC_ALG_RING` *or* `ENC_ALG_KANGARING`.
  The local level is always a ring family — the cheap [ring/kangaring](ring-kangaring.md)
  inner level. `[OBSERVED HIGH]`
* **INTER** all-reduce → `ENC_ALG_RING`, `ENC_ALG_INTER_RDH` (recursive-doubling/halving),
  `ENC_ALG_SINGLE_CYCLE_RING`, *or* `ENC_ALG_MESH`. The outer level may be a
  ring, RDH, single-cycle ring, or the [mesh](mesh-collective.md) die-fabric. `[OBSERVED HIGH]`

`enc_alg_type` values (from [collective-enums](../ops/collective-enums.md), the host
selector draws from these): `RING=0, HIER=1, MESH=2, KANGARING=3, SINGLE_CYCLE_RING=4,
INTRA_RDH=5, SINGLE_STEP_MESH=6, INTER_RDH=7, TWO_STEP_POD_MESH=8, LATENCY_OPT_MESH=9,
BW_OPT_MESH=10, INVALID=11`. **OBSERVED HIGH.**

> **QUIRK — the inner level is never mesh, the outer never kangaring.** The assertions
> are asymmetric by design: the *intra* (local) legs are restricted to the latency-cheap
> ring/kangaring families, the *inter* (global) legs to the bandwidth/scale-friendly
> ring/RDH/mesh families. This is exactly the latency-vs-bandwidth asymmetry hierarchical
> exists to exploit (§7). `[OBSERVED HIGH — the two assertion sets are disjoint on
> KANGARING (intra-only) and MESH/RDH (inter-only).]`

---

## 4. The per-step sequencing — `compose_operation @0x1a8d20`

The host top-level driver `compose_operation` fires the legs in a fixed order. From
its disasm (`objdump -d`, **OBSERVED HIGH** — every `call` target below is a named
`enc_hier_primitive` member resolved by the symbol table):

```c
// enc_hier_primitive::compose_operation(enc_mr_cache* mr)  @0x1a8d20
NRT_STATUS compose_operation(enc_mr_cache *mr) {
    __select_algorithms();                  // 0x1a8d36: pick the 5 per-leg algos (§3)

    // --- pipelined path (large transfers, overlap legs) ---
    __compose_pipeline_allgather(mr, n);    // 0x1a8d7b
    __compose_pipeline_redsct(mr, n);       // 0x1a8df4
    __compose_pipeline_allreduce(mr, n);    // 0x1a8e9e

    // --- non-pipelined path (the canonical 3-phase Rabenseifner order) ---
    __compose_allreduce(mr);                // 0x1a8ed4
    __compose_allgather(mr);                // 0x1a8ee0
    __compose_redsct(mr);                   // 0x1a8f08
}
```

[call-site addresses `0x1a8d36`/`0x1a8d7b`/`0x1a8df4`/`0x1a8e9e`/`0x1a8ed4`/`0x1a8ee0`/
`0x1a8f08`, each a `call` to the named member — **OBSERVED HIGH**. Which path (pipeline
vs non-pipeline) is taken at runtime is gated by transfer size / topology; both paths
are present in the binary. **MED on the runtime gate.**]

The **canonical hierarchical schedule** is then visible *inside* each compose phase: each
`__compose_*` calls **both** its intra and inter page-table builder. Verified for the
reduce-scatter phase (`__compose_redsct @0x1a6d60`, **OBSERVED HIGH**):

```c
// enc_hier_primitive::__compose_redsct(enc_mr_cache*)  @0x1a6d60
__build_redsct_pgt_intra_rdsc(...);   // 0x1a74a9 -> 0x14e320  (INTRA reduce-scatter)
__build_redsct_pgt_inter_rdsc(...);   // 0x1a7b7a -> 0x14e8d0  (INTER reduce-scatter)
```

This is the decisive per-step proof of the two-level structure inside one phase: the
reduce-scatter compose builds an **intra** page table *and* an **inter** page table. The
full hierarchical all-reduce is therefore:

```
PHASE 1  INTRA reduce-scatter   (alg_intra_redsct: RING | KANGARING)   -> local shards
PHASE 2  INTER all-reduce       (alg_inter_allr:   RING | INTER_RDH |
                                  SINGLE_CYCLE_RING | MESH)             -> reduce across leaders
PHASE 3  INTRA all-gather       (alg_intra_allg:   RING | KANGARING)   -> redistribute locally
```

The BW-optimized variant additionally splits the INTER all-reduce into INTER
reduce-scatter (`alg_inter_redsct`) + INTER all-gather (`alg_inter_allg`) — the reason the
log carries five slots, not three. `[3-phase order OBSERVED HIGH from compose ordering +
the intra/inter builder pairs; the BW-opt 5-slot split OBSERVED HIGH from the log string
+ the inter_redsct/inter_allg builders @0x14e8d0/0x14ee00.]`

---

## 5. The firmware-side config — the 8-byte `__stub` handle

At the `libncfw.so` firmware boundary the whole hierarchical config is **one u64**. The
`algo_configs` struct is a union of three adjacent peer sub-regions
([ring](ring-kangaring.md) @`+0x0`, [mesh](mesh-collective.md), hierarchical last); the
hierarchical decoder reads exactly one field.

### 5.1 The decoder is a stub — `ncfw_log_algo_hierarchical_configs @0x18fcb`

```c
// ncfw_log_algo_hierarchical_configs(char* buf, int indent, char* enable, void** cfg)
//   @0x18fcb  (size 0x39a = 922 B; ×4 arch copies byte-identical)
void ncfw_log_algo_hierarchical_configs(char *buf, int indent, char *enable, void **cfg) {
    // wrapper "hierarchical": {   (key @0x6563b)  OR bare {  if enable[0]==0
    uint64_t v = *(uint64_t*)(cfg + 0x0);          // 0x191dc: mov rax,[rbp-0x50]  (cfg)
                                                   // 0x191e0: mov r12,QWORD PTR [rax]  <-- THE FIELD
    snprintf(buf, "%s: \"0x%016lX\"\n", "__stub", v); // key @0x65648 ; fmt @0x65127
    // }
}
```

[**OBSERVED HIGH.** The *only* dereference of `cfg` (`[rbp-0x50]`) in the whole function
is the single `mov r12,[rax]` @`0x191e0` — no loop, no second field, no array stride. The
key string at `0x65648` decodes to `"__stub"` and the format at `0x65127` to
`%s: "0x%016lX"\n` — the **soc_addr/pointer** format (identical to `ncfw_log_addr`'s
`@0x65127`). `"__stub"` appears **exactly 4×** in `.rodata`
(`0x65648`/`0x65cdd`/`0x66372`/`0x66a07`) and nowhere else — a deliberate developer
placeholder, one per arch.]

### 5.2 The region is exactly 8 bytes — the decisive measurement

The hierarchical region is the **last** sub-region of `algo_configs`, so its size is
bounded by the next sub-block in the parent. Measured two ways (**OBSERVED HIGH**):

```text
(a) algo_configs dispatcher lea immediates (sunda, @0x197cf / @0x197f3):
      ring @+0x0 ; mesh @+0x1280 ; hierarchical @+0x2220
(b) algo_configs TOTAL (parent, lea after the algo_configs call, sunda @0x19ac8):
      lea rdx,[rax+0x2228]  ->  total = 0x2228
   => hierarchical region = 0x2228 - 0x2220 = 0x8 = 8 bytes
```

Per-arch (cayman/mariana/mariana_plus use the wider mesh region, hier @`+0x3440`, total
@`+0x3448` — **OBSERVED HIGH** from the `lea [rax+0x3440]`/`lea [rax+0x3448]` pairs
@`0x3259a`/`0x3286f` (cayman), @`0x4b341`/`0x4b616` (mariana), @`0x640e8`/`0x643bd`
(v4+)):

| arch | hier config @ | algo_configs total | hier region size |
| ---- | ------------- | ------------------ | ---------------- |
| `0x05` SUNDA (v2) | `+0x2220` | `0x2228` | **8 B** |
| `0x0c` CAYMAN (v3) | `+0x3440` | `0x3448` | **8 B** |
| `0x14` MARIANA (v4) | `+0x3440` | `0x3448` | **8 B** |
| `0x1c` MARIANA_PLUS (v4+) | `+0x3440` | `0x3448` | **8 B** |

So `algo_configs` is exactly `[ ring 0x1280 ][ mesh 0xFA0/0x21C0 ][ hierarchical 0x8 ]`.
Eight bytes can only hold a single u64 — a pointer/handle, not an inline level array.

> **GOTCHA — the stub is not laziness, it is the struct.** The hierarchical config is a
> stub *because there is nothing inline to walk*: unlike ring (32×149 B channel array) and
> mesh (50/108×80 B event tape), the hierarchical config is **8 bytes**. The level table —
> level count, per-level algorithm, group boundaries — is reached *through* this u64 into
> firmware DRAM (the `*_ncfw_dram_bin` blob), not embedded here. `[region size OBSERVED
> HIGH; "u64 is a DRAM pointer to a level table" INFERRED MED from the soc_addr print
> format + the 8-byte size.]`

---

## 6. The firmware-side runtime — the `run_state` phase cursor

The runtime ctx decoder is **not** a stub: it reads one real field. From the `algo_ctx`
union (`ncfw_log_algo_ctx @0x18cd2`), the hierarchical ctx sits at `ctx+0x200`:

```text
algo_ctx layout (sunda dispatcher, OBSERVED HIGH):
  18e7c: call ncfw_log_algo_ring_ctx          ; ring_ctx  @ ctx+0x0   (32×16 = 512 B)
  18e85: lea rdx,[rax+0x200] ; call hier_ctx   ; hier_ctx  @ ctx+0x200
  18ea9: lea rdx,[rax+0x204] ; call mesh_ctx   ; mesh_ctx  @ ctx+0x204 (event_index u16)
```

This matches the committed [DRAM ctx-log page](ncfw-dram-ctx-log.md) byte-for-byte
(`lea [rax+0x200]` @`0x18e85`; the hierarchical ctx occupies the 4-byte slot
`[ctx+0x200 .. ctx+0x204)`). The decoder:

```c
// ncfw_log_algo_hierarchical_ctx(char* buf, int indent, char* enable, void* ctx)
//   @0x183c6  (size 0x486 = 1158 B; ×4 arch copies byte-identical)
void ncfw_log_algo_hierarchical_ctx(char *buf, int indent, char *enable, void *ctx) {
    uint8_t run_state = *(uint8_t*)(ctx + 0x0);    // 0x18651: mov rax,[rbp-0x60] (ctx)
                                                   // 0x18655: movzx eax,BYTE PTR [rax]
    snprintf(buf, "%s: %hhu", "run_state", run_state); // key @0x65621 ; fmt @0x650e3
}
```

[**OBSERVED HIGH.** `ctx` (`[rbp-0x60]`) is loaded exactly once and read as a **single
byte** at `ctx+0x0`; the other `movzx [rax]` @`0x18465` is the `enable` gate byte (from
`[rbp-0x58]`), not `ctx`. Key `"run_state"` @`0x65621` (×4 in `.rodata`:
`0x65621`/`0x65cb6`/`0x6634b`/`0x669e0`), format `%hhu` @`0x650e3`.]

The `run_state` byte is the hierarchical op's single **phase cursor** — it sequences the
PHASE 1 → 2 → 3 transitions of §4. The *per-leg* progress (how far each ring-scatter /
mesh-reduce has advanced) lives in the **co-resident** ring_ctx (per-channel scoreboard)
and mesh_ctx (`event_index`) that occupy the *same* `algo_ctx` struct — `run_state` only
sequences the level transitions, it does not track per-element progress.
`[slot placement + single-byte read OBSERVED HIGH; "phase cursor" role INFERRED MED from
the name + singularity + the co-resident per-leg ctx.]`

> **NOTE — the 3 reserved ctx bytes.** The hierarchical ctx slot is 4 bytes
> (`ctx+0x200..+0x204`) but only `run_state` @`+0x0` is decoded; `+0x201..+0x203` are
> undecoded at the host boundary. `[slot bound OBSERVED HIGH; reserve content UNKNOWN.]`

---

## 7. How the legs reuse the co-resident descriptors

The hierarchical config is 8 bytes; the legs are full ring/mesh collectives. They are
reconciled by **co-residence, not embedding** — a structural fact from the decoder:

* The three algo regions (ring, mesh, hierarchical) are **peer, adjacent, fixed-offset**
  sub-structs of *one* `algo_configs` union (§5.2). They all exist simultaneously in
  DRAM; the active algorithm is chosen by the cc_op `algo_type` nibble (§9). `[OBSERVED HIGH]`
* The hierarchical region is too small (8 B) to contain a 149-B ring channel (let alone
  32) or an 80-B mesh event, and it decodes no index field — only the u64. So it does
  **not** inline-embed ring/mesh and does **not** carry an explicit decoded index. `[OBSERVED HIGH]`
* Therefore the hierarchical legs **use the co-resident ring and mesh descriptors in
  place**: the INTRA legs drive the resident 32-channel ring config; the INTER all-reduce,
  when `alg_inter_allr == ENC_ALG_MESH`, drives the resident mesh event tape. The 8-byte
  handle is the firmware's pointer to a level-descriptor table that binds *which* resident
  sub-algorithm + *which* subset each level uses. `[reuse model INFERRED MED; "ring+mesh
  co-resident, hier is 8-byte handle not embed" OBSERVED HIGH.]`

The **execution substrate** is shared too. Every leg, whatever its algorithm, lowers on
the device to the same three leaf primitives (§8): a semaphore **WAIT** (spin-poll), a
fenced **SIGNAL** (CSR store), and an atomic CSR **snapshot**. The intra reduce is the
in-SDMA [CCE](../../dma/cce-in-transfer.md) reduce ("the reduce (ALU op) leg of the
firmware ring"); the inter transport rides the [d2d cross-die](../../dma/rdma-cross-die.md)
`io_d2d` path. `[CCE/d2d roles CARRIED from the committed DMA pages.]`

> **Why hierarchical at all.** A flat ring over *all* ranks across dies serializes every
> cross-die hop into ring order (high latency at scale); a flat mesh over all ranks
> ignores the much cheaper intra-die path. Hierarchical does a fast intra-die ring first
> (collapse local data), then a smaller inter-die reduce over only the leaders, then a
> cheap local broadcast — the standard 2-level collective for multi-node training. The
> §3 legality asymmetry (intra = ring/kangaring, inter = ring/RDH/mesh) encodes exactly
> this. `[INFERRED MED, anchored to the OBSERVED legality split.]`

---

## 8. The device execution substrate (scalar-LX re-decode)

The hierarchical leg's device body lives in the NCFW IRAM `0x3e..` cluster (idx 8–11 in
the DRAM+0xB0 dispatch table). Re-decoded under the **scalar-LX length rule** (op0 e/f =
3-byte boundary heuristic, resync at `retw.n`) with the native
`xtensa-elf-objdump XTENSA_CORE=ncore2gp` against the carved v3 IRAM (SHA-256
`d7bc8b81…e4afd`). The body *interior* stays partly hard
(op0=e/f operand bytes desync a linear sweep; no LX TIE config ships to name the e/f
leader ops — out-of-image `call` targets like `0xfffd3920` are mis-synced operand bytes,
flagged and not followed), but the **leaf primitives the body calls** decode cleanly.

The dispatch read that selects the hierarchical (or any) case body is byte-exact
(**OBSERVED HIGH**):

```asm
; v3 IRAM @0x3bf8  (raw bytes 24 b0 00 20 23 a0 58 02)
3bf8:  const16 a2,0xb0      ; a2 = byte offset of the DRAM+0xB0 handler table
3bfb:  addx4   a2,a3,a2     ; a2 = (index a3)<<2 + 0xb0
3bfe:  l32i.n  a5,a2,0      ; a5 = table[index]  (the case-label IRAM address)
```

The three leaf primitives every leg composes, decoded byte-exact from the
`0x3100..0x36f0` windowed helper bank (41 `entry`/32 `retw.n` functions; the bank the
ring/mesh/hier bodies call — **OBSERVED HIGH**):

```asm
; (a) SEMAPHORE WAIT-GE  (spin-poll)  @0x3498  [bytes c0 20 00 28 0a 27 b3 f7]
3498:  memw                ; ordering barrier before the CSR read
349b:  l32i.n a2,a10,0     ; a2 = *(sema CSR)        (a10 = CSR pointer)
349d:  bgeu   a3,a2,0x3498 ; spin while target a3 >= val a2  (release when val > target)
34a0:  retw.n

; (b) SEMAPHORE SIGNAL  (fenced CSR store)  @0x31c7  [bytes c0 20 00 49 0a]
31c7:  memw                ; barrier
       s32i.n a4,a10,0     ; *(CSR) = a4   (the increment/set; a4 = 1<<sema_shift_offset)
```

The WAIT family is the full `{wait-ne, wait-lt/le, wait-ge}` set across two register
banks (a2/a3 and a5/a4), **byte-stable 10/10/10** across v3/v4/v4+; the SIGNAL is **5**
genuine fenced CSR stores (separated from 6 frame-spill `s32i` by base-register
classification); a third primitive is the **atomic 64-bit CSR snapshot** (memw-fenced
read+read of adjacent CSR words, ×3). Native disasm confirms the wait-ge loop reconverges
at the `memw` anchor and decodes exactly as above. `[OBSERVED HIGH — primitives; the
per-step SCHEDULE that orders them (which sema, which target) stays in the e/f-dense
body interior + runtime DRAM target values — **MED**.]`

> **CORRECTION — the device "broadcast-back" is not a separate phase byte.** A prior
> structural read posited a 3-phase device cursor (reduce → reduce → broadcast). The
> host `compose_operation` (§4) shows the redistribution is the **INTRA all-gather** leg
> (`alg_intra_allg`), not a distinct device phase; the single `run_state` byte sequences
> all three legs. The device has **one** phase cursor, three composed legs — not three
> cursors. `[OBSERVED HIGH from the compose ordering + the single run_state byte.]`

---

## 9. Selector — which `algo_type` picks HIERARCHICAL

The ring/mesh/hierarchical choice is the `algo_type` nibble in the cc_op spad-control
entry, `ncfw_log_spad_ctrl_cc_op_entry @0x1840` (**OBSERVED HIGH**):

```text
byte0: algo_type     = bits[0:3]   (movzx [+0]; and 0xf)
       algo_sub_type = bits[4:6]   (shr 4; and 0x7)
       trigger_next  = bit[7]
```

The host `enc_alg_type` enum (§3) puts `ENC_ALG_HIER = 1`, and that value fits the 4-bit
nibble. But within `libncfw.so` itself the `algo_type` is logged as a **raw integer** —
there is no `algo_type → name` enum and no `"hierarchical"` *value* string; only the three
JSON **key** labels exist (`"ring"` @`0x6508d`, `"mesh"` @`0x650a1`, `"hierarchical"`
@`0x6563b`). The numeric encoding seen by the firmware is `ENC_ALG_HIER = 1` from the host
DWARF (committed [collective-enums](../ops/collective-enums.md)); the *binding* of that
value into the cc_op nibble is done by the host NRT, not by this decoder.
`[nibble layout OBSERVED HIGH; HIER=1 OBSERVED HIGH from host DWARF; the firmware does not
self-name the value — absence OBSERVED HIGH.]`

The host SELECT that drives this — `enc_hier_primitive::__select_algorithms @0x14c620`
— probes each candidate leg via its `enc_can_post_*` predicate, logs the five chosen
algos, then validates (disasm, **OBSERVED HIGH**):

```text
14c74d/14c798: call enc_can_post_intra_rdh_operation   @0xfbb00   ; INTRA candidates
14c7da/…:      call enc_can_post_mesh_operation         @0xfb4d0
14c888:        lea r9,[rip…] # 7ed4b0                              ; "Hier algorithm selections" fmt
14c8bc:        call nlog_write                                     ; LOG the 5 chosen algos (§3)
14c8dd:        call enc_validate_operation              @0xfc8f0
14c92c/…:      call enc_can_post_inter_rdh_operation    @0xfbf80   ; INTER candidates
14c9f5/…:      call enc_can_post_mesh_operation         @0xfb4d0
14cab2/…:      call enc_can_post_kangaring_operation    @0xfbe70   ; INTRA candidate
14cb1d:        call enc_can_post_single_cycle_ring      @0xfaa80   ; INTER all-reduce candidate
```

The top-level gate `enc_can_post_hierarchical_operation @0xfc130` decides whether the
whole hierarchical path is admissible for a given `(world-size, topology, dtype)`. This
is the committed selection census in [ALL_REDUCE §6](../ops/all-reduce.md). `[OBSERVED HIGH
— `nm -C` signatures + the in-function `call` census; *which* numeric algo a given config
resolves to depends on the `can_post` thresholds, not fully enumerated — **MED**.]`

---

## 10. Per-arch variants — the ×4 schema

The hierarchical schema is **schema-wide and byte-identical across the four shipped
arches** — every `ncfw_log_*` hierarchical symbol appears 4× with identical function
sizes, and the only per-arch difference is the config-region offset (which tracks the
wider mesh region on v3/v4/v4+). Arch→codename is byte-grounded in `libncfw_get_image`
(`cmp [rbp-0x4],0xNN` legs):

| arch id | blob | codename / gen | hier config @ | `hier_configs` @ | `hier_ctx` @ | `"__stub"` @ | `"run_state"` @ |
| ------- | ---- | -------------- | ------------- | ---------------- | ------------ | ------------ | --------------- |
| `0x05` | `v2_ncfw_*` | **SUNDA** / NC-v2 | `+0x2220` | `0x18fcb` | `0x183c6` | `0x65648` | `0x65621` |
| `0x0c` | `v3_ncfw_*` | **CAYMAN** / NC-v3 | `+0x3440` | `0x31d72` | `0x3116d` | `0x65cdd` | `0x65cb6` |
| `0x14` | `v4_ncfw_*` | **MARIANA** / NC-v4 | `+0x3440` | `0x4ab19` | `0x49f14` | `0x66372` | `0x6634b` |
| `0x1c` | `v4_plus_ncfw_*` | **MARIANA_PLUS** / NC-v4+ | `+0x3440` | `0x638c0` | `0x62cbb` | `0x66a07` | `0x669e0` |

[arch dispatch OBSERVED HIGH — `cmp [rbp-0x4],{0x05,0x0c,0x14,0x1c}` legs
@`0x11c1`/`0x11c7`/`0x11ad`/`0x1199` in `get_image`, each `je` loading the matching
`vN_ncfw_iram_bin` (`lea … # 6a140/79860/83260/8ccc0`). The four `hier_configs` /
`hier_ctx` copies are byte-identical in size (922 B / 1158 B); the `"__stub"` /
`"run_state"` keys each occur exactly 4× in `.rodata`, one per copy. Hier region size = 8 B
in **all four** (§5.2).]

> **CORRECTION — codename↔gen mapping (do not invert).** Arch `0x0c` → **CAYMAN = NC-v3**,
> arch `0x14` → **MARIANA = NC-v4** — proven by the `libncfw_get_image` blob `lea`s and the
> per-codename `ncfw_ctx_log` call targets (committed [mesh-collective](mesh-collective.md),
> [ring/kangaring](ring-kangaring.md)). A survey note that paired `0x0c`→mariana /
> `0x14`→cayman is **wrong against the binary**; this page uses the binary-grounded row
> above. `[OBSERVED HIGH — the `cmp` legs and blob `lea`s are unambiguous.]`

> **NOTE — v5 / MAVERICK is FILE-ABSENT.** `libncfw.so` ships **exactly four** blob pairs
> and four logger sets; there is no `v5_ncfw_*` symbol, no `maverick` decoder, and no
> fifth `cmp` leg in the arch dispatch. Any NC-v5/MAVERICK hierarchical interior is
> **not present in this binary** and is **not stated as fact** on this page. `[absence
> OBSERVED HIGH — `nm | rg -i 'v5_ncfw|maverick'` empty.]`

---

## 11. Residual uncertainty

* **The level table behind the 8-byte handle.** That the firmware config is a single
  8-byte `__stub` u64 is OBSERVED HIGH; *what* the u64 is (a firmware soc_addr pointer to a
  level-descriptor table vs a packed handle) is **INFERRED MED** — the pointer reading is
  favoured by the soc_addr print format and the 8-byte size, but the pointee lives in the
  `*_ncfw_dram_bin` blob, not in the host library.
* **The per-step device SCHEDULE.** The wait/signal/snapshot **primitives** are OBSERVED
  HIGH (§8); the *order* in which a given hierarchical leg fires them (which sema, which
  target, which leg next) is the op0=e/f-dense case-body interior + the runtime-populated
  firmware DRAM target values — neither decodable here. **MED.**
* **The runtime algo resolution.** The five per-leg slots and their legality are OBSERVED
  HIGH (§3); *which* numeric `enc_alg_type` a given `(world-size, topology, dtype)`
  resolves to depends on the `enc_can_post_*` thresholds, not fully enumerated. **MED.**
* **The 3 reserved `run_state` ctx bytes** (`ctx+0x201..+0x203`) are undecoded at the host
  boundary; content **UNKNOWN**.
* **v5 / MAVERICK** hierarchical interior is **FILE-ABSENT** (§10) — any claim about it is
  INFERRED/ABSENT, never stated as fact.

---

## See also

* [Ring + Kangaring Collective](ring-kangaring.md) — the INTRA-leg ring family the
  hierarchical intra legs reuse.
* [Mesh Collective](mesh-collective.md) — the INTER-leg die-fabric collective.
* [NCFW DRAM Context Log](ncfw-dram-ctx-log.md) — the `algo_ctx` layout
  (`hier_ctx @+0x200`) this page's runtime ctx sits in.
* [ALL_REDUCE](../ops/all-reduce.md) — the host `__select_algorithms` SELECT and the
  five-leg census.
* [Collective Enums](../ops/collective-enums.md) — `enc_alg_type` (`HIER=1`),
  `enc_comm_type` (`INTRA=0`/`INTER=1`).
* [CCE In-Transfer Reduce](../../dma/cce-in-transfer.md) — the intra-leg reduce ALU.
* [RDMA Cross-Die](../../dma/rdma-cross-die.md) — the inter-leg `io_d2d` transport.
* [LX-ISA Naming / arch-id Synthesis](lx-isa-naming-archid-synthesis.md) — the
  orchestration synthesis.
