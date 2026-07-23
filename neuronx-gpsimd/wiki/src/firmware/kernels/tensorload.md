# TensorLoad — opcode `0xaa`

`TensorLoad` is the **iTPB sequencer's 2-D scalar memory→register gather**: it reads up to **32**
scalar elements from the full Neuron Address space — walking a 2-D `(num_elem, step_elem)` stride
pattern out of a single base address — and deposits each element, **width-normalized**, into a named
**sequencer general-purpose register**. It is the *load* half of the `mem_2d` format pair; its mirror is
[TensorStore (`0xab`)](./tensorstore.md), which runs the same struct in the opposite direction
(registers/immediates → memory). Its enum byte is `NEURON_ISA_TPB_OPCODE_TENSOR_LOAD = 0xaa`, it is
present and **maintained** (`// Y`) in **all four** byte-grounded GPSIMD ISA generations
(SUNDA / CAYMAN / MARIANA / MAVERICK), and it is **not** a Q7-POOL software kernel and **not** a DVE
compute op — it is decoded directly by the sequencer control front-end, in the same `0xa5..0xab`
control block as `WRITE` / `NOTIFY` / `MOVE` / `ALU_OP` / `COMPARE_BRANCH` / `TensorStore`.

> **CORRECTION — what `TensorLoad` is *not*.** The task framing ("the POOL-side tensor data-movement
> LOAD primitive") is *refined* by the byte evidence: `TensorLoad (0xaa)` is **not** a bulk tile/tensor
> DMA mover, and it is **not** a POOL kernel. Its destination is the sequencer's *scalar register file*,
> not the State-Buffer / PSUM / vector regfile / a tile. The spec text (`aws_neuron_isa_tpb_mem_2d.h:55`)
> says it plainly: *"load up to 32 values from Neuron memory into specified sequencer registers."* The
> bulk tensor movers are `DMA_MEMCPY (0xb8)` and `DMA_INDIRECT (0xbb)` — and *those* **are** POOL
> kernels (they appear in the SUNDA POOL `kernel_info_table`); `0xaa` does not. `TensorLoad` is the
> narrow, ≤32-scalar memory-to-GPR gather the sequencer uses to pull addresses, loop bounds, and scalar
> operands into its own register file. `[HIGH/OBSERVED]`

It is the exact structural twin of `TensorStore`: both opcodes are *bound to one struct*,
`NEURON_ISA_TPB_MEM_2D_STRUCT` (the `mem_2d.rs` format, 64 bytes), and share *one* spec block. The only
asymmetry is the `src_datasrc` axis: `TensorStore` may source from registers **or** packed immediates,
whereas `TensorLoad` forces `src_datasrc == Register` (you cannot "load into an immediate"). Everything
below — opcode, struct, addressing, dtype-extend, dispatch, per-gen presence — `TensorStore` inherits
verbatim with the direction flipped; that page documents only the added immediate-source branch.

Confidence convention on this page: `[HIGH/OBSERVED]` = read directly from byte / header / compile-verify
/ `rg`-on-binary; `[MED/INFERRED]` = reasoned over an OBSERVED fact; `[…/CARRIED]` = re-used from a
sibling report at its stated confidence without re-reading the artifact this pass. Every count is
re-grounded to `rg -c` / `nm` on the shipped binary, never the decompile.

> **NOTE — provenance.** Primary facts derive from the shipped customop-lib package
> `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64`: the in-package arch-isa C interface headers
> `…/c10/include/neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/` (the authoritative opcode /
> operand-struct / enum / validity contract — `aws_neuron_isa_tpb_common.h`,
> `aws_neuron_isa_tpb_mem_2d.h`, `aws_neuron_isa_tpb_pseudo_mem_2d.h`), the per-gen
> `instruction_mapping.json` (`struct2opcode` binding), a `gcc` struct compile-verify against those
> headers, and the shipped host-side ucode library `…/c10/lib/libnrtucode_internal.so` (10,276,288
> bytes; not stripped; the per-arch × per-engine device firmware blobs with their self-name DEBUG tags
> are baked into its `.data`). `v2..v4` (SUNDA/CAYMAN/MARIANA) are byte-grounded; `v5` MAVERICK is
> header-OBSERVED (the ISA contract is byte-identical, but the MAVERICK device-blob interior is not
> re-disassembled this pass — interiors flagged INFERRED where they appear).

---

## 1. Opcode and ISA cluster `[HIGH/OBSERVED]`

`TensorLoad` lives in the **control / sequencer** opcode block `0xa5..0xab`. Reading the opcode enum
`NEURON_ISA_TPB_OPCODE` (`aws_neuron_isa_tpb_common.h`), this whole block is byte-identical across gens:

| byte   | mnemonic                                  | role                              | maint |
| ------ | ----------------------------------------- | --------------------------------- | ----- |
| `0xa5` | `NEURON_ISA_TPB_OPCODE_WRITE`             | scalar register write             | `// Y` |
| `0xa6` | `NEURON_ISA_TPB_OPCODE_NOTIFY`            | semaphore notify                  | `// Y` |
| `0xa7` | `NEURON_ISA_TPB_OPCODE_MOVE`              | full-register move                | `// Y` |
| `0xa8` | `NEURON_ISA_TPB_OPCODE_ALU_OP`            | scalar ALU                        | `// Y` |
| `0xa9` | `NEURON_ISA_TPB_OPCODE_COMPARE_BRANCH`    | compare + branch                  | `// Y` |
| **`0xaa`** | **`NEURON_ISA_TPB_OPCODE_TENSOR_LOAD`** | **memory → sequencer registers (this page)** | **`// Y`** |
| `0xab` | `NEURON_ISA_TPB_OPCODE_TENSOR_STORE`      | sequencer registers/imm → memory ([mirror](./tensorstore.md)) | `// Y` |

Per-gen enum line refs, byte-exact (`rg -n 'OPCODE_TENSOR_LOAD ' aws_neuron_isa_tpb_common.h`):

| gen      | NC-ver | `TENSOR_LOAD 0xaa` | `TENSOR_STORE 0xab` | `PSEUDO_TENSOR_LOAD 0xce` |
| -------- | ------ | ------------------ | ------------------- | ------------------------- |
| sunda    | NC-v2  | `common.h:253`     | `common.h:254`      | `common.h:275`            |
| cayman   | NC-v3  | `common.h:250`     | `common.h:251`      | `common.h:276`            |
| mariana  | NC-v4  | `common.h:255`     | `common.h:256`      | `common.h:282`            |
| maverick | NC-v5  | `common.h:258`     | `common.h:259`      | `common.h:288`            |

The trailing `// Y` column is defined at `common.h:154` ("Tested/Maintained/Not deprecated?"). Deprecated
opcodes carry `// n` (e.g. `WEIGHT_MASK 0x04` is `n, tonga stuff, deprecated`). `TensorLoad` is `// Y` in
every gen — maintained and active SUNDA → MAVERICK with no deprecation. `[HIGH/OBSERVED]`

> **NOTE — disambiguation, do not confuse with the *other* loads.** Several distinct load/store opcodes
> exist; `TensorLoad (0xaa)` is the `mem_2d` sequencer scalar gather and **none of these**:
>
> - `REG_LOAD` → struct `NEURON_ISA_TPB_S4_RL_STRUCT` (a different opcode/struct);
> - `REG_STORE` → struct `NEURON_ISA_TPB_D4_MR_STRUCT` (memset/RNG);
> - `POOL_BUFFER_LOAD` → struct `NEURON_ISA_TPB_S4_PB_STRUCT`;
> - the **bulk** movers `DMA_MEMCPY (0xb8)` / `DMA_INDIRECT (0xbb)`.
>
> The struct→opcode binding (`instruction_mapping.json`) is the discriminator: `MEM_2D_STRUCT` maps to
> exactly `{TENSOR_LOAD, TENSOR_STORE}` and nothing else. `[HIGH/OBSERVED — struct2opcode + enum.]`

### The pseudo pair `0xce` / `0xcd`

The compiler may emit `PSEUDO_TENSOR_LOAD (0xce)` rather than `0xaa` directly. The pseudo opcodes are a
distinct band whose upper three bits are `0b110` (`common.h` FIXME note at ~`:262`: *"Pseudo instructions
are generated by compiler and translated into non-pseudo HW instructions by NRT"*). The pseudo struct,
`NEURON_ISA_TPB_PSEUDO_MEM_2D_STRUCT`, is also 64 bytes, but **it does not carry a resolved address** — it
carries a symbolic `var_id` + `var_offset`. NRT resolves the variable to a concrete 64-bit Neuron address
and lowers `0xce → 0xaa` (the real `MEM_2D` instruction) at bind time. See §6 for the lowering. `struct2opcode`
confirms `PSEUDO_MEM_2D_STRUCT → {PSEUDO_TENSOR_LOAD, PSEUDO_TENSOR_STORE}` in all four gens. `[HIGH/OBSERVED]`

> **CLUSTER placement matters for surface classification.** Because `0xaa` sits in the `0xa5..0xab`
> sequencer control block, it is an **iTPB sequencer control-plane** instruction, *not* a DVE / PE / ACT /
> POOL compute op. That is what makes it a hardware-native sequencer instruction rather than a software
> kernel — see §5. `[HIGH/OBSERVED]`

---

## 2. What "LOAD" means here — source and destination memory spaces `[HIGH/OBSERVED]`

`TensorLoad` moves data **memory → sequencer scalar registers**. Not DRAM→SBUF, not SBUF→PSUM, not
memory→vector-regfile, not a tile copy. Concretely:

**SRC = the full 64-bit Neuron Address space.** `start_addr` is a `NEURON_ISA_TPB_ADDR8` whose immediate
form is `NEURON_ISA_TPB_NEURON_ADDR = uint64_t` (`common.h:443`). Any valid Neuron address is reachable:
State-Buffer (SBUF), SBUF scratch, PSUM, the device-side DataRAM / DGE memory, per-core Q7 DRAM, and
general HBM reached through the dynamic **16 MB memory windows**. The spec constrains the *whole 2-D
tensor to lie in **one** aligned 16 MB region* (`mem_2d.h:73`) — precisely the hardware MEM_WINDOW TLB
constraint: the sequencer uses *one* memory window per `mem_2d` access, so the gather cannot straddle a
16 MB boundary. `[HIGH/OBSERVED — mem_2d.h:52,73; common.h:443.]`

The `start_addr` field (`ADDR8` union, `common.h:553`) is itself a small addressing mode, set by the high
marker byte (mask `0xFC`, `common.h:517`):

| marker (high byte)               | meaning                                       | `ADDR8` member |
| -------------------------------- | --------------------------------------------- | -------------- |
| `0x00` `MARKER_IMM`              | address = u64 immediate                        | `addr_immediate` |
| `0x40` `MARKER_SHAPE_REG`        | addr imm, shape from register                  | `addr_immediate` |
| `0x80` `MARKER_ADDR_REG`         | address from register pair (`reg_lo`,`reg_hi`)  | `addr_reg` |
| `0xC0` `MARKER_ADDR_SHAPE_REG`   | addr from reg, shape from reg                   | `addr_reg` |
| `0x20`/`0x60` `MARKER_ADDR_TBL_*`| address from an address **table** (`table_index` + `offset`) | `addr_table` / `addr_tbl_offs` |

So the base address can be a literal, a value held in two sequencer registers, or a table lookup
(`table_index` into an address table + a 32-bit or 64-bit register/immediate offset). This is exactly how
`TensorLoad` chains: an earlier `TensorLoad` pulls a pointer into registers, and a later instruction's
`addr_reg` consumes it (register-indirect addressing). `[HIGH/OBSERVED for the marker table; the chaining
*use* is MED/INFERRED from `addr_reg`.]`

**DST = the sequencer general-purpose register file.** The 32-byte `data` field carries up to **32**
register numbers (`NEURON_ISA_TPB_REG_NUM = uint8_t`, `common.h:686`), *"used in order first to last"*
(`mem_2d.h:66`) — `data[0]` receives the first loaded element, `data[1]` the second, and so on. The register
file is 64 GPRs wide (`NEURON_ISA_TPB_NUM_REGISTERS = 64U`, `common.h:31`; the `ENGINE_REGISTERS` enum
runs `R0..R63`, `common.h:1009+`); a single `TensorLoad` names up to 32 of them. `[HIGH/OBSERVED]`

> **QUIRK — "32" is the element count, the register *file* is 64.** Do not read the "32 register pointers"
> as a 32-register file. The cap is on *how many elements one instruction loads* (`num_elem[0]*num_elem[1] ≤
> 32`), and the `data` array is 32 `REG_NUM` slots. Each slot is a `uint8_t` and can name any of the 64
> sequencer GPRs. To fill more than 32 registers you issue more than one `TensorLoad`. `[HIGH/OBSERVED]`

> **NOTE — the registers are an addressable HW block.** In the Q7 NX address view the sequencer register
> block appears as *"NX MEM REG BLOCK (Sequencer Registers)"* at `[0x100000, 0x100840)`. The `R0..R31`
> ↔ `0x100000` byte binding is a name-level match, not byte-traced in this pass. `[MED/OBSERVED]`

The reachable memory spaces are detailed on the [Memory Model](../../dma/sbuf-psum-banks.md) page; the
on-chip SBUF/PSUM bank geometry is on the [SBUF + PSUM bank model](../../dma/sbuf-psum-banks.md) page
(planned). The partition-offset encoding of an SBUF vs PSUM address is documented in-header at
`common.h:455-468`: bit `[25] == 0` ⇒ SBUF, bit `[25] == 1` ⇒ PSUM; quadrant select in `[24:23]` (SBUF) /
`[21:20]` (PSUM). `[HIGH/OBSERVED]`

---

## 3. The operand struct — `NEURON_ISA_TPB_MEM_2D_STRUCT` (64 B, compile-verified) `[HIGH/OBSERVED]`

One struct serves both `0xaa` and `0xab`. The 64-byte layout was confirmed by `gcc` compile-verify
(`offsetof`/`sizeof`) against the shipped headers, **byte-identical in all four gens**:

| off | size | field                       | type                          | `TensorLoad` role |
| --- | ---- | --------------------------- | ----------------------------- | ----------------- |
| 0   | 4    | `header`                    | `NEURON_ISA_TPB_HEADER`       | `header.opcode = 0xaa`; `inst_word_len`; `debug_cmd`/`debug_hint` |
| 4   | 8    | `events`                    | `NEURON_ISA_TPB_EVENTS`       | wait/update semaphore sync (standard) |
| 12  | 1    | `dtype`                     | `NEURON_ISA_TPB_DTYPE`        | element dtype (the 8-type set, §4) |
| 13  | 1    | `src_datasrc`               | `NEURON_ISA_TPB_DATA_SRC`     | **forced `0` (Register)** for load |
| 14  | 2    | `num_elem[2]`               | `uint8_t[2]`                  | 2-D element counts `n0`,`n1`; `n0*n1 ≤ 32` = number of loads |
| 16  | 8    | `start_addr`                | `NEURON_ISA_TPB_ADDR8`        | u64 Neuron base address (imm / reg / table; §2) |
| 24  | 8    | `step_elem[2]`              | `int32_t[2]`                  | signed element strides along each 2-D dim |
| 32  | 32   | `data`                      | `NEURON_ISA_TPB_MEM2D_DATA`   | 32 destination `REG_NUM`s (register interpretation) |

`ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_MEM_2D_STRUCT) == 64, …)` is asserted in-header at `mem_2d.h:128`.
Compile-verify output (identical sunda / cayman / mariana / maverick):

```
sizeof(MEM_2D)=64
header      off=0  sz=4
events      off=4  sz=8
dtype       off=12 sz=1
src_datasrc off=13 sz=1
num_elem    off=14 sz=2
start_addr  off=16 sz=8
step_elem   off=24 sz=8
data        off=32 sz=32
```

> **GOTCHA — `start_addr` is a u64, the strides are `int32_t`, and they are *not* in a tensor2d sub-struct.**
> The header comment (`mem_2d.h:111-120`) explains why: this is *"not tensor2d because — u64 start_addr —
> i32 step"*, and the fields are *"not in a separate struct due to packing/alignment issues."* So the 2-D
> descriptor is `{ u64 start_addr; u8 num_elem[2]; i32 step_elem[2]; }` flattened directly into the
> instruction word. The strides are **signed** — negative strides walk backwards through memory. `[HIGH/OBSERVED]`

### The `data` union and the register-only constraint

`data` is the union `NEURON_ISA_TPB_MEM2D_DATA` (`common.h:738-747`):

```c
typedef union NEURON_ISA_TPB_MEM2D_DATA {
    NEURON_ISA_TPB_REG_NUM registers[32];  // REG_NUM = uint8_t : the LOAD interpretation
    uint8_t                uint8[32];      // ─┐
    int8_t                 int8[32];       //  │
    uint16_t               uint16[16];     //  │ packed-immediate overlays —
    int16_t                int16[16];      //  │ used by TensorStore-Immediate ONLY
    uint32_t               uint32[8];      //  │ (a u64 store reinterprets uint32[8] as 4×u64)
    int32_t                int32[8];       //  │
    float                  fp32[8];        // ─┘
} NEURON_ISA_TPB_MEM2D_DATA;
```

For `TensorLoad`, **only `registers[32]` is meaningful** — `src_datasrc` is forced to `Register`, so the
load can only target registers. The immediate overlays exist solely for the `TensorStore`-Immediate path.
`[HIGH/OBSERVED]`

### Validity contract — `is_valid_tensor_load`

The in-header validity predicate (`mem_2d.h:138-147`) ANDs the following; this is the exact decode-time
acceptance gate:

```c
// is_valid_tensor_load(i):
//   has_valid_neuron_header(i)         // header well-formed
//   && has_valid_neuron_events(i)      // event/sync well-formed
//   && has_tensor_load_opcode(i)       // header.opcode == TensorLoad  (mem_2d.h:270)
//   && mem2d_shared_constraints(i)     // num_elem 1..32 each dim  AND  dtype in the 8-type set
//   && mem2d_datasrc_zero(i)           // src_datasrc == Register   (mem_2d.h:228)  <- LOAD-ONLY
//   && mem2d_32_elem_check(i)          // n0*n1 <= 32               (mem_2d.h:237)
//   && mem2d_u64_register_check(i)     // if u64+Register, all reg numbers EVEN
//   && valid_start_addr(i)             // is_aligned_start_addr8(start_addr, dtype)
```

The key contrast vs `TensorStore`: where `TensorStore` calls `mem2d_datasrc_valid` (Register **or**
Immediate) and `mem2d_store_num_elem_check` (per-dtype caps), `TensorLoad` calls `mem2d_datasrc_zero`
(Register *only*) and the uniform `mem2d_32_elem_check` (≤32). The struct is symmetric; the asymmetry is
only the Immediate path, present for STORE and forbidden for LOAD. `[HIGH/OBSERVED]`

---

## 4. dtype-extend on load — no float cast, integer sign/zero-extension only `[HIGH/OBSERVED]`

This is the defining datapath behavior. `TensorLoad` is *not* a numeric converter — there is **no
`fp↔int`, no `fp32↔bf16`, no rounding/pack step** on the load path. It performs a **width-normalize** into
a 32-bit (or 64-bit) register, with classic scalar-load extension semantics (`mem_2d.h:57-67`):

| dtype  | enum (`common.h:711-719`) | on-load into the destination register      |
| ------ | ------------------------- | ------------------------------------------ |
| UINT8  | `0x3`                     | **zero**-extend → 32-bit                    |
| UINT16 | `0x5`                     | **zero**-extend → 32-bit                    |
| INT8   | `0x2`                     | **sign**-extend → 32-bit                    |
| INT16  | `0x4`                     | **sign**-extend → 32-bit                    |
| UINT32 | `0x9`                     | bit-copy → 32-bit                           |
| INT32  | `0x8`                     | bit-copy → 32-bit                           |
| FP32   | `0xA`                     | **bit-copy** (raw 32-bit pattern) → 32-bit — *not* reinterpreted as a number |
| UINT64 | `0x1`                     | loaded across an **even register pair** (reg `n` ← lo32, reg `n+1` ← hi32) |

The accepted set is enforced by `mem2d_dtype_check` (`mem_2d.h:176-185`) — exactly these eight types.
`[HIGH/OBSERVED]`

> **QUIRK — FP32 is a bit-copy, and the float types are *absent*.** Loading an `FP32` element copies the
> 32-bit IEEE pattern into the register unmodified — the sequencer GPR holds the raw bits, with no
> conversion. And note the full `Dtype` enum has `BFLOAT16 (0x6)`, `FP16 (0x7)`, `FP8_EXP{3,4,5}
> (0xD/0xE/0xF)`, `FP32R (0xB)` — *none* of which `TensorLoad` accepts. Its set is the integer + `fp32` +
> `u64` *scalar* set, not the compute-dtype set. `TensorLoad` is a scalar memory fetch, so it sees only
> what a scalar register can hold. `[HIGH/OBSERVED]`

> **QUIRK — `u64` consumes a register pair and forces even register numbers.** Because each `u64` element
> fills `data[k]` *and* `data[k]+1`, `mem2d_u64_register_check` (`mem_2d.h:187-226`) requires **every** one
> of the 32 `data[]` entries to be even when `dtype == UINT64` (the predicate `mem2d_odd_register_used`
> ORs `registers[i] % 2 != 0` over all 32 slots). The in-header note states it directly: *"for u64 dtype,
> all registers must be even numbered (e.g. r0, r14, etc.)"* (`mem_2d.h:67`). `[HIGH/OBSERVED]`

Where this sits in the data-movement family (the dtype-convert column):

| instruction          | opcode | struct       | dtype-convert on move?                                |
| -------------------- | ------ | ------------ | ----------------------------------------------------- |
| **TensorLoad**       | `0xaa` | `MEM_2D`     | **NO cast; integer zero/sign-extend to 32 b only**     |
| TensorStore          | `0xab` | `MEM_2D`     | NO cast; truncating store to dtype width               |
| MOVE                 | `0xa7` | `CTRL_MV`    | NO cast; full-register move gated to `{u32,i32,fp32}`  |
| Copy                 | `0x46` | `S4D4_TR`    | element copy, *may* convert dtype                      |
| Cast                 | `0x47` | `S4D4_TR`    | element copy, *does* convert dtype (fp round/pack)     |

`TensorLoad` and `TensorStore` are the only members that move *with width extension/truncation but no
numeric cast*; `Copy`/`Cast` (see [Cast/Copy](./cast-copy.md), [dtype model](./dtype-model.md)) are the
element-wise numeric converters. `[HIGH/OBSERVED]`

---

## 5. Dispatch surface — sequencer-native, not a POOL kernel `[HIGH/OBSERVED]`

There are two GPSIMD dispatch surfaces: **(A)** the Q7-POOL `kernel_info_table` (software kernels routed by
`funcVA`), and **(B)** the sequencer/engine-native opcodes decoded in hardware. `TensorLoad` is firmly **(B)**,
proven two ways.

**(B1) Absent from every POOL `kernel_info_table`.** Decoding the carved POOL EXTISA images with the shipped
Cadence `ncore2gp xtensa-elf-readelf` (8-byte entry, opcode @ `+3` BE, `funcVA` @ `+4` LE):

- **SUNDA** (`extisa_SUNDA_POOL_PERF_EXTISA_0.so`, `kernel_info_table` §[6], 18 entries):
  `0x41 0x43 0x44 0x46 0x47 0x49 0x67 0x68 0x74 0x79 0x7a 0x7c 0x7d 0x7e 0x92 0xb8 0xbb 0xe7`.
- **CAYMAN** (`extisa_CAYMAN_POOL_PERF_EXTISA_0.so`, §[7], 17 entries):
  `0x41 0x45 0x46 0x47 0x51 0x52 0x7b 0x7c 0x7d 0x7e 0xbe 0xf0(×5) 0xf2`.
- **MAVERICK** (`kernel_info_table`, 17 entries): `0xaa` absent.

`0xaa` (and `0xab`) appear in **none** of them. Note `0xb8` (`DMA_MEMCPY`) and `0xbb` (`DMA_INDIRECT`)
**do** appear in the SUNDA POOL table — the *bulk* tensor movers route through POOL; the scalar
`TensorLoad` does not. `[HIGH/OBSERVED — carved binaries + ncore2gp readelf; CARRIED for the carve decode.]`

**(B2) 16-copy SEQ-blob residency.** In the shipped host `libnrtucode_internal.so`, every engine's device
firmware blob is baked into `.data` with self-name DEBUG tags `S: <Name>`. Counting the tag directly on the
binary (`rg -ao 'S: TensorLoad' libnrtucode_internal.so | wc -l`) partitions cleanly by engine:

| tag                 | copies | engine family (by blob multiplicity) |
| ------------------- | ------ | ------------------------------------ |
| `S: TensorLoad`     | **16** | **iTPB SEQUENCER** (this op)          |
| `S: TensorStore`    | 16     | iTPB SEQUENCER (mirror)               |
| `S: WRITE`          | 16     | iTPB SEQUENCER                        |
| `S: MOVE`           | 16     | iTPB SEQUENCER                        |
| `S: FindIndex8`     | 4      | DVE                                   |

`TensorLoad` / `TensorStore` / `WRITE` / `MOVE` all land at exactly **16** — they reside in the same 16
sequencer-engine blobs; `FindIndex8` at **4** is a DVE op. So the "S:" surface, read by multiplicity,
independently says `TensorLoad` = SEQUENCER engine. `[HIGH/OBSERVED — re-grounded counts this pass.]`

> **NOTE — the "S:" tags are *not* DVE-only.** A refinement of the earlier method: the `S:` self-name tags
> exist in *every* engine's firmware blob (sequencer, DVE, DMA all carry them — `S: DMA` appears 32×, both
> descriptor halves). The clean discriminators are therefore (i) **membership** in the POOL
> `kernel_info_table` (= POOL software kernel) vs **absence** (= hardware/engine-native), and (ii) for the
> native ones, the **blob multiplicity** of the `S:`-tag (16 = SEQ, 4 = DVE, 3 = ACT/PE/POOL-fold).
> `TensorLoad`: absent from the POOL table **and** 16-copy tag ⇒ **iTPB sequencer-native**. `[HIGH/OBSERVED]`

**Verdict.** `TensorLoad (0xaa)` is an iTPB **sequencer** (control-engine) instruction, decoded by the same
front-end that runs `WRITE`/`MOVE`/`ALU_OP`/`COMPARE_BRANCH`/`TensorStore` — the `0xa5..0xab` control block.
It is neither a DVE compute op nor a Q7-POOL software kernel. `[HIGH/OBSERVED]`

---

## 6. Dispatch chain — per gen (identical sunda → maverick) `[HIGH/OBSERVED + MED]`

Because `TensorLoad` is sequencer-native, the chain has **no** `callx8` / `kernel_info` / `funcVA` hop — no
POOL trampoline. Per gen, identical:

1. **(compile)** The compiler may emit `PSEUDO_TENSOR_LOAD (0xce)`, a `NEURON_ISA_TPB_PSEUDO_MEM_2D_STRUCT`
   (also 64 B), carrying a **symbolic** address: `var_id (u32, off 16-19)` + `var_offset (u32, off 20-23)`
   *instead of* a resolved `start_addr`. `[HIGH/OBSERVED]`
2. **(lower / bind)** NRT resolves `var_id`+`var_offset` to a concrete 64-bit Neuron address and rewrites
   `0xce → 0xaa`, materializing the real `MEM_2D` instruction with `start_addr` filled in. The pseudo→real
   binding is `PSEUDO_MEM_2D_STRUCT → {PSEUDO_TENSOR_LOAD, PSEUDO_TENSOR_STORE}` (`struct2pseudo_opcode`,
   all gens). `[HIGH/OBSERVED for the binding; the resolver internals MED/INFERRED.]`
3. **(encode)** `instruction = MEM_2D_STRUCT`, `header.opcode = 0xaa`, `src_datasrc = 0`, `num_elem[0..1]`,
   `start_addr` (ADDR8), `step_elem[0..1]`, `data` = up to 32 destination register numbers. `[HIGH/OBSERVED]`
4. **(validate)** `is_valid_tensor_load` (§3). `[HIGH/OBSERVED]`
5. **(decode)** The sequencer front-end matches `header.opcode == 0xaa` in the `0xa5..0xab` control block and
   routes to the `mem_2d` load handler — the device blob whose self-name DEBUG tag is `S: TensorLoad` (16
   copies). No `kernel_info_table` lookup, no `funcVA`, no POOL `callx8`. `[HIGH/OBSERVED for "no POOL hop";
   MED for the exact handler VA — inlined/templated, no discrete host symbol.]`
6. **(execute)** The 2-D scalar-gather loop (§7). `[HIGH per-element rule; MED for the exact walk order.]`

> **CORRECTION — the pseudo form drops `u64` and the address-alignment checks.** The pseudo validity
> predicate `pseudo_mem2d_dtype_check` (`pseudo_mem_2d.h:155-163`) accepts only `{u8,i8,u16,i16,u32,i32,fp32}`
> — **`UINT64` is *absent*** at the compiler level — and `is_valid_pseudo_tensor_load` (`pseudo_mem_2d.h:125`)
> omits `mem2d_u64_register_check` and `valid_start_addr` entirely. Those address-alignment and even-pair
> constraints are enforced only *after* lowering, against the resolved `0xaa`. So a `u64` `TensorLoad` cannot
> originate as a pseudo; it must be emitted as a direct `0xaa`. This is a detail the `mem_2d.h`-only view does
> not surface. `[HIGH/OBSERVED — pseudo_mem_2d.h.]`

---

## 7. Algorithm — the 2-D scalar-gather load loop `[HIGH spec / MED uarch]`

`TensorLoad` is a hardware sequencer instruction — there is no Q7 `ivp_*`/TIE op named `tensor_load` in the
Vision-DSP roster; the sequencer decodes and executes it, not a Q7 kernel body. The struct + spec prescribe
the following. The element address is `start_addr + (i0·step_elem[0] + i1·step_elem[1])·sizeof(dtype)` — a
2-D strided walk (`base + inner_stride·i0 + outer_stride·i1`), the canonical "base + row-stride × row" access
pattern, generalized to two dimensions with signed element strides.

```c
// Symbol: NEURON_ISA_TPB_MEM_2D_STRUCT executed under header.opcode == 0xaa (TensorLoad).
// R[] is the sequencer GPR file (64 regs); data[] are REG_NUM destinations, first-to-last.
// Spec: aws_neuron_isa_tpb_mem_2d.h:54-101 ; validity: is_valid_tensor_load (mem_2d.h:138-147).
static void tensor_load_exec(const NEURON_ISA_TPB_MEM_2D_STRUCT *i, uint32_t R[64])
{
    const unsigned   n0   = i->num_elem[0];          // 1..32, inner 2-D dim
    const unsigned   n1   = i->num_elem[1];          // 1..32, outer 2-D dim   (n0*n1 <= 32)
    const int32_t    s0   = i->step_elem[0];         // signed element stride, inner
    const int32_t    s1   = i->step_elem[1];         // signed element stride, outer
    const unsigned   esz  = dtype_size_bytes(i->dtype); // 1/2/4/8 ; start_addr must be esz-aligned
    const uint64_t   base = addr8_resolve(&i->start_addr); // imm | reg-pair | table  (common.h:553)

    // PRECONDITION (decode-time, is_valid_tensor_load):
    //   src_datasrc == Register; n0,n1 in 1..32; n0*n1 <= 32; dtype in the 8-type set;
    //   if dtype==u64 then every data[k] is even; base is dtype-aligned;
    //   the whole span [min(addr)..max(addr)] lies within ONE aligned 16MB memory window.

    unsigned k = 0;                                  // index into data[] = destination registers
    for (unsigned i1 = 0; i1 < n1; ++i1) {           // outer 2-D dimension
        for (unsigned i0 = 0; i0 < n0; ++i0) {       // inner 2-D dimension
            // element address via the 2-D stride walk (strides are signed -> may decrement)
            uint64_t ea = base + (int64_t)(s0 * (int)i0 + s1 * (int)i1) * (int)esz;

            uint8_t  reg = i->data.registers[k];     // next destination, first-to-last
            switch (i->dtype) {
                case DTYPE_UINT8:  R[reg] = (uint32_t)(uint8_t) load8 (ea);  break; // zero-extend
                case DTYPE_UINT16: R[reg] = (uint32_t)(uint16_t)load16(ea);  break; // zero-extend
                case DTYPE_INT8:   R[reg] = (uint32_t)(int32_t)(int8_t) load8 (ea); break; // sign-extend
                case DTYPE_INT16:  R[reg] = (uint32_t)(int32_t)(int16_t)load16(ea); break; // sign-extend
                case DTYPE_UINT32:
                case DTYPE_INT32:
                case DTYPE_FP32:   R[reg] = load32(ea);  break;                // bit-copy (fp32 raw bits)
                case DTYPE_UINT64: {                                           // even reg pair
                    uint64_t v = load64(ea);
                    R[reg]     = (uint32_t)(v & 0xFFFFFFFFu);                   // lo32 -> reg n
                    R[reg + 1] = (uint32_t)(v >> 32);                          // hi32 -> reg n+1
                    break;
                }
            }
            ++k;
        }
    }
    // POSTCONDITION: k == n0*n1; no continuation -- a single TensorLoad gathers <= 32 scalars.
}
```

Notes on the contract:

- **Alignment.** `start_addr` must be `dtype`-aligned (`is_aligned_start_addr8`, `mem_2d.h:160-162`). For
  `u64`+Register, every destination register number is even (`mem2d_u64_register_check`). `[HIGH/OBSERVED]`
- **No multi-tile continuation.** There is no continuation inside one `TensorLoad` — it is bounded to ≤32
  scalars in *one* 16 MB window (`mem_2d.h:71-73`). Larger transfers use the bulk DMA movers
  (`DMA_MEMCPY 0xb8` / `DMA_INDIRECT 0xbb`). `[HIGH/OBSERVED]`
- **Mechanism.** This is a sequencer scalar load (memory read into the GPR file), **not** a DMA descriptor
  and **not** a vector-regfile load. The "no DMA" reading is grounded in §5 (absent from the POOL DMA-kernel
  table, sequencer-blob residency). `[HIGH/OBSERVED classification; the exact 16 MB-window TLB programming is
  MED.]`
- **Complexity.** `O(n)`, `n = n0·n1 ≤ 32` scalar fetches, single pass. `[HIGH bound.]`
- **MED/INFERRED.** The exact 2-D address-walk *order* (the `step_elem` stride math above is the standard
  `mem_2d`/Tensor pattern, not instruction-stream-traced); whether `step_elem` is in elements or bytes (the
  header says *"step"* in the element-stride context — taken here as element-stride, scaled by `sizeof(dtype)`).
  `[MED/INFERRED.]`

---

## 8. Per-gen presence — byte-exact SUNDA → MAVERICK `[HIGH/OBSERVED]`

ISA-definition presence: opcode enum byte + `// Y` maintained flag + `struct2opcode` map + `mem_2d.h` spec
block + 64 B compile-verify — all from the shipped arch-isa headers + JSON:

| gen      | NC-ver | enum `0xaa` `// Y` | `MEM_2D` spec block | `struct2opcode → MEM_2D` | struct 64 B | verdict           |
| -------- | ------ | ------------------ | ------------------- | ------------------------ | ----------- | ----------------- |
| sunda    | NC-v2  | YES (`cmn:253`)    | YES                 | YES                      | YES         | PRESENT `[HIGH/OBS]` |
| cayman   | NC-v3  | YES (`cmn:250`)    | YES                 | YES                      | YES         | PRESENT `[HIGH/OBS]` |
| mariana  | NC-v4  | YES (`cmn:255`)    | YES                 | YES                      | YES         | PRESENT `[HIGH/OBS]` |
| maverick | NC-v5  | YES (`cmn:258`)    | YES                 | YES                      | YES         | PRESENT `[HIGH/OBS]` |

`struct2opcode.NEURON_ISA_TPB_MEM_2D_STRUCT` returns exactly `["…TENSOR_LOAD","…TENSOR_STORE"]` in **all four**
`instruction_mapping.json` files (verified this pass with `jq`). `TENSOR_LOAD (0xaa)` is therefore ISA-defined,
maintained (`// Y`), and 64 B-struct-identical in every gen from the earliest (SUNDA/NC-v2) — no
gen-presence asymmetry, no deprecation. `[HIGH/OBSERVED]`

> **NOTE — v5 caveat.** The MAVERICK *header* contract is byte-identical (enum byte, struct size, spec
> block, JSON binding all OBSERVED). The MAVERICK *device-blob interior* (the in-firmware handler bytes) is
> not re-disassembled this pass; its presence is asserted from the header + the 16-copy `S:`-tag count, not
> from a MAVERICK instruction-stream decode. `[v5 interior INFERRED.]`

Binary dispatch-surface presence (the "is it a POOL kernel?" question), carved firmware via `ncore2gp
readelf` — `0xaa` **absent from every available POOL table** (SUNDA 18-entry, CAYMAN 17-entry, MAVERICK
17-entry; the MARIANA POOL carve was not re-decoded — absence inferred from the identical header struct).
SEQ-blob residency: `S: TensorLoad` = 16 (this pass), matching `WRITE`/`MOVE`/`TensorStore`. `[HIGH/OBSERVED;
MARIANA carve MED/INFERRED.]`

---

## 9. dtype matrix `[HIGH/OBSERVED — mem2d_dtype_check + spec]`

`TensorLoad (0xaa)` accepts exactly these 8 dtypes (`mem2d_dtype_check`, `mem_2d.h:176-185`); enum bytes from
`common.h:711-719`:

| dtype  | enum  | on-load into register     | max #loads / inst (Register dst) |
| ------ | ----- | ------------------------- | -------------------------------- |
| UINT8  | `0x3` | zero-extend → 32 b        | 32  (`n0*n1 ≤ 32`)               |
| INT8   | `0x2` | sign-extend → 32 b        | 32                               |
| UINT16 | `0x5` | zero-extend → 32 b        | 32                               |
| INT16  | `0x4` | sign-extend → 32 b        | 32                               |
| UINT32 | `0x9` | bit-copy → 32 b           | 32                               |
| INT32  | `0x8` | bit-copy → 32 b           | 32                               |
| FP32   | `0xA` | bit-copy (raw 32 b) → 32 b | 32                               |
| UINT64 | `0x1` | lo/hi → even reg pair     | 32 (each consumes a pair; reg# even) |

> **GOTCHA — the per-dtype caps (16 / 8 / 4) are a STORE thing, not a LOAD thing.** The `mem2d_store_num_elem_check`
> caps (`u16 ≤ 16`, `u32/fp32 ≤ 8`, `u64 ≤ 4`) apply *only* to the `TensorStore`-Immediate path, where `data`
> holds *packed immediates* and a wider dtype consumes more of the 32-byte field. `TensorLoad` always uses
> register destinations, so its sole numeric cap is `mem2d_32_elem_check` — `n0*n1 ≤ 32` — uniform across all
> 8 dtypes. (A `u64` load still uses ≤32 `data` slots, but pairs them into ≤16 even/odd register pairs.)
> `[HIGH/OBSERVED]`

No `fp32r` / `bf16` / `fp16` / `fp8` in the `TensorLoad` set — it is the integer + `fp32` + `u64` scalar set,
not the compute-dtype set. `[HIGH/OBSERVED]` See the full GPSIMD [datatype model](./dtype-model.md) for how
these enum bytes map across the whole ISA.

---

## 10. Relationship to TensorStore (`0xab`) `[HIGH/OBSERVED]`

[TensorStore (`0xab`)](./tensorstore.md) is the exact mirror and shares *everything* structural:

- **same** struct `NEURON_ISA_TPB_MEM_2D_STRUCT` (64 B), **same** spec file (`mem_2d.h`), **same**
  `0xa5..0xab` sequencer control block, **same** 16-copy `S: TensorStore` SEQ-blob residency, **same** absence
  from the POOL `kernel_info_table`, **same** 4-gen `// Y` presence (`sunda:254 cayman:251 mariana:256
  maverick:259`).
- **opposite direction**: registers/immediates → Neuron memory, vs `TensorLoad`'s memory → registers.
- **the one added axis**: `src_datasrc` may be `0` (Register) **or** `1` (Immediate). For Immediate, the
  32-byte `data` field is *packed immediate values* (`u8[32]`/`u16[16]`/`u32[8]`/`fp32[8]`, with `u64`
  reinterpreting `uint32[8]` as 4×u64) and the per-dtype element-count caps apply
  (`mem2d_store_num_elem_check`: `u8 ≤ 32`, `u16 ≤ 16`, `u32/fp32 ≤ 8`, `u64 ≤ 4`). `TensorLoad` has **no**
  immediate path (`src_datasrc` forced `0`).
- **no dtype cast on store either** — a truncating width store, the exact mirror of the zero/sign-extend
  load.

So `TensorStore` inherits §1–§9 verbatim with the direction flipped and the `datasrc=Immediate` branch added.
`[HIGH/OBSERVED]`

---

## 11. Cross-references

- [TensorStore (`0xab`)](./tensorstore.md) — the `mem_2d` *store* mirror (same struct, opposite direction,
  adds the immediate-source branch).
- [ISA Batch 06 — Vector Loads](../../isa/ref/b06-loads.md) — the *vector*-register `ivp_l*` load family
  (memory → vector regfile); contrast with `TensorLoad`'s scalar memory → sequencer-GPR path.
- [SBUF + PSUM bank model](../../dma/sbuf-psum-banks.md) — the on-chip State-Buffer / PSUM bank geometry
  (planned); explains the partition-offset encoding `TensorLoad` addresses with.
- [Memory Model (HBM, DataRAM, Translation Windows)](../../dma/sbuf-psum-banks.md) — the full 64-bit Neuron
  Address space and the 16 MB memory-window TLB constraint that bounds a `mem_2d` access.
- [The Unified Datatype Model](./dtype-model.md) — the `Dtype` enum bytes and the GPSIMD type lattice.
- [The Opcode Catalog / Ledger](./opcode-catalog-ledger.md) — the full sequencer/engine opcode map.

---

## 12. Confidence / provenance summary

**HIGH / OBSERVED**

- opcode `0xaa`, `// Y` maintained, all four gen `common.h` enums (byte-pinned, line-cited);
  `struct2opcode → {TENSOR_LOAD, TENSOR_STORE}` in all 4 `instruction_mapping.json`.
- `TensorStore 0xab` the contiguous mirror.
- operand struct `NEURON_ISA_TPB_MEM_2D_STRUCT`, 64 B, all offsets + sub-sizes (`gcc` compile-verified in all
  four gens, byte-identical).
- SRC = full 64-bit Neuron Address space (SBUF / PSUM / DataRAM / DRAM / HBM via 16 MB windows); `start_addr`
  is `ADDR8` (imm / reg-pair / table). DST = sequencer GPR file (64 regs; ≤32 named per instruction); one
  16 MB window per access.
- dtype set `{u8,i8,u16,i16,i32,u32,fp32,u64}`; on-load = integer zero/sign-extend to 32 b + `i32/u32/fp32`
  bit-copy + `u64` even-pair — **no numeric cast**.
- dispatch surface: **absent** from every POOL `kernel_info_table` (SUNDA/CAYMAN/MAVERICK carves) ⇒ not a POOL
  software kernel; 16-copy `S: TensorLoad` SEQ blob ⇒ iTPB sequencer-native (counts re-grounded on the binary
  this pass).
- ISA-present + maintained in all four gens SUNDA (NC-v2) → MAVERICK (NC-v5).
- pseudo `0xce` lowering: `PSEUDO_MEM_2D_STRUCT` (64 B, `var_id`+`var_offset` instead of `start_addr`) →
  `{PSEUDO_TENSOR_LOAD, PSEUDO_TENSOR_STORE}`; pseudo dtype set drops `u64`.

**MED / INFERRED**

- exact 2-D address-walk order (the `step_elem` stride math is the standard `mem_2d` pattern, not
  instruction-stream-traced); element-vs-byte stride scaling.
- exact in-firmware handler VA (inlined/templated; no discrete host symbol).
- `R0..R31` ↔ NX `0x100000` "Sequencer Registers" block exact byte binding (name match).
- MARIANA POOL-carve absence (inferred from the identical header struct, not re-decoded).
- MAVERICK (v5) device-blob interior (header-OBSERVED only; interior INFERRED).

**CORRECTION (HIGH / OBSERVED).** The task framing "POOL-side tensor data-movement LOAD" is refined:
`TensorLoad` is **not** a POOL kernel and **not** a bulk tile/tensor DMA — it is the iTPB **sequencer scalar
memory → register load** (≤32 scalars). The bulk tensor movers are `DMA_MEMCPY (0xb8)` / `DMA_INDIRECT
(0xbb)`, which *are* POOL kernels. Additionally surfaced this pass (not in the `mem_2d.h`-only view): the
**pseudo form** `0xce` carries a symbolic `var_id`+`var_offset` (not a resolved address) and **omits `u64`**
plus the alignment/even-pair checks, all enforced only after NRT lowers `0xce → 0xaa`.
