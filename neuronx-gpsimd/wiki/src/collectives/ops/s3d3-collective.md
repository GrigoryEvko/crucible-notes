# S3D3 Collective (SB2SB, `0xBF`)

This page decodes the **S3D3 Collective** — opcode **`0xBF`**, struct
`NEURON_ISA_TPB_S3D3_COLLECTIVE_STRUCT` (64 B), header doc verbatim *"Neuron S3D3_Collective
Format — one 3d SRC Tensor, one 3d DST Tensor. Use for: SB2SB Collective using Pool/Q7 iDMA
engine."* It is the **one real HW instruction in the collective family**: the on-engine,
intra-node State-Buffer → remote-State-Buffer iDMA leg that the device POOL/SEQ engine
actually executes, and the leg the pseudo trigger-collectives lower *to*. The S-format name
decodes as **S**ource-**3D** / **D**est-**3D**: a `src_mem_pattern` and a `dst_mem_pattern`,
each a `NEURON_ISA_TPB_TENSOR3D` (16 B = `ADDR4` base + 3 signed `int16` strides + 3 unsigned
`uint16` shape dims).

> **What it is, in one line.** `0xBF` is the wire opcode that the device SB2SB kernel runs
> ([SB2SB Remote-Copy Collective Kernel](../../firmware/kernels/sb2sb-remote-copy.md)); it
> rides the cross-die RDMA path ([RDMA Cross-Die SBUF→SBUF P2P](../../dma/rdma-cross-die.md)),
> its reduce variant folds in the in-SDMA CCE reduce
> ([CCE In-Transfer Compute](../../dma/cce-in-transfer.md)), and it is the per-step primitive
> of [`ALL_REDUCE`](all-reduce.md). The compiler-level lowering symbol is
> `InstGPSIMDSB2SB` (Penguin BIR roster, `compiler/bir-inst-roster.md`).

**Provenance.** Every field, offset, enum value, opcode, symbol address and string below is
re-grounded against: the clean ISA headers
(`aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64`, compile-verified with `gcc` `sizeof`/
`offsetof`); `instruction_mapping.json`; and the host runtime `libnrt.so.2.31.24.0`
(`aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce`, ELF x86-64, not stripped, IDA-sidecar
verified) — the encoder that builds and validates the `0xBF` word. Device-side log strings
are cross-checked against `libnrtucode_internal.so`. Confidence tags: **HIGH** = compile-
verified header / direct DWARF / direct disasm / verbatim string; **MED** = strong inference;
**LOW** = plausible. Evidence: **OBSERVED** = read directly; **INFERRED**; **CARRIED** =
sibling-page fact.

---

## 1. Classification — a real HW op, neither a pseudo-op nor a mode

`S3D3_Collective` is **not** a pseudo-op and is **not** a mode of `TRIGGER_COLLECTIVE`. It is
a distinct, real hardware instruction.

| binding | value | source |
| --- | --- | --- |
| opcode | `NEURON_ISA_TPB_OPCODE_SB2SB_COLLECTIVE = 0xbf` | `aws_neuron_isa_tpb_common.h:262` |
| struct→opcode | `"NEURON_ISA_TPB_S3D3_COLLECTIVE_STRUCT": ["NEURON_ISA_TPB_OPCODE_SB2SB_COLLECTIVE"]` | `instruction_mapping.json` `struct2opcode` (cayman/mariana/maverick) |
| INST union member | `s3d3_collective` | `aws_neuron_isa_tpb_util.h:115` |
| header file | `aws_neuron_isa_tpb_s3d3_collective.h` | — |

**[HIGH / OBSERVED.]** A `gcc` probe evaluates `NEURON_ISA_TPB_OPCODE_SB2SB_COLLECTIVE ==
0xbf`, `PSEUDO_TRIGGER_ALL_REDUCE == 0xc7`, `PSEUDO_TRIGGER_COLLECTIVE == 0xc8`.

**It is below the pseudo range.** NRT defines the pseudo opcodes as those with *"upper three
bits of the opcode equal to `0b110`"* (`common.h:263` comment), i.e. `0xC0..0xDF`. `0xBF =
0b1011_1111` → upper three bits `0b101`, **below** the pseudo block. Confirmed two ways:

* `0xBF` is **not** in `is_pseudo_op()` — that predicate is an explicit opcode list
  (`aws_neuron_isa_tpb_instr_assert.h:594-626`) enumerating `PSEUDO_DMATRIGGER`,
  `PSEUDO_TRIGGER_ALL_REDUCE`, `PSEUDO_TRIGGER_COLLECTIVE`, `PSEUDO_TRIGGER_COLLECTIVE2`, …;
  `SB2SB_COLLECTIVE` does **not** appear. It is also absent from `struct2pseudo_opcode` in
  the JSON (`jq '.struct2pseudo_opcode | has("…S3D3_COLLECTIVE_STRUCT")' → false`).
* `0xBF` **is** in the POOL-engine branch of `neuron_isa_check_opcode_on_engine()`
  (`instr_assert.h:741`): the block opens
  `engine == NEURON_ISA_TPB_NEURON_ENGINE_POOL` (`:692`) and lists
  `opcode == NEURON_ISA_TPB_OPCODE_SB2SB_COLLECTIVE` among the valid POOL opcodes (alongside
  `COPY`, `CAST`, `GATHER`, `TENSOR_TENSOR_ARITH_OP`, …).

**[HIGH / OBSERVED — opcode value + the is_pseudo_op list + the POOL-engine assert.]** ⇒ S3D3
executes on the **POOL engine** — the "Pool/Q7 iDMA engine" of the doc comment — consistent
with the device-side POOL/Q7 data-plane decode (`decode_sb2sb_collective`, §4).

### The lowering relationship

```
  pseudo TriggerCollective[2]                       per-leg            real HW
  0xC7 TRIGGER_ALL_REDUCE   ┐                    SB2SB / DMA legs       0xBF S3D3_COLLECTIVE
  0xC8 TRIGGER_COLLECTIVE   ├── NRT host-side ──▶  (intra-node,    ──▶  (POOL engine decode:
  0xD9/0xDA COLLECTIVE2     ┘   lowering          q7 iDMA leg)          decode_sb2sb_collective)
