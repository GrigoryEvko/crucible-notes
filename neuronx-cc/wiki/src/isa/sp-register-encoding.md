# SP Register-Lane + TensorLoad/Save Encoding

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; the cp311/cp312 wheels rebuild `libwalrus.so`, so confirm any address against the target wheel — see [Build & Version Provenance](../reference/versions.md)). The `CoreV2GenImpl::visitInst*` encoders and the `sub_*` field-setter helpers live in `libwalrus.so` (`.text`/`.rodata`, VA == file offset; cp310 GNU build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`); the `bir::Inst*` kinds and `bir::AluOpType` enum live in `libBIR.so` (build-id `a9b1ea38…`); the `rl_*` / `m2d_*` validator field names come from `neuronxcc/isa_tpb/sunda/neuron_isa_tpb_pybind.*.so`. Treat every address as version-pinned.*

## Abstract

This page is the byte-for-byte field map of the **SP register-lane compute and memory ops** — the bundles the SP-class sequencer emits to move scalars between named registers, do scalar ALU on them, and stream a register lane to/from memory. Four BIR kinds are covered: `RegisterMove` (`0xA7`), `RegisterAlu` (`0xA8`), `TensorLoad` (`0xCE`/`0xAA`) and `TensorSave` (`0xCD`/`0xAB`). The functional model — what the SP engine *does* with the named register file and how loops are pre-lowered into counter-ALU + branch pairs — is [1.12 the SP engine](../arch/sp-engine.md); the sync/branch/control band that shares the same 64-byte skeleton is [2.20 SP sync / branch / control encoding](sp-sync-encoding.md). This page is the *encoding*: for each op, the 64-byte bundle field map, the value source, and the disassembly store-site that pins it.

Every bundle is a `std::array<unsigned char, 64>`: zero-filled by its constructor (so any byte the encoder does not write is a hard `0x00`), opcode-stamped, sync-band-filled, payload-filled by the op-specific encoder, optionally validated, and `fwrite(buf, 1, 0x40, bin)`-ed. Three things recur and are worth fixing up front. **First**, the `CodeGenMode` fork (`*((int*)this+156)`): `==1` GENERATE_ISACODE emits the 64 bytes; `==2` RUN_ISA_CHECKS builds the same bundle on the stack and runs the pybind ISA validator (vtable slot) without emitting; `==0` COLLECT_OPCODES records only the opcode tag; anything else is a `FATAL`. The field map below is the GENERATE path; the RUN_ISA_CHECKS path writes the *same offsets* (confirmed for `RegisterMove`, where the on-stack `__m128i` pair lands src@+16, dst@+24/+25, imm@+32 identically). **Second**, the `imm-vs-reg` distinction is a single bundle byte (`+0x0E`) on `RegisterMove`/`RegisterAlu`, but for `TensorLoad`/`TensorSave` it is an **opcode + helper-template** fork driven by the memory access-pattern *kind*. **Third**, every memory op embeds a `MEM_PATTERN2D` (`m2d_*`) descriptor, byte-identically across its two address modes (only the opcode and the DRAM `var_id`/`var_offset` differ).

> **GOTCHA — `Load`/`Save` are not register-lane bundles; `TensorLoad`/`TensorSave` are.** `bir::InstLoad` (`visitInstLoad` @ `0x127a620`) and `bir::InstSave` (@ `0x127a640`) are thin wrappers, each literally `return generateDynamicDMA(this, inst, *((int*)inst + 84));`. They compile to a **dynamic DMA descriptor**, not to a 64-byte bundle. The bundle-encoded SP load/save instructions are `bir::InstTensorLoad` and `bir::InstTensorSave` (§4–§6).

For reimplementation the contract is:

- The shared 12-byte control band (opcode + sync wait/update) and the `CodeGenMode` fork (§1).
- `RegisterMove` (§2): the `valid_num` 32-/64-bit selector, the `src_mode` imm-vs-reg byte, and the dst reg-pair.
- `RegisterAlu` (§3): the `_ALU_OP` wire byte at `+0x0C` (the §3.1 30-arm LUT, shared with [1.12](../arch/sp-engine.md)), the `operand_mode` byte, and the register/immediate operand slots.
- `TensorLoad`/`TensorSave` (§4–§6): the opcode/mode dispatch on AP kind, the embedded `m2d` descriptor, and the IMM-mode `var_id`/`var_offset` injection.

## At a glance

| | |
|---|---|
| **Encoders** | `CoreV2GenImpl::visitInstRegisterMove` @ `0x12202e0` · `visitInstRegisterAlu` @ `0x12430e0` · `visitInstTensorLoad` @ `0x1251890` · `visitInstTensorSave` @ `0x1252580` |
| **Opcodes** | `RegisterMove 0xA7` · `RegisterAlu 0xA8` · `TensorLoad 0xCE`(imm)/`0xAA`(reg) · `TensorSave 0xCD`(imm)/`0xAB`(reg) |
| **Bundle** | `std::array<unsigned char, 64>`, zero-init, `fwrite … 0x40`; finalize/append `sub_12095A0` (Generator+64) |
| **Sync band** | `+0x04` wait_mode (`sub_120BE70`) · `+0x05` wait_idx · `+0x06` update_mode (`sub_120C430`) · `+0x07` update_idx · `+0x08` value/reg dword |
| **ALU op wire** | `+0x0C` ← `sub_12039C0(op)` = `_ALU_OP`; 30-arm jump table (§3.1), identity head + permuted compares + scattered transcendentals |
| **m2d helpers** | `sub_124B990` = `generateTensorLoadSave<…MEM_2D_STRUCT>` (reg addr) · `sub_124B5B0` = `…<…PSEUDO_MEM_2D_STRUCT>` (imm/DRAM addr) |
| **m2d descriptor** | dtype `+0x0C` · num_elem[0/1] `+0x0E`/`+0x0F` · step_elem[0/1] `+0x18`/`+0x1C` · base reg `+0x20` · offset/hi reg `+0x21` |
| **IMM extras** | `var_id` u32 `+0x10` (`sub_124BD70`) · `var_offset` u64 `+0x14` (`sub_1250E50`) |
| **REG marker** | `0x80` at `+0x17` (reg-address marker, TensorLoad/Save REG mode) |
| **dtype helper** | `sub_120E650(dtype_enum)` → ISA dtype byte; int64 forced to enum `0x0F` |
| **CodeGenMode** | `*((int*)this+156)`: 1 = emit · 2 = ISA-check · 0 = collect-opcode · else FATAL |
| **Evidence** | full `objdump` disasm of the four encoder bodies + the two `m2d` template instantiations; `rl_*`/`m2d_*` pybind validator string table |

Every field row carries a confidence cell: CERTAIN means the exact store is disassembled, HIGH means a LUT or symbol cross-check backs it with one link not exhaustively walked (typically where the decompiler failed on the body and only the disasm shifts were read), MEDIUM means zero-init is implied or the byte is only seen being read back. No ordinal, offset, or field name is fabricated.

---

## 1. The shared 64-byte control band

Every SP register-lane bundle begins with the same ~12-byte control band, written by the opcode stamp plus the sync wait/update setup before the op-specific payload at `+0x0C`. The wait/update helpers come in two flavours — `<MEM_2D_STRUCT>` for the register-address path and `<PSEUDO_MEM_2D_STRUCT>` for the immediate/DRAM path — but they write the *same* header offsets.

```text
Shared control band (offsets 0..11)
  +0x00  opcode            written by Generator vtbl[+72] (0xA7/0xA8/0xAA/0xAB/0xCD/0xCE)
  +0x01  header/reserved   3 bytes, zero-init by bundle ctor (engine/qual)
  +0x04  sync_wait_mode    sub_120BE70(Wait::getMode)         [setupSyncWait]
  +0x05  events.wait_idx   sub_12173A0 "instr.events.wait_idx"
  +0x06  sync_update_mode  sub_120C430(Update::getMode)       [setupSyncUpdate]
  +0x07  events.update_idx sub_12173A0 "instr.events.update_idx"
  +0x08  sync_value/reg    dword. Wait: SEM_GE_IMM=value, SEM_GE_REG(=2)=regId.
                           Update: value, written only when mode != 3 && mode != 4.
```

| off | width | field | value / source | conf |
|---|---|---|---|---|
| `+0x00` | 1 | `opcode` | written by `Generator` vtbl `+0x48`/init | CERTAIN |
| `+0x01` | 3 | header/reserved | `0` (engine/qual band, zero-init) | MEDIUM |
| `+0x04` | 1 | `sync_wait_mode` | `sub_120BE70(Wait::getMode)` | CERTAIN |
| `+0x05` | 1 | `events.wait_idx` | `sub_12173A0` | CERTAIN |
| `+0x06` | 1 | `sync_update_mode` | `sub_120C430(Update::getMode)` | CERTAIN |
| `+0x07` | 1 | `events.update_idx` | `sub_12173A0` | CERTAIN |
| `+0x08` | 4 | `sync_value` / `sync_reg` | wait/update share this dword; reg id on `SEM_GE_REG` | CERTAIN |

> **NOTE — only two wait modes, two value-less update modes.** From `sub_121F280` (`setupSyncWait`, lines 67–90) and `sub_121EFC0` (`setupSyncUpdate`, lines 53–64): the wait path writes the `+0x08` dword for exactly two modes — `SEM_GE_IMM` (the dword is an immediate threshold) and `SEM_GE_REG`(=2) (the dword is a register id). The update path *skips* the `+0x08` write for modes `3` and `4` (semaphore-ref-only updates). The wait and update share the same dword slot.

Offsets `≥ 0x0C` are op-specific payload (§2–§6).

---

## 2. RegisterMove — opcode `0xA7`

> **Anchor:** `CoreV2GenImpl::visitInstRegisterMove` @ `0x12202e0` (Hex-Rays clean, 596 lines). BIR `bir::InstRegisterMove`: `dst = getOutput<RegisterAccess>(0)`; `src = argument[0]` (a `RegisterAccess` or an `ImmediateValue`). The dtype guard accepts only fp32/uint32/int32 (non-64-bit) or int64; `isInteger64Dtype()` selects the 64-bit layout (`v54` = dst dtype enum, forced to `15`/`0x0F` on the int64 path).

### 2.1 Field map

```text
RegisterMove 64-byte bundle (r15 in the GENERATE path) — opcode 0xA7
  +0x00  opcode 0xA7
  +0x04..+0x08  sync control band (§1)
  +0x0C  valid_num         1 = 32-bit move (1 reg) / 2 = 64-bit move (reg pair)
  +0x0D  dtype             sub_120E650(v54)   (fp32/u32/i32, or i64)
  +0x0E  src_mode          0 = src is register / 1 = src is immediate   ← the imm-vs-reg fork
  +0x10  src reg id        getRegId(src.getLocation(),0)   (only when src_mode==0)
  +0x18  dst reg id lo     getRegId(dst.getLocation(),0)
  +0x19  dst reg id hi     getRegId(dst.getLocation(),1)   (64-bit only)
  +0x20  immediate         src_mode==1: i32 (32-bit) or u64 (64-bit)
```

| off | width | field (`rl_mv_*`) | value / enum | grounding | conf |
|---|---|---|---|---|---|
| `+0x00` | 1 | `opcode` | `0xA7` | `movb $0xa7` @ `0x1220814` | CERTAIN |
| `+0x01` | 3 | `rl_mv_reserved_z` | `0` | zero-init | MEDIUM |
| `+0x0C` | 1 | `rl_mv_valid_num` | `1`=32-bit / `2`=64-bit | `movb …,0xc` @ `0x12205ad`/`0x1220770` | CERTAIN |
| `+0x0D` | 1 | dtype | `sub_120E650(v54)` | `mov %al,0xd(%r15)` @ `0x12205c0` | CERTAIN |
| `+0x0E` | 1 | `src_mode` (imm-vs-reg) | `0`=reg / `1`=imm | `movb …,0xe` @ `0x1220ad0`/`0x1220d98` | CERTAIN |
| `+0x10` | 1 | `rl_mv_valid_regi` (src) | `getRegId(src.loc,0)`; present iff `src_mode==0` | `mov %al,0x10(%r15)` @ `0x1220ae7` | CERTAIN |
| `+0x18` | 1 | dst reg id lo | `getRegId(dst.loc,0)` | `mov %al,0x18(%r15)` @ `0x12205d3` | CERTAIN |
| `+0x19` | 1 | dst reg id hi | `getRegId(dst.loc,1)` (64-bit) | `mov %al,0x19(%r15)` @ `0x12207af` | CERTAIN |
| `+0x20` | 4/8 | immediate | `src_mode==1`: i32 (32b) / u64 (64b) | `mov %eax/%rax,0x20(%r15)` @ `0x1220de5`/`0x1220da6` | CERTAIN |

### 2.2 The imm-vs-reg fork and the 64-bit constraint

`src_mode` at `+0x0E` is the whole story: `0` means the source is a physical register whose id lands at `+0x10`; `1` means the source is an immediate whose value lands in the `+0x20` dword (32-bit) or qword (64-bit). The two are mutually exclusive — `+0x10` is written only on the register path, `+0x20` only on the immediate path.

> **GOTCHA — a 64-bit register-to-register move is rejected.** The encoder asserts `!is64Bit` on the register-source path with `"64bit REG MOVE instr must have ImmediateValue src"`. A 64-bit move *must* be immediate-source (`valid_num==2`, `src_mode==1`, `+0x20` qword). The RUN_ISA_CHECKS path (`this+156==2`) confirms the same offsets into an on-stack `__m128i` pair (`v90`/`v91`/`v92`): src→BYTE0 of `v91` (=`+0x10`), dst→BYTE8/9 of `v91` (=`+0x18`/`+0x19`), imm→`v92` (=`+0x20`).

---

## 3. RegisterAlu — opcode `0xA8`

> **Anchor:** `CoreV2GenImpl::visitInstRegisterAlu` @ `0x12430e0 .. 0x1244ab0` (~`0x19d0` B). Hex-Rays returned no `cfunc` for this body; the field map below is objdump-grounded with the store-sites inline. BIR `bir::InstRegisterAlu`: up to two source operands (reg or imm) + a dst reg + the ALU-op selector. `dst ← src1 <op> src2`. The op selector is a `bir::AluOpType` carried at *struct* offset `InstRegisterAlu+0xF0` (BIR JSON key `"op"`, byte-proven in the simulator/dumper — see [1.12 §2](../arch/sp-engine.md)); the encoder reads it and runs it through the §3.1 wire LUT.

### 3.1 The ALU-op wire LUT — `sub_12039C0` (`_ALU_OP`)

The op byte at bundle `+0x0C` is **not** the BIR `AluOpType` ordinal — it is the *silicon ISA* opcode, produced by a 30-arm jump table (`sub_12039C0`, joined to the `_ALU_OP` table; call site `@0x1243404` → store `mov %al,0xc(%r12)` `@0x1243410`). The table identity-maps the arithmetic head `0..18`, then **permutes** the comparison block and **scatters** the transcendental tail, and `FATAL`s on `average`(24)/`elemwise_mul`(25) and the out-of-bound tail (30–32). This is the **same** table documented at [1.12 §2.1](../arch/sp-engine.md) — keep it consistent.

```c
// AluOpType -> wire byte, the 30-arm jump table behind RegisterAlu.
// bundle+0x0C receives this wire byte. arms 24,25 + the (30..32) tail FATAL.
static uint8_t aluop_to_wire(bir::AluOpType op) {
  switch (op) {                       // BIR name             wire byte
    case  0: return 0x00;             // bypass               0x00
    case  1: return 0x01;             // bitwise_not          0x01
    case  2: return 0x02;             // arith_shift_left     0x02
    case  3: return 0x03;             // arith_shift_right    0x03
    case  4: return 0x04;             // add                  0x04
    case  5: return 0x05;             // subtract             0x05
    case  6: return 0x06;             // mult                 0x06
    case  7: return 0x07;             // divide               0x07
    case  8: return 0x08;             // max                  0x08
    case  9: return 0x09;             // min                  0x09
    case 10: return 0x0a;             // bitwise_and          0x0a
    case 11: return 0x0b;             // bitwise_or           0x0b
    case 12: return 0x0c;             // bitwise_xor          0x0c
    case 13: return 0x0d;             // logical_and          0x0d
    case 14: return 0x0e;             // logical_or           0x0e
    case 15: return 0x0f;             // logical_xor          0x0f
    case 16: return 0x10;             // logical_shift_left   0x10
    case 17: return 0x11;             // logical_shift_right  0x11
    case 18: return 0x12;             // is_equal             0x12   <- identity ends here
    // --- the comparison block PERMUTES ---
    case 19: return 0x18;             // not_equal            0x18
    case 20: return 0x13;             // is_gt                0x13
    case 21: return 0x14;             // is_ge                0x14
    case 22: return 0x16;             // is_lt                0x16
    case 23: return 0x15;             // is_le                0x15
    case 24: fatal("Invalid enum variant for enum AluOpType");  // average      — REJECTED
    case 25: fatal("Invalid enum variant for enum AluOpType");  // elemwise_mul — REJECTED
    // --- the transcendental tail SCATTERS ---
    case 26: return 0x1a;             // pow                  0x1a
    case 27: return 0x1b;             // mod                  0x1b
    case 28: return 0x1d;             // rsqrt                0x1d
    case 29: return 0x19;             // abs                  0x19
    default: fatal("Invalid enum variant for enum AluOpType");  // 30..32 out of bound
  }
}
```

> **QUIRK — encode through the table, never raw.** The wire space is the silicon ISA opcode space; the BIR enum was extended over time and renumbered *around* the comparison/transcendental opcodes, which keep their original silicon bytes (`0x13`–`0x1d`). Feeding the BIR index straight to the wire produces wrong comparisons — `not_equal`(19) would emit `0x13` (`is_gt`) instead of `0x18`. The LUT below is cross-checked against the encoder's direct byte read and the enum ordering; the jump-table body itself was not re-derived byte for byte.

### 3.2 Field map

```text
RegisterAlu 64-byte bundle (r12 in the encoder) — opcode 0xA8
  +0x00  opcode 0xA8
  +0x04..+0x08  sync control band (§1)
  +0x0C  ALU-op wire byte    aluop_to_wire(inst+0xF0)      ← §3.1 table
  +0x0D  valid_dtyp/num      dtype0 (sub_120E650) / count   (RUN_ISA_CHECKS: src/elt dtype)
  +0x0E  operand_mode        0 = src operand is register / 1 = src operand is immediate
  +0x0F  valid_dtyp (dst)    dtype1 (sub_120E650)           (RUN_ISA_CHECKS path)
  +0x10  reg id A (dst lo)    getRegId(getLocation,0)
  +0x11  src reg id           getArgument<RegisterAccess>->getRegId
  +0x12  src reg id (op B)    dyn_cast<RegisterAccess>->getRegId
  +0x13  src reg id           getRegId
  +0x14  dst reg-lo / +0x15 dst reg-hi   getOutput<RegisterAccess>->getRegId(0/1)
  +0x18  immediate            operand_mode==1: i32 / i64
```

| off | width | field (`rl_al_*`) | value / source | grounding | conf |
|---|---|---|---|---|---|
| `+0x00` | 1 | `opcode` | `0xA8` | `movb $0xa8` @ `0x1243253` | CERTAIN |
| `+0x0C` | 1 | `rl_al_op` (ALU op) | `sub_12039C0(op)` = `_ALU_OP` wire byte | `mov %al,0xc(%r12)` @ `0x1243410` | CERTAIN |
| `+0x0D` | 1 | `rl_al_valid_dtyp`/num | dtype0 (`sub_120E650`) or count=1 | `mov %al,0xd(%r12)` @ `0x1243ec8`; `movb $1,0xd` @ `0x1243570` | HIGH |
| `+0x0E` | 1 | `operand_mode` (imm-vs-reg) | `0`=reg / `1`=imm | `movb $0,0xe` @ `0x12435b9`; `movb $1,0xe` @ `0x1244154` | CERTAIN |
| `+0x0F` | 1 | `rl_al_valid_dtyp` (dst) | dtype1 (`sub_120E650`) | `mov %al,0xf(%r12)` @ `0x1243edf` | HIGH |
| `+0x10` | 1 | reg id A (dst lo / src0) | `getRegId(getLocation,0)` | `mov %al,0x10(%r12)` @ `0x124342b` | CERTAIN |
| `+0x11` | 1 | src reg id | `getArgument<RegAcc>->getRegId` | `mov %al,0x11(%r12)` @ `0x1243598` | CERTAIN |
| `+0x12` | 1 | src reg id (operand B) | `dyn_cast<RegAcc>->getRegId` | `mov %al,0x12(%r12)` @ `0x12435e0` | CERTAIN |
| `+0x13` | 1 | src reg id | `getRegId` | `mov %al,0x13(%r12)` @ `0x124368e` | CERTAIN |
| `+0x14` | 1 | dst reg-lo | `getLocation->getRegId(0)` | `mov %al,0x14(%r12)` @ `0x12436d8` | CERTAIN |
| `+0x15` | 1 | dst reg id (hi) | `getOutput<RegAcc>->getRegId` | `mov %al,0x15(%r12)` @ `0x12436b6` | CERTAIN |
| `+0x18` | 4/8 | immediate | `operand_mode==1`: i32 / i64 | `mov %eax/%rax,0x18(%r12)` @ `0x12441bb`/`0x124415f` | CERTAIN |

> **GOTCHA — the `movsbl 0x43(%r12)` @ `0x12446c8` is not a wire field.** Offset `0x43` is 67 bytes, past the end of the 64-byte (`0x00..0x3F`) bundle this op emits, so it cannot be a `RegisterAlu` wire byte. `r12` is the bundle base, but `+0x43` lands in the enclosing encoder/scratch object — most plausibly a flag the ISA-check arm reads back. No `rl_al_reserved*` string exists in the binary to anchor a field name there either. Whether a genuine reserved or guard byte exists inside `+0x00..+0x3F` is an open gap.

> **NOTE — the register-slot roles are only partly resolvable from this encoder.** The op byte (`+0x0C`), the operand-mode bit (`+0x0E`), and the immediate slot (`+0x18`) are pinned. The exact *role* of each register byte in `+0x10..+0x15` (which is `dst` vs `src0` vs `src1`) is partly ambiguous from the disasm alone: the `getOutput<>` reads feed `+0x0F`/`+0x15` (dst dtype + dst hi) and the `getArgument<>` reads feed `+0x11`/`+0x13` (srcs). The cross-checked sim/dumper view in [1.12 §2.2](../arch/sp-engine.md) resolves this as `dst@+0x10`, `src1@+0x11`, `src2@+0x12`, 3rd operand `+0x13`, with `+0x14`/`+0x15` the 64-bit dst reg-pair (lo/hi). The bundle-offset family (`+0x0C`/`+0x0E`/`+0x10`…) is the *wire* offset; the BIR `op` key at `InstRegisterAlu+0xF0` is the *struct* offset — do not confuse the two.

> **GOTCHA — accumulate/reduce bits not enumerated.** The BIR keys `is_64bit`/`dge_type`/`reduce_cmd`/`acc` exist on `InstRegisterAlu`, and the bundle carries the `+0x0D`/`+0x0E` mode bytes, but the full mapping of `acc`/`reduce_cmd` to bundle bytes beyond the 32-bit non-accumulate path is [INFERRED]. The 64-bit op pairs two consecutive physical registers (`+0x14` lo, `+0x15` hi).

---

## 4. TensorLoad / TensorSave — opcode & mode dispatch

> **Anchors:** `visitInstTensorLoad` @ `0x1251890` (Hex-Rays failed → disasm); `visitInstTensorSave` @ `0x1252580` (Hex-Rays clean, 379 lines). BIR `bir::InstTensorLoad` / `bir::InstTensorSave`. For `TensorSave`: `arg0` = `RegisterAccess` (the register operand), `out0` = `AccessPattern` (the memory operand), `out1` (`RegisterAccessPattern`) carries the 64-bit pseudo offset register. `TensorLoad` is symmetric: `out0` = dst `RegisterAccess`, the memory AP is the arg/out operand.

The IMM-vs-REG distinction here is **not** a `+0x0E` byte — it is a fork on the memory access-pattern *kind*, selecting both the opcode and the `m2d` helper template:

```c
// IMM/REG selector — visitInstTensorLoad/Save, @0x125196d.
int kind = *(int*)(memAP + 24);          // AccessPattern kind at memAP+0x18
if (kind == 1) {                         // PhysicalAP  -> IMM / DRAM-pseudo path
    opcode = is_save ? 0xCD : 0xCE;
    emit_m2d = sub_124B5B0;              // generateTensorLoadSave<...PSEUDO_MEM_2D_STRUCT>
    // + inject var_id (sub_124BD70) and var_offset (sub_1250E50)
} else if (kind == 3) {                  // RegisterAP  -> REGISTER address path
    opcode = is_save ? 0xAB : 0xAA;
    emit_m2d = sub_124B990;              // generateTensorLoadSave<...MEM_2D_STRUCT>
    // + set 0x80 reg-address marker at bundle+0x17
} else {
    fatal("codegen stage can only have PhysicalAP or RegisterAP");  // SymbolicAP rejected
}
```

| inst | mode | AP kind | opcode | m2d helper | extra fields |
|---|---|---|---|---|---|
| `TensorLoad` | IMM (DRAM) | `1` PhysicalAP | `0xCE` (206) | `sub_124B5B0` (`PSEUDO_MEM_2D`) | `var_id` `+0x10`, `var_offset` `+0x14` |
| `TensorLoad` | REGISTER | `3` RegisterAP | `0xAA` (170) | `sub_124B990` (`MEM_2D`) | `0x80` marker `+0x17`; offset reg pair `+0x10`/`+0x11` |
| `TensorSave` | IMM (DRAM) | `1` PhysicalAP | `0xCD` (205) | `sub_124B5B0` (`PSEUDO_MEM_2D`) | `var_id` `+0x10`, `var_offset` `+0x14` |
| `TensorSave` | REGISTER | `3` RegisterAP | `0xAB` (171) | `sub_124B990` (`MEM_2D`) | `0x80` marker `+0x17` |

> **GOTCHA — the IMM opcode is the *larger* of each pair.** `TensorLoad` is `0xCE` for IMM and `0xAA` for REG; `TensorSave` is `0xCD` for IMM and `0xAB` for REG. Each pairing is fixed by which descriptor builder follows the opcode store: `TensorLoad` `movb $0xce` @ `0x1251bb9` → `sub_124B5B0(PSEUDO)` @ `0x1251c26`, versus `movb $0xaa` @ `0x12521b5` → `sub_124B990(MEM_2D)` @ `0x1252295`; `TensorSave` `movb $0xcd` @ `0x12530a4` → `sub_124B5B0(PSEUDO)` @ `0x12530da`, versus `movb $0xab` @ `0x1252a5c` → `sub_124B990(MEM_2D)` @ `0x1252a93`.

> **GOTCHA — TensorSave store restrictions.** `TensorSave` `FATAL`s on a register store to **PSUM**, and on an **SB** store when module-version == 20 (`!out.isLocationSB()`). `TensorLoad` has no PSUM/SB restriction — it loads into a register lane.

---

## 5. The embedded MEM_PATTERN2D (`m2d`) descriptor

Both `m2d` template instantiations write an **identical** field layout (only the opcode and the IMM-path `var_id`/`var_offset` differ, set by the caller). The descriptor base pointer (`a1`) equals the bundle base, so the offsets below are absolute bundle offsets (verified: `%r15` REG path, `%r14` IMM path; byte-identical bodies, `sub_124B5B0`/`sub_124B990` lines 87–138).

```text
MEM_PATTERN2D descriptor (m2d_*), absolute bundle offsets
  +0x0C  dtype                sub_120E650(inst.dtype);  int64 path -> sub_120E650(0xF)
  +0x0D  reserved/flag        = 0
  +0x0E  num_elem[0]          sub_1247BF0 "instr.num_elem[0]"   (int64: 2 * num_elem[0])
  +0x0F  num_elem[1]          sub_1247BF0 "instr.num_elem[1]"
  +0x18  step_elem[0]         sub_124A9D0 "instr.step_elem[0]"
  +0x1C  step_elem[1]         sub_124A9D0 "instr.step_elem[1]"
  +0x20  base/address reg id  getRegId(memAP.getLocation(),0)
  +0x21  offset/hi reg id     int64: getRegId(memAP.loc,1)  else if a4: getRegId(a4.loc,0)
```

| off | width | field (`m2d_*`) | value / source | conf |
|---|---|---|---|---|
| `+0x0C` | 1 | `m2d_dtype_check` (dtype) | `sub_120E650(dtype)`; int64 → `0x0F` | CERTAIN |
| `+0x0D` | 1 | reserved/flag | `0` | CERTAIN |
| `+0x0E` | 1 | `m2d_num_elem` / num_elem[0] | `sub_1247BF0`; int64 doubles to `2*num_elem[0]` | CERTAIN |
| `+0x0F` | 1 | num_elem[1] | `sub_1247BF0` | CERTAIN |
| `+0x18` | 4 | step_elem[0] | `sub_124A9D0` | CERTAIN |
| `+0x1C` | 4 | step_elem[1] | `sub_124A9D0` | CERTAIN |
| `+0x20` | 1 | base/address reg id (`m2d_u64_register`) | `getRegId(mem.loc,0)` | CERTAIN |
| `+0x21` | 1 | offset/hi reg id | int64: `getRegId(mem.loc,1)` / else `getRegId(a4.loc,0)` | CERTAIN |

The element-count source is the instruction's access-pattern pair list at `inst+0x50`, indexed by the count at `inst+0x58`: `num_elem` comes from `APPair.second`, `step_elem` from `APPair.first`. A 64-bit dtype (`isInteger64Dtype`) doubles `num_elem[0]` and uses the register pair (lo,hi) at `+0x20`/`+0x21`.

> **NOTE — m2d is the same descriptor as [2.5](mempattern-2d-3d.md).** This `MEM_PATTERN2D` is the DST-role 12-byte descriptor of [2.5](mempattern-2d-3d.md): an `ADDR4`-style start word plus per-dimension strides and element counts. Here it is embedded directly into the SP load/save bundle at `+0x0C` rather than carried in a compute instruction's `INST_UNION`; the dtype/num/step fields and the register-base addressing are the SP-specific framing. The pybind validators (`m2d_dtype_check`, `m2d_num_elem_check`, `m2d_store_num_elem_check`, `m2d_32_elem_check`, `m2d_datasrc_valid`/`m2d_datasrc_zero`, `m2d_u64_register`/`*_start_addr`) run on this descriptor in the RUN_ISA_CHECKS path.

---

## 6. Absolute 64-byte field maps — TensorSave / TensorLoad

The opcode and the IMM-path `var_id`/`var_offset` are the only deltas on top of the shared §5 descriptor.

### 6.1 TensorSave, IMM mode (PhysicalAP / DRAM-pseudo) — opcode `0xCD`

> Helpers: `setupSyncWait/Update<PSEUDO_MEM_2D>` (`0x121F8E0`/`0x121F620`), `generateTensorLoadSave<PSEUDO_MEM_2D>` (`0x124B5B0`), `var_id` (`0x124BD70`), `var_offset` (`0x1250E50`).

| off | width | field | value / source | grounding | conf |
|---|---|---|---|---|---|
| `+0x00` | 1 | `opcode` | `0xCD` | `movb $0xcd` @ `0x12530a4` | CERTAIN |
| `+0x04`..`+0x08` | — | sync band | §1 (pseudo setup) | — | CERTAIN |
| `+0x0C` | 1 | dtype | `sub_120E650` | §5 | CERTAIN |
| `+0x0E`/`+0x0F` | 1 | num_elem[0/1] | `sub_1247BF0` | §5 | CERTAIN |
| `+0x10` | 4 | `instr.var_id` | `sub_124BD70` @ `lea 0x10(%r14)` | `@0x1252fa4`/`0x1252fb8` | CERTAIN |
| `+0x14` | 4/8 | `instr.var_offset` | `sub_1250E50` @ `lea 0x14(%r14)` (DRAM var-relative offset) | `@0x1252fe6`/`0x1252ffb` | CERTAIN |
| `+0x18`/`+0x1C` | 4 | step_elem[0/1] | `sub_124A9D0` | §5 | CERTAIN |
| `+0x20`/`+0x21` | 1 | m2d base/offset reg id | `getRegId(mem.loc,0/1)` | §5 | CERTAIN |

### 6.2 TensorSave, REG mode (RegisterAP) — opcode `0xAB`

> Helpers: `setupSyncWait/Update<MEM_2D>` (`0x121F280`/`0x121EFC0`), `generateTensorLoadSave<MEM_2D>` (`0x124B990`).

| off | width | field | value / source | grounding | conf |
|---|---|---|---|---|---|
| `+0x00` | 1 | `opcode` | `0xAB` | `movb $0xab` @ `0x1252a5c` | CERTAIN |
| `+0x04`..`+0x08` | — | sync band | §1 | — | CERTAIN |
| `+0x0C` | 1 | dtype | `sub_120E650` | §5 | CERTAIN |
| `+0x0D` | 1 | reserved `0`; `+0x0E`/`+0x0F` num_elem | §5 | — | CERTAIN |
| `+0x10` | 1 | offset register id (lo) | `getRegId(out0.reg,?)` | `mov %al,0x10` | CERTAIN |
| `+0x11` | 1 | offset register id (hi) | `getRegId(out1.reg,?)` | `mov %al,0x11` | CERTAIN |
| `+0x17` | 1 | reg-address marker | `0x80` | `movb $0x80,0x17` @ `0x1252bb1` | CERTAIN |
| `+0x18`/`+0x1C` | 4 | step_elem[0/1] | `sub_124A9D0` | §5 | CERTAIN |
| `+0x20`/`+0x21` | 1 | m2d base/offset reg id | `getRegId` | §5 | CERTAIN |

### 6.3 TensorLoad — symmetric (dst RegisterAccess)

`TensorLoad` mirrors `TensorSave` with a destination register instead of a source:

- **IMM mode (PhysicalAP/DRAM)** — opcode `0xCE`: same layout; `m2d@+0x0C…`; `var_id@+0x10` (`sub_124BD70`), `var_offset@+0x14` (`sub_1250E50`). *Anchors: `0x1251bb9 movb $0xce`; `0x1251c26 sub_124B5B0`; `0x1251c59` var_id; `0x1251c9b` var_offset.*
- **REG mode (RegisterAP)** — opcode `0xAA`: same layout; `m2d@+0x0C…`; offset reg pair `@+0x10`/`+0x11`; `0x80` marker `@+0x17`. *Anchors: `0x12521b5 movb $0xaa`; `0x1252295 sub_124B990`; the `0x80` at `+0x17` set @ `0x1251f89`; offset reg `getRegId` @ `0x1251fa2` / `0x1251fc8` → `+0x10`/`+0x11`.*

`TensorLoad` has no PSUM/SB restriction (it loads into a register lane).

---

## 7. Encoder helper inventory

| helper | role |
|---|---|
| `sub_12039C0` | `_ALU_OP` enum-ordinal table — RegisterAlu op wire byte at `+0x0C` (§3.1) |
| `sub_120E650` | dtype enum → ISA dtype code (RegisterMove `+0x0D`, RegisterAlu `+0x0D`/`+0x0F`, m2d `+0x0C`); int64 forced to `0x0F` |
| `sub_120BE70` | wait-mode → code (header `+0x04`); modes `SEM_GE_IMM`, `SEM_GE_REG`(=2) |
| `sub_120C430` | update-mode → code (header `+0x06`); modes `3`,`4` = sem-ref-only (no value dword) |
| `sub_12173A0` | event-idx setter (`instr.events.{wait,update}_idx`) → header `+0x05`/`+0x07` |
| `sub_124B990` / `sub_124B5B0` | `generateTensorLoadSave<MEM_2D>` (reg addr) / `<PSEUDO_MEM_2D>` (imm/DRAM addr) |
| `sub_124BD70` / `sub_1250E50` | IMM-path `instr.var_id` (u32 `+0x10`) / `instr.var_offset` (u64 `+0x14`) setters |
| `sub_1247BF0` / `sub_124A9D0` | `instr.num_elem[i]` / `instr.step_elem[i]` setters (m2d `+0x0E`/`+0x0F`, `+0x18`/`+0x1C`) |
| `sub_12095A0` | bundle finalize / append (writes to `Generator+64`) |
| `bir::isInteger64Dtype` | selects the 64-bit layout (reg-pair / doubled num_elem / imm64) |

---

## 8. Confidence summary

**Pinned by an exact disassembled store:** all four opcodes (`0xA7`/`0xA8`/and the four TensorLoad/Save opcodes); the RegisterMove field map in full (`valid_num`, `src_mode`, src/dst reg ids, imm); the RegisterAlu op byte (`+0x0C`), operand-mode bit (`+0x0E`), immediate slot (`+0x18`), and the individual register store-sites; the TensorLoad/Save IMM-vs-REG dispatch on AP kind (`cmpl $0x1, 0x18(memAP)` @ `0x125196d`); the full `m2d` descriptor layout; the IMM-mode `var_id`/`var_offset` injection; the `0x80` reg-address marker at `+0x17`; the sync-band offsets.

**Cross-checked against a LUT or symbol, with the body not byte-re-derived here:** the 30-arm `_ALU_OP` wire LUT (§3.1, consistent with [1.12](../arch/sp-engine.md)), and the RegisterAlu dtype bytes at `+0x0D`/`+0x0F`.

**[INFERRED]:** the `+0x01..+0x03` reserved header bytes, from zero-init; the RegisterAlu register-slot *roles* in `+0x10..+0x15`, resolved against the sim/dumper view in [1.12 §2.2](../arch/sp-engine.md); and the `acc`/`reduce_cmd` accumulate bundle bytes beyond the 32-bit non-accumulate path.

---

## Cross-References

- [1.12 — The SP Engine](../arch/sp-engine.md) — the functional/architectural model of the SP register file, the same 30-arm `RegisterAlu` ALU-op table, and loop pre-lowering into counter-ALU + branch pairs.
- [2.5 — MEM_PATTERN2D / 3D](mempattern-2d-3d.md) — the DST-role descriptor embedded by TensorLoad/Save (`m2d`) and the 4+4N geometry.
- [2.20 — SP Sync / Branch / Control Encoding](sp-sync-encoding.md) — the sibling SP control band sharing the same 64-byte skeleton (semaphore wait/update, branch, barrier, call/return/exit).
- [Build & Version Provenance](../reference/versions.md) — the pinned build, the cp310/11/12 parity argument, and the per-binary GNU build-ids.
- BIR Part 7 (codegen control-register I17) — the codegen-side `CodeGenMode` dispatch and the control register that selects emit vs ISA-check vs collect-opcode.