```

The compiler emits the pseudo `TriggerCollective[2]` (`0xC7`/`0xC8`/`0xD9`/`0xDA`,
TOP_SP-targeted); NRT lowers them host-side into a sequence of SDMA/SB2SB legs; the device
ucode's decode set contains `SB2SB_Collective` but **no** `TriggerCollective` — the pseudo
trigger is gone before the device sees anything. S3D3 is the intra-node iDMA(q7) leg across
an LNC core group. **[HIGH structure / x-ref [`trigger-collective.md`](trigger-collective.md),
[`trigger-collective2-ext.md`](trigger-collective2-ext.md), [`all-reduce.md`](all-reduce.md).]**

> **NOTE.** Three collective opcodes — `0xC7` AllReduce, `0xC8` Collective, `0xD9`/`0xDA`
> Collective2/_ext — are pseudo. `0xBF` S3D3 is the **only** collective-family member that
> is a true HW instruction. Do not conflate "S3D3 collective" with the pseudo triggers.

---

## 2. The 64-byte `S3D3_COLLECTIVE_STRUCT` — byte-exact (compile-verified)

`ISA_STATIC_ASSERT(sizeof == 64)` (`aws_neuron_isa_tpb_s3d3_collective.h:36`). A `gcc`
`offsetof`/`sizeof` probe against the shipped header and the host `libnrt` DWARF for
`NEURON_ISA_TPB_S3D3_COLLECTIVE_STRUCT` (`byte_size 64`) give **identical** member offsets:

| off | size | field | C type | meaning |
| --- | --- | --- | --- | --- |
| 0  | 4  | `header`              | `NEURON_ISA_TPB_HEADER`       | `{opcode(1B)==0xBF, inst_word_len, debug_cmd, debug_hint}` |
| 4  | 8  | `events`              | `NEURON_ISA_TPB_EVENTS`       | per-instruction wait/update semaphore (§6) |
| 12 | 1  | `in_dtype`            | `NEURON_ISA_TPB_DTYPE`        | SOURCE element dtype (§5) |
| 13 | 1  | `out_dtype`           | `NEURON_ISA_TPB_DTYPE`        | DEST element dtype; validator enforces `in_dtype == out_dtype` (§5) |
| 14 | 2  | `reserved0[2]`        | `uint8_t[2]`                  | pad; validator: `== 0` |
| 16 | 16 | `src_mem_pattern`     | `NEURON_ISA_TPB_TENSOR3D`     | SOURCE SBUF 3-D access pattern (§3) |
| 32 | 1  | `lnc_size_fmt`        | `NEURON_ISA_TPB_LNC_SIZE_FMT` | LNC peer-NeuronCore grouping (§7); LNC4/LNC8 rejected |
| 33 | 1  | `num_active_channels` | `uint8_t`                     | # active SDMA pooling channels, 1..128 (firmware `num_chans`) (§7) |
| 34 | 14 | `reserved1[14]`       | `uint8_t[14]`                 | pad; validator: `[0..13] == 0` |
| 48 | 16 | `dst_mem_pattern`     | `NEURON_ISA_TPB_TENSOR3D`     | DEST (remote) SBUF 3-D access pattern (§3) |
| **64** | | total | | one 64 B TPB instruction word |

The probe output (header included verbatim, `gcc` compiled, run):

```text
sizeof S3D3_COLLECTIVE = 64
sizeof TENSOR3D=16 TENSOR2D=12 PSEUDO_ADDR8TENSOR2D=24 ADDR4=4 PSEUDO_ADDR8=8 HEADER=4 EVENTS=8
  off header               =  0  size 4
  off events               =  4  size 8
  off in_dtype             = 12  size 1
  off out_dtype            = 13  size 1
  off reserved0            = 14  size 2
  off src_mem_pattern      = 16  size 16
  off lnc_size_fmt         = 32  size 1
  off num_active_channels  = 33  size 1
  off reserved1            = 34  size 14
  off dst_mem_pattern      = 48  size 16
```

**[HIGH / OBSERVED — both the gcc probe AND the shipped host binary's DWARF agree on every
offset.]**

> **CORRECTION — a header-comment off-by-one.** The shipped header
> (`aws_neuron_isa_tpb_s3d3_collective.h:32`) annotates `reserved1[14]` as `(35 - 47)` and
> gives no explicit comment for byte 34. The **compiled** layout (the `gcc` probe *and* the
> host DWARF) places `num_active_channels` at byte 33 (1 byte) and therefore `reserved1[14]`
> at bytes **34..47**, so `dst_mem_pattern` lands at 48. The header `(35-47)` annotation is
> an off-by-one typo; the real `reserved1` span is **34..47**. This matches the device-side
> decode (`reserved1 @ 34`). **[HIGH / OBSERVED — gcc + DWARF both 34.]**

The component field types (all in `aws_neuron_isa_tpb_common.h`):

```c
typedef struct NEURON_ISA_TPB_HEADER {                       // common.h:411 — 4 B
    NEURON_ISA_TPB_OPCODE opcode;        // 1 B; == 0xBF
    uint8_t               inst_word_len; //
    uint8_t               debug_cmd;     //
    uint8_t               debug_hint;    //
} NEURON_ISA_TPB_HEADER;

typedef struct NEURON_ISA_TPB_EVENTS {                       // common.h:418 — 8 B
    NEURON_ISA_TPB_WAIT_MODE   wait_mode;       // 1 B
    uint8_t                    wait_idx;        // 1 B
    NEURON_ISA_TPB_UPDATE_MODE update_mode;     // 1 B
    uint8_t                    update_idx;      // 1 B
    uint32_t                   semaphore_value; // 4 B
} NEURON_ISA_TPB_EVENTS;
```

---

## 3. The `TENSOR3D` access pattern (the src/dst 3-D mem patterns)

Both `src_mem_pattern` and `dst_mem_pattern` are `NEURON_ISA_TPB_TENSOR3D`
(`aws_neuron_isa_tpb_common.h:649`):

```c
typedef struct NEURON_ISA_TPB_TENSOR3D {  // 16 B; probe: start_addr@0, step_elem@4, num_elem@10
    NEURON_ISA_TPB_ADDR4 start_addr;      // 4 B — SBUF base (union; §3.2)
    int16_t              step_elem[3];    // 6 B — 3 SIGNED strides, in ELEMENTS
    uint16_t             num_elem[3];     // 6 B — 3 UNSIGNED shape dims (element counts)
} NEURON_ISA_TPB_TENSOR3D;
```

Each operand is a **full 3-D strided SBUF region**: a base partition-offset plus, per
dimension `d ∈ {0,1,2}`, `num_elem[d]` iterations advancing by `step_elem[d]` *elements*. The
signed strides allow reverse / transposed traversal; the third dimension is the extra axis a
2-D pattern cannot express. **[HIGH / OBSERVED.]**

### 3.1 Why "S3D3" — the S-format taxonomy

In the shipped ISA the `SnDn` naming encodes (`n` Sources × 3-D, `n` Dests × 3-D). **S3D3** =
one 3-D Source + one 3-D Dest (header verbatim: *"one 3d SRC Tensor, one 3d DST Tensor"*). The
sibling `S3S3D3_TT` (`aws_neuron_isa_tpb_s3s3d3_tt.h`, also 64 B) carries `src0_mem_pattern`,
`src1_mem_pattern`, `dst_mem_pattern` — **two** 3-D Sources + one 3-D Dest (a tensor-tensor
op). So "S3D3" literally means Source-3D / Dest-3D, and the pattern type is the same
`TENSOR3D` the rest of the `s3d3_*` instruction family uses. **[HIGH / OBSERVED.]**

### 3.2 The `ADDR4` base (4 B, union)

`NEURON_ISA_TPB_ADDR4` (`common.h:499`) is a 4-byte union:

```c
typedef union NEURON_ISA_TPB_ADDR4 {
    NEURON_ISA_TPB_PARTITION_OFFSET addr_immediate;  // uint32 — immediate SBUF partition/byte offset
    NEURON_ISA_TPB_ADDR_REG4        addr_reg;         // {regnum, reserved0[2], marker}
} NEURON_ISA_TPB_ADDR4;
```

`PARTITION_OFFSET` is a `uint32` (`common.h:447`) whose bit `[25]==0` distinguishes an SBUF
address from a PSUM address (`common.h:462-466`). The `marker` byte top-bits select
IMM (`0x00`) / ADDR_REG (`0x80`) / SHAPE_REG (`0x40`) / ADDR_SHAPE_REG (`0xC0`)
(`common.h:486-491`).

### 3.3 SBUF-only residency (the "SB2SB" constraint)

The validator runs both operands through `tensor3d_valid()`:

```c
// aws_neuron_isa_tpb_s3d3_collective.h:53-54 (validator doc-comment, verbatim)
   tensor3d_valid(src_mem_pattern, in_dtype,  WriteTensor::False, AllowedInPSUM::False, AllowedInSBUF::True)
&& tensor3d_valid(dst_mem_pattern, out_dtype, WriteTensor::True,  AllowedInPSUM::False, AllowedInSBUF::True)
```

`tensor3d_valid` (`common.h:1906`) requires each `num_elem[d] >= 1` (or a shape register) AND
`tensor_start_addr_valid(start_addr, dtype, write, AllowedInPSUM::False, AllowedInSBUF::True)`.
So **both src and dst must be SBUF-resident** (`PSUM::False`) — this is the literal "SB2SB"
= **S**tate-**B**uffer to **S**tate-**B**uffer. **[HIGH / OBSERVED.]**

### 3.4 The per-instruction size cap (≤ 256 elements)

```c
// aws_neuron_isa_tpb_s3d3_collective.h:97-99 + common.h:1742-1744
fn size_check_src(t: Tensor3d) -> bool { t3d_element_count(t) <= 256 }
fn t3d_element_count(t: Tensor3d) -> u32 { t.num_elem[0] * t.num_elem[1] * t.num_elem[2] }
```

A single `0xBF` word moves at most **256 elements** of the source pattern. Larger collective
buffers are tiled across multiple S3D3 legs and across the `num_active_channels` SDMA
channels (§7). **[HIGH / OBSERVED — validator predicate + the element-count formula.]**

### 3.5 Byte-exact contrast with the 2-D `PSEUDO_ADDR8TENSOR2D`

The v2 pseudo `TriggerCollective2` (`0xD9`/`0xDA`) carries `PSEUDO_ADDR8TENSOR2D`
(`common.h:625`, 24 B) instead of `TENSOR3D` (16 B). The narrower `int16`/`uint16` of
`TENSOR3D` (vs `int32`/`uint32` in the pseudo) is exactly what lets **three** dims + a 4-byte
base fit in 16 B, so two operands plus the header/events/dtype/lnc/channels fit in **one**
64-B word — whereas the 2-D pseudo spends 24 B per operand and needs two instructions.

| aspect | S3D3 `TENSOR3D` (real HW, `0xBF`) | v2 `PSEUDO_ADDR8TENSOR2D` (`0xD9`) |
| --- | --- | --- |
| size | 16 B | 24 B |
| base addr | `ADDR4` (4 B; SBUF partition-offset OR addr-reg) | `PSEUDO_ADDR8` (8 B; imm NeuronAddr / reg / table / neff-var; DRAM-or-SBUF, runtime-resolved) |
| dimensions | **3** (`num_elem[3]`) | 2 (`num_elem[2]`) |
| stride type | `int16  step_elem[3]` (signed) | `int32  step_elem[2]` (signed) |
| shape type | `uint16 num_elem[3]` | `uint32 num_elem[2]` |
| max element count | `t3d_element_count <= 256` (static assert) | none (pseudo; runtime checks only) |
| emitter / executor | compiler→NRT lowering→`0xBF` word; **device decodes** | compiler emits pseudo; **NRT lowers host-side**, HW never sees it |
| validation | full static ISA validator | none (pseudo; runtime-only) |

> **NOTE — 2-D logical / 3-D physical.** The host expresses the *logical* collective as a 2-D
> (partition × free) sharding — see the host assertion strings `"Invalid number of
> dimensions(%d) for sb2sb allreduce expected 2d"`, `"… reduce scatter must be 2d"`, and the
> `cc_dim 0` free/partition-dimension checks (§8) — but the *physical* `0xBF` word carries
> 3-D mem patterns, the extra dim being the per-channel / per-rank tiling of that 2-D logical
> shape across `num_active_channels`. **[The 2-D-logical / 3-D-physical reading is MED /
> INFERRED from the host "2d" assertion strings vs the `TENSOR3D` struct; the `TENSOR3D` and
> `PSEUDO_ADDR8TENSOR2D` layouts themselves are HIGH / OBSERVED.]**

---

## 4. Host encoder → device-struct path

`libnrt` carries **both** (a) the byte-identical `S3D3_COLLECTIVE_STRUCT` type plus its
validator (it can construct *and* check the `0xBF` word) **and** (b) an **SB2SB encoder
family** that emits the lowered per-channel SDMA packets the collective composers drive.

### 4.1 The SB2SB encoder symbol family

`nm libnrt.so` (and the IDA `functions.json` bounds) — addresses **OBSERVED**:

| symbol | addr | size | role |
| --- | --- | --- | --- |
| `__encd_dma_common_sb2sb`    | `0x23e1b0` | 0x9fb | the common per-channel builder |
| `encd_dma_copy_sb2sb`        | `0x23ebb0` | 0x6c  | COPY-mode wrapper (reduce-arg = 0) |
| `encd_dma_reduce_copy_sb2sb` | `0x23ec20` | 0xd4  | REDUCE-mode wrapper (reduce-arg = 1) |
| `encd_mesh_memcopy_sb2sb`    | `0x239840` | —     | mesh copy leg (→ `add_dma_packet`) |
| `encd_mesh_reduce_sb2sb`     | `0x23b6a0` | —     | mesh reduce leg (→ `add_dma_packet_cce`) |
| `dbg_is_valid_sb2sb_collective` | `0x363a90` | 0x1086 | runtime full S3D3 validator (§4.5) |

The callers are the ring/mesh/hierarchical composers (`enc_hier_primitive::__compose_*`, the
mesh primitives). The source module is `encd` (the runtime's "encode-descriptor" module).
**[HIGH / OBSERVED — `nm -C` + the `functions.json` `callees`/`callers` graph.]**

### 4.2 Copy-vs-reduce dispatch (disasm, OBSERVED)

Both wrappers first verify the peer ("neighbor") is valid, then tail into the common builder
with the **reduce-mode selector** as the differentiating argument:

```c
// encd_dma_copy_sb2sb @ 0x23ebb0 (COPY) — annotated from objdump -M intel
//   cmp BYTE PTR [neigh+0x4], 0          ; the encd_neigh peer-valid byte
//   jne  <abort>                         ; abort if the peer is invalid
//   xor  edx, edx                        ; reduce_mode = 0   ← COPY
//   call __encd_dma_common_sb2sb

// encd_dma_reduce_copy_sb2sb @ 0x23ec20 (REDUCE-COPY)
//   cmp  BYTE PTR [rcx+0x4], 0           ; same peer-valid byte
//   jne  <abort>
//   lea  r13, [rbx*8+0]                  ; rbx = channel count; alloca rbx*8-byte op array
//   sub  rsp, rax                        ;   …on the stack, then fill it with the reduce op
//   <fill loop: mov QWORD PTR [rax], rdx ; ax += 0x10>
//   mov  edx, 0x1                        ; reduce_mode = 1   ← REDUCE
//   mov  r8d, 0x8                        ; default op/dtype arg = 8 (positional)
//   call __encd_dma_common_sb2sb
```

**[HIGH / OBSERVED — the `xor edx,edx` (copy) vs `mov edx,0x1` (reduce) selector, the
`[neigh+0x4]` peer-valid compare, the per-channel op-array `alloca`, and the `r8d=0x8`
default are direct in the disassembly.]**

### 4.3 The common builder — SBUF port folding + per-channel loop

```c
// __encd_dma_common_sb2sb @ 0x23e1b0 — call census from objdump
//
// 1. SBUF AXI-port folding:
//      encd_is_address_in_sbuf            @ 0x235030   (src/dst SBUF residency)
//      encd_arch_get_num_sb_ports         @ 0x255fc0
//      map_sb_ports_to_fold               @ 0x235040
//      encd_arch_get_axi_port_for_sbuf_addr @ 0x255f90
//
// 2. Per-channel loop (index rbx, stride 8) — branch on reduce_mode (test edx,edx / test ecx,ecx):
//      reduce_mode == 0  →  copy   leg : call encd_dma_copy        @ 0x23cf80
//                                        call __encd_dma_copy      @ 0x238c80
//      reduce_mode == 1  →  reduce leg : call encd_dma_reduce_copy @ 0x23e070
//                                        call __encd_dma_reduce_copy
```

`__encd_dma_copy` builds the SDMA packet ring — `get_dma_queue_info`,
`set_addr_routing_bits` (the cross-die routing bits, [RDMA Cross-Die](../../dma/rdma-cross-die.md)),
`add_dma_packet`, `alloc_sema_value` (the per-leg semaphore),
`encd_arch_get_sp_sema_i_ofst`. The reduce leg additionally calls **`add_dma_packet_cce`** —
the SDMA CCE reduce-on-transfer descriptor builder ([CCE In-Transfer](../../dma/cce-in-transfer.md)).
**[HIGH / OBSERVED — the call census in the `0x23e1b0..0x23ebab` disassembly; the
`add_dma_packet` (copy) vs `add_dma_packet_cce` (reduce) split is direct in
`encd_mesh_memcopy_sb2sb` / `encd_mesh_reduce_sb2sb`'s callee lists.]**

### 4.4 `encd_neigh_t` — the host peer selector

The SB2SB encoders take an `encd_neigh_t` (4-byte enum, DWARF + strings OBSERVED):

```c
enum encd_neigh {
    ENCD_NEIGH_LOCAL, ENCD_NEIGH_NEXT, ENCD_NEIGH_PREV, ENCD_NEIGH_GATEWAY,
    ENCD_NEIGH_PEER_RMTV, ENCD_NEIGH_PEER_RMTV2, ENCD_NEIGH_PEER_LOCAL,
    ENCD_NEIGH_NEXT_PEER_RMTV, ENCD_NEIGH_INVALID, ENCD_NEIGH_NUM
};
```

This is the topology selector naming **which** peer NeuronCore the SB2SB leg targets:
`LOCAL` = self-copy (the LNC1 self-test case, §7); `NEXT`/`PREV` = the ring neighbor; `PEER_*`
= the LNC2-partner / cross-die peer. It is the host analogue of the device-side `lnc_size_fmt`
peer grouping. The encoder resolves `encd_neigh_t` → a concrete src/dst SoC address via
`get_neighbor_metaring` (`0x235e40`) / `get_port_to_neighbor_for_metaring` (`0x2329e0`), and
the `routing_id` via `set_addr_routing_bits` (`0x231340`). **[HIGH / OBSERVED — the
enumerators + the resolver symbols.]**

### 4.5 The host runtime validator (`dbg_is_valid_sb2sb_collective`)

`dbg_is_valid_sb2sb_collective` (`0x363a90`) executes the **full S3D3 validator** at runtime —
the same body as the header spec (§2/§7), compiled into the host runtime. Its sub-call graph
(`functions.json` callees) is `dbg_has_valid_neuron_header`, `dbg_has_valid_neuron_events`,
`dbg_tensor3d_valid` (called **twice** — src then dst), `is_valid_enum`, plus the inline
S3D3 checks visible in the disassembly:

```text
0x363b0e  call dbg_has_valid_neuron_header
0x363b6d  call dbg_has_valid_neuron_events
0x363e2f  cmp  r11b, 0x2          ; lnc_size_fmt == LNC4(=2)  → reject
0x363ed8  sub  r9d,  0x2          ; (lnc_size_fmt - 2)
0x363edc  cmp  r9b,  0x1          ;   <= 1  ⇒ fmt ∈ {2,3} = LNC4/LNC8 → reject
0x363ec5  call is_valid_enum      ; is_valid_enum(LncSizeFmt, fmt)
0x364014  cmp  QWORD [...], 0xffff ; the 0xffff element-count guard
0x364138  call dbg_tensor3d_valid ; src_mem_pattern
0x3641c6  call dbg_tensor3d_valid ; dst_mem_pattern
```

**[HIGH / OBSERVED — the validator sub-call graph + the LNC-reject compares byte-pinned in
the disassembly.]**

### 4.6 The two producers of an S3D3 word

* **(i) The compiler** (`InstGPSIMDSB2SB`, Penguin BIR lowering) emits the `0xBF` S3D3 word
  into the NEFF instruction stream as the structured-copy / collective leg; NRT's lowering of
  a pseudo `TriggerCollective[2]` resolves the operands; the device POOL/SEQ engine decodes
  that `0xBF` word (`decode_sb2sb_collective` → `rdma_desc_gen` → `rdma_desc_start`, see the
  [SB2SB firmware kernel](../../firmware/kernels/sb2sb-remote-copy.md)).
* **(ii) The host `encd_*_sb2sb` family** (§4.1) is the algorithm-composition path: when the
  runtime composes a ring/mesh/hierarchical collective it emits the per-channel SDMA packets
  for each SB2SB leg directly (driving the SDMA descriptors the `0xBF` word would otherwise
  trigger).

Both paths converge on the **same** SDMA BD-ring and the same cross-die routing the device
leg programs. **[Both producers HIGH / OBSERVED (symbol families + device decode); which path
runs for a given collective is compose-path-dependent and not traced end-to-end here — MED.]**

---

## 5. The `in_dtype` → `out_dtype` cast — a constrained-identity slot

`in_dtype` (off 12) and `out_dtype` (off 13) are each a 1-byte `NEURON_ISA_TPB_DTYPE`
(`common.h:722`):

```
INVALID=0  UINT64=1  INT8=2   UINT8=3   INT16=4  UINT16=5  BFLOAT16=6  FP16=7
INT32=8    UINT32=9  FP32=10  FP32R=11  INT64=12 FP8_E3=13 FP8_E4=14   FP8_E5=15
```

This is byte-identical to the SDMA dtype / `nrt_dtype` ([scalartype/DTYPE
Rosetta](../../abi/scalartype-dtype-rosetta.md)) — the dtype passes through verbatim into the
SDMA descriptor. **[HIGH / OBSERVED.]**

**The cast is constrained to identity on every shipping gen.** The validator carries

```c
// aws_neuron_isa_tpb_s3d3_collective.h:101-103
fn dtype_equality_check(d_in: Dtype, d_out: Dtype) -> bool { (d_in == d_out) }
```

invoked from `is_valid_sb2sb_collective`. The three gens that support SB2SB ship a
**byte-identical** `s3d3_collective.h` (only the `ISA header for NC-vN` comment differs):

| gen | NeuronCore | ships `s3d3_collective.h`? | `struct2opcode` binding? |
| --- | --- | --- | --- |
| sunda    | V2 | **no** | **no** |
| cayman   | **V3** | yes | yes |
| mariana  | V4 | yes | yes |
| maverick | V5 | yes | yes |

`has_valid_nc_sb2sb_collective(nc) = (nc >= V3)` (`…:63-65`), so V2/sunda ships no S3D3 at all.
On every gen that *does*, the on-chip validator **requires `in_dtype == out_dtype`** — SB2SB
is a structured COPY/REDUCE with **no dtype conversion**. The device confirms: the firmware
logs a **single** dtype for the move (`P%i: SB2SB_Collective : … dtype=%d, …`, one dtype, not
a src/dst pair). **[HIGH / OBSERVED — the equality predicate identical across cayman/mariana/
maverick + sunda's absence + the single-dtype device log string.]** ⇒ the dual
`in_dtype`/`out_dtype` field is a forward-looking *structural* cast slot, **encoded but
disabled**.

---

## 6. The `events` / semaphore field

`events` (off 4, 8 B) = `NEURON_ISA_TPB_EVENTS {wait_mode(1B), wait_idx(1B), update_mode(1B),
update_idx(1B), semaphore_value(u32)}` (`common.h:418`). This is the per-instruction hardware
semaphore the `0xBF` instruction **waits** on before issue (`wait_mode`/`wait_idx` vs
`semaphore_value`) and **updates** on completion (`update_mode`/`update_idx`).

Combined with the device-side LOCAL + REMOTE semaphore descriptors that `rdma_desc_gen`
programs, this is the on-chip end of the NCFW counted barrier: an SB2SB leg waits its inbound
semaphore ≥ target and increments the peer's semaphore on completion, chaining the ring/mesh
steps. The host allocates the per-leg semaphore in the encoder (`alloc_sema_value`, §4.3) and
programs the SP sema offset (`encd_arch_get_sp_sema_i_ofst`). The cross-die two-semaphore
completion and the `EVT_SEM` semaphore-block base are decoded on the
[RDMA Cross-Die](../../dma/rdma-cross-die.md) page. **[events-field layout HIGH / OBSERVED from
the header; the barrier-chaining mapping is CARRIED from the firmware/RDMA pages — MED.]**

---

## 7. `lnc_size_fmt` peer grouping + `num_active_channels`

### 7.1 `NEURON_ISA_TPB_LNC_SIZE_FMT` (off 32)

`common.h:815`, header comment *"LncSize setting for sb2sb collective using q7 iDMA"*:

```c
typedef enum NEURON_ISA_TPB_LNC_SIZE_FMT {
    NEURON_ISA_TPB_LNC_SIZE_FMT_LNC1 = 0, // NC copies to itself. Used for self-test only.
    NEURON_ISA_TPB_LNC_SIZE_FMT_LNC2 = 1, // NC's grouped: NC0-NC1, NC2-NC3, NC4-NC5, NC6-NC7
    NEURON_ISA_TPB_LNC_SIZE_FMT_LNC4 = 2, // NC's grouped: NC0-NC3, NC4-NC7
    NEURON_ISA_TPB_LNC_SIZE_FMT_LNC8 = 3, // NC's grouped: NC0-7
} NEURON_ISA_TPB_LNC_SIZE_FMT;
```

"LNC" = **L**ogical **N**euron**C**ore group — how many physical NeuronCores fuse into one
logical core for the collective; the field selects the **peer set** an SB2SB leg moves
between. The validator:

```c
// aws_neuron_isa_tpb_s3d3_collective.h:67-71
fn has_valid_LncSizefmt(fmt: LncSizeFmt) -> bool {
       is_valid_enum(EnumList::LncSizeFmt, fmt)
    && (fmt != LncSizeFmt::LNC4)
    && (fmt != LncSizeFmt::LNC8)
}
```

So **only LNC1 (self-test) and LNC2 (NC-pair) are legal**; LNC4 and LNC8 are rejected — both
in the header AND in the host-runtime disasm (`cmp r11b,0x2`; `(fmt-2) <= 1`, §4.5). This
makes SB2SB an **intra-node, ≤ 2-NC** operation. The host string makes it explicit:
**`"We do not support a SB2SB for multi-node workloads"`**. **[HIGH / OBSERVED — enum + reject
predicate in header AND host disasm + the string.]**

> **GOTCHA — why LNC4/LNC8 are rejected.** SB2SB's transport is the Pool/Q7 iDMA, whose
> pre-sync + cross-die window remapper is a **point-to-point** leg between a NeuronCore and
> **one** peer (an LNC2 group is exactly such a pair; LNC1 is the degenerate self-loop). A
> >2-core fan-out is realized as a **sequence of LNC2 SB2SB legs** by the ring/mesh composer,
> not one LNC4/LNC8 instruction. **[LNC2-pair reading HIGH; the "larger groups = multi-leg,
> not one instr" rationale MED / INFERRED.]**

### 7.2 `num_active_channels` (off 33, `uint8`)

```c
// aws_neuron_isa_tpb_s3d3_collective.h:92-95 + common.h:35
fn check_active_ports(n: u8) -> bool { (n != 0) && (n <= POOLING_NUM_CHANNELS) }
static const uint32_t NEURON_ISA_TPB_POOLING_NUM_CHANNELS = 128U;
```

So **1..128** SDMA "pooling" channels run the move in parallel. This is the firmware's
`num_chans` (device log `P%i: SB2SB_Collective: num_chans=%0d, tpb_idx=%u`). The host runtime
iterates exactly this many channels in `__encd_dma_common_sb2sb` (the per-channel loop, §4.3),
emitting one SDMA packet per active channel; the firmware's `dma_mask` / `n_active_dmas` is
the bitmap of those channels (device log `dma_mask=0x%04x, n_active_dmas=%d`). **[HIGH /
OBSERVED — validator const + host per-channel loop + device log.]**

---

## 8. Reduce vs Copy classification — S3D3 does both

S3D3/SB2SB is **not** copy-only. The host SB2SB encoder family has two modes:

* **COPY (structured move)** — `encd_dma_copy_sb2sb` / `encd_mesh_memcopy_sb2sb` →
  `__encd_dma_copy` → **`add_dma_packet`**. Used for the **all-gather** legs (host strings
  `"Mesh allgather Sb2Sb …"`, `"Invalid number of dimensions(%d) for sb2sb allgather"`). No
  element-wise compute — just the 3-D strided SBUF→remote-SBUF copy with dtype pass-through.
* **REDUCE-COPY (recv-and-reduce)** — `encd_dma_reduce_copy_sb2sb` / `encd_mesh_reduce_sb2sb`
  → `encd_dma_reduce_copy` → **`add_dma_packet_cce`** (the SDMA CCE reduce-on-transfer
  descriptor, [CCE In-Transfer](../../dma/cce-in-transfer.md)). Used for the
  **reduce-scatter / all-reduce** legs (host strings `"Mesh reduce scatter Sb2Sb …"`, `"…
  sb2sb reduce scatter must be 2d"`, `"… sb2sb allreduce expected 2d"`). The reduce op rides
  the SDMA CCE op `SDMA_CCETYPE {ADD=0, FMA=1, MAX=2, MIN=3, EXT=4, GCE=5}` (IDA `enums.json`,
  OBSERVED) — the reduce wrapper's `r8d=0x8` default and the per-channel op array (§4.2) carry
  it.

> **QUIRK — the `0xBF` word has no reduce/`op` field.** Unlike the pseudo `TriggerCollective`
> (`0xC8`, `op @ 12`) and `Collective2` (`0xD9`, `op @ 12`), the S3D3 struct (§2) carries **no
> `op`**. The reduce, when present, lives in the **SDMA CCE descriptor** the SB2SB leg
> programs — not in the `0xBF` word. So:
> * as a **HW instruction**, S3D3 is a structured 3-D SBUF↔SBUF **copy** (dtype-identity);
> * as a **collective leg**, the runtime drives it in copy or reduce-copy mode by choosing the
>   COPY vs CCE-reduce SDMA descriptor for that leg.
>
> **[Both encoder modes + the `add_dma_packet` vs `add_dma_packet_cce` split HIGH / OBSERVED;
> that the `0xBF` word carries no `op` field HIGH / OBSERVED (struct §2); the reduce-in-CCE
> binding HIGH / CARRIED from the CCE page.]**

### Additional host gating (OBSERVED strings)

| string | meaning |
| --- | --- |
| `We do not support SB2SB for op type %d` | only a subset of op types use SB2SB |
| `NEFF_FEATURE_BIT_PARTIAL_CC_SB2SB_SUPPORT (1ULL << 22)` | NEFF feature flag gating partial-collective SB2SB |
| `GetSequenceBoundSb2sbCollective` | a sequence-bound helper for the leg |
| `Mesh allgather Sb2Sb does not support step element to be larger than 1` | unit-stride constraint on the mesh all-gather leg |
| `Mesh reduce scatter Sb2Sb does not support step element to be larger than 1` | unit-stride constraint on the mesh reduce-scatter leg |
| `%d rank cc_dim 0 sb2sb expectes input is distributed to ranks in free dimension …` | the `cc_dim` free/partition 2-D logical-shape check |

**[HIGH / OBSERVED — all strings present in `libnrt.so`.]**

---

## 9. S3D3 (`0xBF`) vs the `TRIGGER_COLLECTIVE2` family — diff

| aspect | S3D3 `0xBF` (this page) | `TriggerCollective2` `0xD9`/`0xDA` |
| --- | --- | --- |
| class | **real HW op** (opcode `< 0xC0`) | pseudo op (`0xD9`/`0xDA`, `0b110`) |
| executes on HW? | **yes** (POOL engine) | no (NRT lowers host-side) |
| encoding | 1× 64 B word | 2× 64 B (`0xD9` + `0xDA` back-to-back) |
| operand pattern | `TENSOR3D` (16 B): `ADDR4` + 3×`int16` stride + 3×`uint16` (3-D, SBUF-only) | `PSEUDO_ADDR8TENSOR2D` (24 B): `ADDR8` + 2×`int32` stride + 2×`uint32` (2-D; DRAM-or-SBUF) |
| src/dst | `src_mem_pattern @16`, `dst_mem_pattern @48` | src0/src1 on `0xD9`; dst on `0xDA` |
| element cap | `<= 256` (static `t3d` cap) | none (pseudo; runtime only) |
| dtype | `in_dtype`/`out_dtype` (`==` enforced) | single dtype `@13` |
| reduce op | **none in the word** (reduce lives in the SDMA CCE leg) | `op` (ALU_OP) `@12` in the word |
| peer/group selector | `lnc_size_fmt` (LNC1/LNC2; LNC4/LNC8 rejected) | group_id / channel_id / stream_id |
| parallelism | `num_active_channels` (1..128) | cc_buf / priority / chaining |
| validator | full static ISA validator | none (pseudo; runtime validates) |
| role | the on-chip **intra-node iDMA leg** the others lower **to** | the compiler-level collective trigger that **lowers to** SB2SB/DMA |

**[All rows HIGH / OBSERVED from §2-8 + the sibling
[`trigger-collective2-ext.md`](trigger-collective2-ext.md).]**

---

## 10. Reconciliation with sibling pages

| this page (host S3D3) | sibling | status |
| --- | --- | --- |
| `0xBF` `S3D3_COLLECTIVE_STRUCT` 64 B; in/out dtype; src/dst `TENSOR3D`; `lnc_size_fmt`; `num_active_channels` | [SB2SB firmware kernel](../../firmware/kernels/sb2sb-remote-copy.md) reads this same 64 B struct (`reserved1 @ 34`) | **confirmed** + the host build/validate side |
| `reserved1 @ offset 34` (header `(35-47)` comment is off-by-one) | firmware page also `34` | **confirmed** — gcc probe + host DWARF |
| `TENSOR3D` 16 B (`ADDR4` + 3×`i16` + 3×`u16`) | firmware page same | **confirmed** — compile-exact |
| `dtype_equality (in == out)`; device logs a single `dtype=%d` | firmware single-dtype log | **confirmed** — no cast |
| LNC4/LNC8 rejected | host + device validators | **confirmed** |
| `num_active_channels <= 128` | firmware `num_chans` | **confirmed** |
| copy vs reduce-copy encoder split (`add_dma_packet` / `add_dma_packet_cce`) | [CCE In-Transfer](../../dma/cce-in-transfer.md) (the CCE reduce); [ALL_REDUCE](all-reduce.md) (compose→legs) | **confirmed** — the host encoder family + the copy/reduce split |
| `0xBF` = real HW, not pseudo; POOL engine | [`trigger-collective.md`](trigger-collective.md) / [`trigger-collective2-ext.md`](trigger-collective2-ext.md) | **confirmed** — POOL-engine assert |
| events / semaphore field | [RDMA Cross-Die](../../dma/rdma-cross-die.md) (LOCAL+REMOTE sema, two-sema completion) | **confirmed** |

---

## 11. Confidence ledger

**HIGH / OBSERVED** — compile-verified header / direct DWARF / direct disasm / verbatim string:

* The `0xBF` `S3D3_COLLECTIVE_STRUCT` 64 B layout, every field offset (gcc probe `sizeof==64`
  AND the host `libnrt` DWARF give identical offsets; `reserved1 @ 34`, correcting the header
  comment's `(35-47)` typo).
* `TENSOR3D` (16 B) = `ADDR4(4)` + `int16 step[3]` + `uint16 num[3]`; the 256-element cap;
  SBUF-only (`AllowedInSBUF::True`, `AllowedInPSUM::False`).
* The byte-exact contrast with `PSEUDO_ADDR8TENSOR2D` (24 B, 2-D, `int32`/`uint32`).
* `0xBF` is a real HW op (`< 0xC0`; not in `is_pseudo_op`; in the POOL-engine assert); the
  pseudo `TriggerCollective[2]` lower **to** it.
* `in_dtype`/`out_dtype` + `dtype_equality_check(in == out)` present & identical in
  cayman/mariana/maverick; sunda (V2) ships no S3D3 ⇒ constrained-identity cast slot, no real
  conversion; device logs a single dtype.
* `LNC_SIZE_FMT` enum (LNC1/LNC2/LNC4/LNC8) + the validator rejecting LNC4 & LNC8 (header AND
  host disasm `cmp r11b,0x2` / `(fmt-2) <= 1`); the string `"We do not support a SB2SB for
  multi-node workloads"`.
* `num_active_channels` 1..128 (`POOLING_NUM_CHANNELS = 128`).
* The host SB2SB encoder family `__encd_dma_common_sb2sb` → `{encd_dma_copy_sb2sb |
  encd_dma_reduce_copy_sb2sb}` + `encd_mesh_{memcopy,reduce}_sb2sb`; the per-channel loop; the
  copy(`add_dma_packet`) vs reduce(`add_dma_packet_cce`) split; `encd_neigh_t` peer selector;
  the host validator `dbg_is_valid_sb2sb_collective`.
* The SB2SB host-string set (2-D logical-shape asserts, op-type gating,
  `NEFF_FEATURE_BIT_PARTIAL_CC_SB2SB_SUPPORT`, `GetSequenceBoundSb2sbCollective`).

**MED** — strong inference: the "2-D logical / 3-D physical" reading (host asserts 2-D
collective shapes, the `0xBF` word carries `TENSOR3D` — the 3rd dim = per-channel tiling); the
LNC4/LNC8-rejected rationale (point-to-point iDMA leg, larger groups = a sequence of LNC2
legs); which producer (compiler-emitted `0xBF` word vs host `encd_*_sb2sb` SDMA packets) runs
for a given collective (compose-path-dependent, not traced end-to-end); the events→NCFW-
barrier mapping; the reduce-wrapper's `r8d=0x8` default as a positional op default.

**LOW / unrecovered:** the exact per-channel descriptor bitfield the encd path writes at the
CCE user offset; the concrete SDMA tail-pointer CSR for the SB2SB leg (the POOL/Q7 iDMA
doorbell is the device-side `TDRTP`/`RDRTP`, not byte-pinned here — see the
[firmware kernel](../../firmware/kernels/sb2sb-remote-copy.md)).
