# Access-Pattern Encoder Dispatch

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; the cp311/cp312 wheels rebuild `libwalrus.so`, so confirm any address against the target wheel — see [Build & Version Provenance](../reference/versions.md)). Every symbol and offset is read from `neuronxcc/starfish/lib/libwalrus.so` (cp310 GNU build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`; libBIR build-id `a9b1ea38…`). For `.text`/`.rodata`, virtual address equals file offset; `.data` carries a `+0x400000` delta and is not touched here. Treat every address as version-pinned.*

## Abstract

The descriptor pages [2.3](tensor-descriptors.md)–2.7 each describe *one* wire struct — `TENSOR1D/2D/3D/4D`, `MEM_PATTERN2D/3D/4D`, `MXMEM_PATTERN1D`, the indirect 16-byte forms. This page describes the **encoder that picks among them**: given a `bir::AccessPattern` for one operand slot of one Tonga instruction, which descriptor template does the backend instantiate, and what fills it? That is the access-pattern encoder *dispatch* — the bridge between the BIR-level `codegenAccess` (Part 7) and the per-format wire validators (Part 8).

The central finding inverts the obvious mental model. A reader who has built an LLVM `SelectionDAG` selector expects a runtime switch — *"the access pattern has 3 active dimensions, therefore emit `TENSOR3D`."* **No such switch exists.** The descriptor *type* is chosen **statically, per operand role**, by the op's own `generate*`/`visitInst*` codegen function: the function names the C++ template type (`assignAccess<NEURON_ISA_TPB_TENSOR3D>`) for *each* slot at compile time, baked into the encoder. The access pattern's dimension count (`Pattern.size` at `AP+0x58`) drives **only** the unit-fill loop *inside* that fixed template — every unused free dim of the chosen `N`-D template is written `{step=1, num=1}` (`0x00010001`). Selection is structural, not data-driven.

The dispatch is a cartesian product of **three orthogonal axes**, resolved at codegen: **AXIS-A** (operand role → descriptor template, the static per-role table), **AXIS-B** (`AP.kind` at `+0x18` → base-address mode: Physical / Register / reject-Symbolic), and **AXIS-C** (`isTensorIndirectDynamicAP()` / MX-op → self-route into the gather or MX sub-arm *inside the chosen leaf*). The page walks each axis, gives the full decision tree as annotated pseudocode, and closes by tying each emitted descriptor type to its matching L2 wire validator reached through the per-format `is_valid_<fmt>` gate.

For reimplementation the contract is:

- **AXIS-A** — the per-role template table: which op/slot names which `assignAccess<T>` template (matmul → `TENSOR3D`×3, copy → `TENSOR4D`×2, MX → `MXMEM_PATTERN1D`+`MEM_PATTERN3D`), and the proof that this is a compile-time instantiation, not a runtime dim-count selector.
- **The funnel** — `Generator::assignAccess<T>` wrapper guards (the indirect-field reject, `[1,4]` rank, SB/PSUM space, "`N` bytes to encode") and the vtable-leaf tail-dispatch.
- **AXIS-C** — the indirect/MX self-route: the `isTensorIndirectDynamicAP()` (vtbl `+0x80`) predicate that promotes a static `TENSOR3D` slot to `INDIRECT16B` in place, and the MX leaf that tail-calls `assignIndirectPatternForMX<MXINDIRECT16B>`.
- **AXIS-B** — the single 3-way `assignStartAddr<ADDR4>` kind dispatch (the only `ADDR4` base writer per arch) and the `a4` bool that selects the data-vs-scale address resolver.
- **The validation pairing** — each descriptor type → its `*_valid` leaf, reached via `is_valid_<fmt>`.

## At a glance

| | |
|---|---|
| **Type router (dormant)** | `CoreV2GenImpl::assignAccessToType(string, AP&)` @ `0x1237a50` — string→type, **self-PLT xref only** (§6) |
| **Operand funnel** | `Generator::assignAccess<T>` — `TENSOR3D-v4` @ `0x150a450`, `TENSOR4D-v2` @ `0x133e710`, `MEM_PATTERN3D` @ `0x15092b0`, `MEM_PATTERN2D` @ `0x1509b80` |
| **Arch leaves** | `CoreV4GenImpl::assignAccess{1,2,3,4}D` @ `0x150b690`/`0x150c120`/`0x150ccf0`/`0x150d8e0` — **`3D` @ `0x150ccf0` = inline static↔INDIRECT16B router** |
| **Static packer** | `Generator::assignStaticPattern<core_v4::TENSOR3D>` @ `0x150c390` (unit-fill `0x10001`); siblings `…TENSOR{1,2,4}D` @ `0x150ad20`/`0x150b7e0`/`0x150cf60` |
| **Base writer (AXIS-B)** | `Generator::assignStartAddr<core_v4::ADDR4>` @ `0x1508df0` (byte-identical to v2 @ `0x1172e10`) — the *only* `ADDR4` writer |
| **MX leaves** | `assignAccessForMX<MXMEM_PATTERN1D>` @ `0x150e2f0` → `assignIndirectPatternForMX<MXINDIRECT16B>` @ `0x150de90` |
| **Indirect predicate** | `bir::*AccessPattern::isTensorIndirectDynamicAP()` — libBIR extern, **vtbl slot `+0x80`** |
| **Op encoders (witnessed)** | matmul `generateMatMul` @ `0x1248650` ; `visitInstTensorCopy` @ `0x1237f50` ; `generateMatmultMx` @ `0x143ebd0` |
| **Validators** | `mxmem1d_valid` @ `0x1447190` · `tensor_start_addr_valid` @ `0x127f590` · `mm_dst_mem3d_valid_nc_v4` @ `0x1447fe0` · `mem{2,3,4}d_valid` @ `0x14468c0`/`0x14467d0`/`0x1446550` |
| **Validation gate** | `is_valid_smx1d3_mm` @ `0x1501ac0` ; reached via `runSingleISACheck` @ `0x1435010` → `is_valid_neuron_engine_instruction` @ `0x1502120` |
| **Evidence** | full `objdump -M intel` disasm of the v4 funnel/leaf/packer/base-writer bodies + `nm -DC` symtab |

---

## 1. The three-axis decision tree

The descriptor selected for a single operand slot is the resolution of three independent decisions. They are independent in the sense that any combination is reachable — an `MX` slot can be static or indirect; a `TENSOR3D` slot can be static or indirect; the base address can be physical or register in any of those. The tree is therefore a product, not a switch.

```text
AXIS-A  OPERAND ROLE (which slot of which op)  → picks the descriptor TEMPLATE   (compile-time, §2)
AXIS-B  AP.kind @ +0x18 (Physical/Register/Sym) → picks the BASE-ADDR mode        (runtime, §4)
AXIS-C  isTensorIndirectDynamicAP() / MX-op     → SELF-ROUTES to gather / MX sub-arm (runtime, §3)
```

The whole tree, in one picture:

```text
op encoder names descriptor TYPE per operand role        ── AXIS-A (compile-time template instantiation)
   │
   ├─ plain TENSOR/MEM_PATTERN slot ──► assignAccess<T> wrapper (guards, §2)
   │       └─ vtbl-leaf ──► [ isTensorIndirectDynamicAP()? ]      ── AXIS-C
   │                          │ no                       │ yes
   │                          ▼                          ▼
   │              assignStaticPattern<T>          INDIRECT16B inline
   │                  (4+4N pack, unit-fill)      (indices@+0, data@+4, K@+8, +3|=0x20)
   │                          │
   │                          ▼
   │              assignStartAddr<ADDR4> ── kind @ +0x18 ──┐      ── AXIS-B
   │                  ==1 Physical │ ==3 Register │ else (2/0) → REJECT "no SymbolicAP"
   │                  static u32   │ reg-mode     │
   │                  (a4: 0=DATA/1=SCALE addr resolver)
   │
   └─ MX slot ──► assignAccessForMX<MXMEM_PATTERN1D>
                     └─ [ isTensorIndirectDynamicAP(data)? ]      ── AXIS-C
                          │ no                          │ yes
                          ▼                             ▼
                  static MX pair                assignIndirectPatternForMX<MXINDIRECT16B>
                  (data a4=0 @+0,               (indices + data + scale gather)
                   scale a4=1 @+4)
```

> **QUIRK — there is no `getMemPattern`-style runtime "if dims==3 emit `TENSOR3D`" selector.** The type is a compile-time template instantiation chosen by operand role (AXIS-A). The access pattern's `Pattern.size` only feeds the unit-fill loop bound inside the chosen template (§3). Any earlier note implying a runtime dim-count switch is corrected below (§9).

---

## 2. AXIS-A — the per-role template table

### Purpose

AXIS-A is the *type* decision. It is invisible at runtime because it has already happened: when the op's codegen function (`generateMatMul`, `visitInstTensorCopy`, `generateMatmultMx`, …) was compiled, the C++ source named `assignAccess<NEURON_ISA_TPB_TENSOR3D>(...)` for slot 0, `assignAccess<…TENSOR3D>(...)` for slot 1, and so on. The instantiated template body for each type is a distinct function in the binary; the call site selects it. There is no dispatch object, no table lookup, no dim-count test — the descriptor type is structurally bound to (op, operand-slot).

### The witnessed instantiations

Each `generate*`/`visit*` body names the literal template type for each operand slot. Each row below is read from the decompiled body plus the `objdump` PLT-call arguments:

| Op / Role | Descriptor type | Encoder evidence | Confidence |
|---|---|---|---|
| Matmul (CoreV2) src Ifmap | `TENSOR3D` | `generateMatMul` @ `0x1248650`, line 1023 | CERTAIN |
| Matmul (CoreV2) moving / weights | `TENSOR3D` | `generateMatMul`, line 1025 | CERTAIN |
| Matmul (CoreV2) dst (perf arm) | `TENSOR3D` | `generateMatMul`, lines 473/475 | CERTAIN |
| TensorCopy (CoreV2) src + dst | `TENSOR4D` ×2 | `visitInstTensorCopy` @ `0x1237f50`, lines 122/123 | CERTAIN |
| Matmul-MX (CoreV4) data + scale pair | `MXMEM_PATTERN1D` | `generateMatmultMx` @ `0x143ebd0`, lines 148/242 | CERTAIN |
| Matmul-MX (CoreV4) moving operand | `MEM_PATTERN3D` | `generateMatmultMx`, lines 149/243 | CERTAIN |
| Indirect DMA load/save (CoreV2) | `ADDR8` (DMA twin) | `generateIndirectLoadSave` @ `0x1268c00` | CERTAIN |
| (generic) on-engine dst → SBUF/PSUM | `MEM_PATTERN2D/3D` | N07 dst variants @ `0x1509b80`/`0x15092b0` | CERTAIN |
| act / pool / dve / reduce | per-role template | not exhaustively swept; read by homology | HIGH |

The matmul `TENSOR3D` calls were cross-checked in `objdump`: the PLT calls land at `0x1248f33`/`0x1248f4b`, and the dst call passes `a4 = xor ecx,ecx = 0` — i.e. the dst slot is *not* a scale slot.

### The TENSOR-vs-MEM_PATTERN distinction is naming, not math

`TENSOR3D` and `MEM_PATTERN3D` are the *same field math* — the 4+4N rule of [2.3](tensor-descriptors.md) — under two different wire-struct names with two different role validators: `TENSOR` is the **src / engine-input** variant; `MEM_PATTERN` is the **dst / PSUM-write** variant (see [2.5](mempattern-2d-3d.md)). They are chosen by which template the op names for that slot, never recomputed at runtime. The proof is structural:

> **NOTE — `TENSOR3D` and `MEM_PATTERN3D` share their packer leaf.** `assignAccess<core_v4::TENSOR3D>` @ `0x150a450` and `assignAccess<core_v4::MEM_PATTERN3D>` @ `0x15092b0` *both* tail-call the same arch vtable slot (`a1 + 0x48`, the `CoreV4GenImpl::assignAccess3D` leaf @ `0x150ccf0`). The two wrappers differ only in their compile-time guards (§2.1) and the "16 bytes to encode" assert wording. The bytes they emit are identical — both bodies end `(*(…)(a1 + 0x48LL))(a1,a2,a3,a4)`.

### Slot width ↔ type

The `_Znwm` (operator `new`) allocation sizes inside the dormant `assignAccessToType` (§6) and the "must have `N` bytes to encode" asserts pin the width of each type — exactly the 4+4N rule plus the MX/indirect packed forms:

| Type | Slot width | Layout note | Source |
|---|---|---|---|
| `TENSOR1D` | 8 B | `4 + 4·1` | `0x8` alloc in `assignAccessToType` |
| `TENSOR2D` | 12 B | `4 + 4·2` | `0xC` alloc |
| `TENSOR3D` | 16 B | `4 + 4·3` | `0x10` alloc |
| `TENSOR4D` | 20 B | `4 + 4·4` | `0x14` alloc |
| `MXMEM_PATTERN1D` | 16 B | data `ADDR4` @+0 / scale `ADDR4` @+4 / `K` @+8 / step-dir @+0xA / base-part @+0xB | [2.6](mxmem-pattern1d.md), J26 |
| `INDIRECT16B` / `MXINDIRECT16B` | 16 B | indices @+0 / data @+4 [/ scale @+8] / `+3` indirect bit | §3 |

*Anchors: the `mov edi,0x8` / `0xc` / `0x10` / `0x14` immediates bracket the four `std::string::compare` calls in `assignAccessToType` (`0x1237b97`, `0x1237c29`, … `0x1237cc4`), each immediately preceding a `_Znwm@plt`.*

### 2.1 The operand-encoder funnel — `assignAccess<T>` guards

Every plain on-engine operand reaches its leaf through `Generator::assignAccess<T>` (T = `TENSOR{1..4}D` or `MEM_PATTERN{2,3}D`). The wrapper runs four compile-time-uniform guards, then tail-calls the arch vtable leaf. All four guards appear across the four wrapper bodies as verbatim rodata strings:

```c
function assignAccess_T(slot, AP, a4):          // e.g. <core_v4 TENSOR3D> @0x150a450
    // (G-a) INDIRECT-FIELD REJECT — TENSOR types ONLY:
    if AP.has_indirect_field() && is_TENSOR_type:
        reportError("ISA mem pattern is static tensor pattern but has indirect field")
        // ⭐ ASYMMETRY: MEM_PATTERN2D/3D wrappers DO NOT carry this reject (string absent).
    // (G-b) RANK RANGE:
    if !(1 <= AP.Pattern.size <= 4):            // Pattern.size @ AP+0x58
        reportError("..., which must be in range of [1, 4]")
    // (G-c) MEMORY SPACE — must be SBUF or PSUM (DRAM rides the DMA ADDR8 path, not here):
    if !(isLocationSB(AP) || isLocationPSUM(AP)):
        NeuronAssertion("BIRAP.isLocationSB() || BIRAP.isLocationPSUM()")     // ec1008
    // (G-d) SLOT-WIDTH assert (rodata, shared with the SRC twin):
    assert("ISA mem pattern {3D} must have {16} bytes to encode")
    // tail to the arch leaf, slot picked by the descriptor TYPE:
    return (*(this->vtbl + 0x48))(this, slot, AP, a4)        // TENSOR3D & MEM_PATTERN3D → +0x48
```

> **GOTCHA — the indirect-field reject is asymmetric.** The `TENSOR3D`/`TENSOR4D` wrappers reject an access pattern that carries tensor-indirect metadata ("static tensor pattern but has indirect field"); the `MEM_PATTERN2D`/`MEM_PATTERN3D` wrappers do **not** (the string is absent from their bodies). A reimplementer who copies the guard set uniformly will wrongly reject a legal indirect dst. The dst path tolerates the indirect field because the dst of a gather is a normal contiguous write. The string is present in `TENSOR3D`@49 and `TENSOR4D`@50, absent in `MEM_PATTERN3D`.

The tail dispatch picks the vtable slot by descriptor type, read off the `(*(a1 + 0xNN))(a1,a2,a3,a4)` tails:

| Wrapper | vtbl slot | Arch leaf |
|---|---|---|
| `TENSOR4D` | `+0x38` (slot 7) | `CoreV2GenImpl::assignAccess4D` @ `0x1177620` |
| `TENSOR3D-v4` | `+0x48` (slot 9) | `CoreV4GenImpl::assignAccess3D` @ `0x150ccf0` |
| `MEM_PATTERN3D` | `+0x48` (slot 9) | **same leaf** as `TENSOR3D-v4` |
| `MEM_PATTERN2D` | `+0x40` (slot 8) | `CoreV4GenImpl::assignAccess2D` @ `0x150c120` |

On CoreV2 the leaves are thin one-line forwarders to `assignStaticPattern`. On CoreV4 the `assignAccess3D` leaf is the inline static↔indirect router described next.

---

## 3. AXIS-C — the indirect / MX self-route

### Purpose

AXIS-C is a runtime decision made *inside* the chosen leaf, not a separate dispatch. Two leaves contain an inline gather sub-arm, both gated on the *same* predicate — `bir::*AccessPattern::isTensorIndirectDynamicAP()`, reached through vtbl slot `+0x80`. When the predicate is true, the slot self-promotes from its static form to the 16-byte indirect form *in place*; the static and gather forms share the slot and the leaf.

### (i) Non-MX gather — the CoreV4 `assignAccess3D` leaf

```c
function CoreV4_assignAccess3D(this, slot, AP, a4):     // @0x150ccf0
    if !AP.vtbl[0x80](AP):                              // NOT tensor-indirect-dynamic
        return assignStaticPattern_TENSOR3D(this, slot, AP, a4)   // the static 16-byte path (§4)
    // else — tensor-indirect (gather): emit INDIRECT16B in place
    reportError(AP.vtbl[0x80](AP), "must be a Tensor Indirect AP")
    indicesAP = AP.vtbl[0x68](AP)                       // getTensorIndirectIndicesAP
    assignStartAddr_ADDR4(this, slot + 0, indicesAP, a4=0)        // INDICES → ADDR4 @+0
    assignStartAddr_ADDR4(this, slot + 2, AP,        a4=0)        // DATA    → ADDR4 @+4 (slot+2 words)
    slot[4] = AP.vtbl[0x90](AP)                         // getNumIndirectIndices → K-extent @+8
    assert("TODO: support Tensor Indirect Indices AP to have dynamic offset")   // indices: no dyn off
    assert("TODO: support Tensor Indirect Data AP to have dynamic offset")      // data:    no dyn off
    *((byte*)slot + 3) |= 0x20                          // ADDR4 +3.bit5 = indirect-mode marker
```

A single `TENSOR3D` slot therefore self-promotes to `INDIRECT16B` when the access pattern is a tensor-indirect-dynamic AP.

*Anchors: `call [rax+0x80]` @ `0x150cd35`/`0x150cd79`, `call [rax+0x68]` @ `0x150cdbf`, `call [rax+0x90]` @ `0x150cdef`, the `or BYTE PTR [rax+0x3],0x20` stamp @ `0x150ced8`, the string "must be a Tensor Indirect AP" @ `0x1c8639e`, and the two dynamic-offset asserts @ `0x1d71ee0`/`0x1d71f20`.*

### (ii) MX gather — the CoreV4 `assignAccessForMX` leaf

```c
function CoreV4_assignAccessForMX_MXMEM_PATTERN1D(this, slot, dataAP, scaleAP):  // @0x150e2f0
    if dataAP.vtbl[0x80](dataAP):                       // data is tensor-indirect-dynamic
        return assignIndirectPatternForMX_MXINDIRECT16B(this, slot, dataAP, scaleAP)  // @0x150de90 (gather)
    // else — static MX pair:
    assert(dataAP.parent == scaleAP.parent, "DataAP and ScaleAP not from same parent inst")
    assert(!scaleAP.vtbl[0x80](scaleAP), "must not be Tensor Indirect AP when use assignStaticPatternForMX")
    assignStartAddr_ADDR4(this, slot + 0, dataAP,  a4=0)         // DATA  → ADDR4 @+0
    assignStartAddr_ADDR4(this, slot + 4, scaleAP, a4=1)         // SCALE → ADDR4 @+4 (E8M0)
    slot[0xB] = getBasePartitionInUnderlyingMemory(scaleAP)
                 % ArchModel.vtbl[0x64]()                        // scale base-partition % PE_count
    slot.K_extent[+8] = NumElementsPerPartition × 4              // ×4 iff packed-MX dtype (vtbl+0x90)
```

*Anchors: `call [rax+0x80]` @ `0x150e427`/`0x150e4f7`; the two `assignStartAddr<ADDR4>` calls distinguished by `xor ecx,ecx` (a4=0) @ `0x150e544` and `mov ecx,0x1` (a4=1) @ `0x150e554`; the strings "DataAP and ScaleAP not from same parent inst" and "must not be Tensor Indirect AP when use assignStaticPatternForMX".*

### Operand-role selection within a multi-operand inst

A single inst's operands each receive the right descriptor by argument index plus template, chosen in the op's `generate*` function. For MX matmul (`generateMatmultMx` @ `0x143ebd0`, with the `getArgument` indices read off the body):

```c
arg0 = getArgument<AccessPattern>(inst, 0)            → DATA  (Ifmap)
arg1 = getArgument<AccessPattern>(inst, 1)            → MOVING operand
arg2 = getArgument<PhysicalAccessPattern>(inst, 2)    → SCALE (E8M0)
out0 = getOutput<AccessPattern>(inst, 0)              → dst
assignAccessForMX<MXMEM_PATTERN1D>(slot, arg0=DATA, arg2=SCALE)   // data+scale → ONE descriptor
assignAccess<MEM_PATTERN3D>(slot, arg1)                            // moving → its own descriptor
```

DATA and SCALE are packed into one `MXMEM_PATTERN1D` by the dual-arg encoder; the `a4` bit (§4) distinguishes their address resolvers. For the non-MX gather, the index vector is **fetched from the data AP's companion** (`getTensorIndirectIndicesAP`, vtbl `+0x68`), not supplied as a separate argument index. For plain dense ops every operand uses the same template (matmul → `TENSOR3D`, copy → `TENSOR4D`); the role is the argument index only, and all pass `a4=0`.

---

## 4. AXIS-B — the 3-way `ADDR4` kind dispatch

### Purpose

AXIS-B is the *base-address mode* decision, and it is the single point where the access pattern's `kind` field is read. `assignStartAddr<core_v4::ADDR4>` @ `0x1508df0` (byte-identical to the v2 body @ `0x1172e10`) is the **only** `ADDR4` base writer every static-pattern, MX, and indirect leaf calls. It reads `kind = *(DWORD*)(AP + 0x18)` (the `ArgumentKind`) and branches three ways.

### Algorithm

```c
function assignStartAddr_ADDR4(this, slot, AP, a4):       // @0x1508df0
    kind = *(DWORD*)(AP + 0x18)                           // read once: mov eax,[rsi+0x18]

    if kind == 1:                                          // PhysicalAccessPattern — STATIC byte addr
        Hwm = *(this + 0x260)                              // the addr-resolver object (+608)
        if a4: addr = Hwm.vtbl[0x28](AP)                  // a4==1 → getStartAddressForMXScale (SCALE)
        else:  addr = Hwm.vtbl[0x20](AP)                  // a4==0 → getStartAddress          (DATA)
        reportError(addr <= UINT32_MAX, "memory address out of uint32 range")
        *(DWORD*)slot = addr                               // ADDR4 @+0 = static u32 byte-address

    else if kind == 3:                                     // RegisterAccessPattern — DYNAMIC / REGISTER
        reportError(is_regloc_offset(AP + 0x114),
                    "offset register cannot be applied to 32bit Addressing in ISA")
        *(byte*)(slot + 3) |= 0x80                         // ADDR4 bit-31 = register-indirect
        RegId = Register::getRegId(*(AP + 0xE8), 0)        // the COLORED physical reg id
        reportError(RegId, "Register has not been allocated yet!")
        *(byte*)slot = RegId                               // ADDR4 byte-0 = reg id

    else:                                                  // kind 2 (Symbolic) or 0 (base) — HARD REJECT
        reportError(0, "codegen stage can only have PhysicalAP or RegisterAP, "
                       "no SymbolicAP is allowed")
```

*Anchors: `mov eax,[rsi+0x18]` @ `0x1508e24`; `cmp eax,0x1` @ `0x1508e2f`; `mov rdi,[rdi+0x260]` (Hwm @ +608) @ `0x1508e38`; `call [rax+0x20]` @ `0x1508e4c` and `call [rax+0x28]` @ `0x1508ed0`; `cmp eax,0x3` @ `0x1508ee0`; `movzx eax,[r15+0x114]` (is_regloc_offset) @ `0x1508f15`; `or BYTE PTR [rbx+0x3],0x80` @ `0x1508f58`; `mov rdi,[r15+0xe8]` (RegId source) @ `0x1508f5c`; the SymbolicAP-reject string @ `0x1d51e48`.*

### The `a4` bit is the operand-role (data / scale) resolver

> **QUIRK — `a4` selects the address resolver, not "includePartition".** In the `ADDR4` writer, `a4==0` → DATA stream (`getStartAddress`, Hwm vtbl `+0x20`); `a4==1` → SCALE stream (`getStartAddressForMXScale`, Hwm vtbl `+0x28`). A binary sweep shows `a4=1` is passed *only* by the two MX leaves (the `assignAccessForMX` scale slot @+4 and the `MXINDIRECT` scale slot @+8). Every plain `TENSOR`/`MEM_PATTERN`/indices/data slot passes `a4=0`. So "scale" is literally the boolean arg to `assignStartAddr<ADDR4>`. Do **not** conflate this with the *other* documented `a4` bool — the `includePartition` flag on `assignStaticPattern`'s own `a4` (J25 §3.4) — they are distinct booleans on different functions. The `if(a4) vtbl+0x28 else vtbl+0x20` branch, the `a4=0`/`a4=1` MX call sites at `0x150e544`/`0x150e554`, and the all-zero non-MX leaves settle it.

### Static / dynamic / symbolic routing

The `(step, num)` and dtype words are identical across kind-1 and kind-3; only the `@+0` `ADDR4` word differs (immediate vs reg-mode). `kind == 2` (Symbolic) is impossible at the encoder because `lower_ap` (L32) already converted it to kind-3; the SymbolicAP reject is the residue that catches a pipeline bug. The reg-mode `RegId` is minted by `lower_ap` and colored by register allocation (L33) before the encoder runs (see Part 7, `codegenAccess` / I20).

---

## 5. The static path + default-fill

`assignStaticPattern<T>` (T = the chosen template) packs the constant geometry of a non-indirect slot:

```c
function assignStaticPattern_T(this, slot, AP, a4):        // e.g. <core_v4 TENSOR3D> @0x150c390
    assignStartAddr_ADDR4(this, slot + 0, AP, a4)          // base ADDR4 @+0 (the §4 kind dispatch)
    // free dims Pattern[1..N-1] → (step,num) u16 pairs, TWO separate contiguous arrays:
    for d in 1 .. AP.Pattern.size - 1:                     // index 0 (partition W) is SKIPPED
        slot.steps[d] = Pattern[d].step                    // i16 array  @ +4 ..
        slot.nums[d]  = Pattern[d].num                     // u16 array  @ +(4+2N) ..
    // ⭐ DEFAULT-FILL: every UNUSED dim of the fixed template = {step=1, num=1}:
    fill_remaining(slot, 0x00010001)                       // the 65537 immediate — NEVER zero
    // Pattern[0] = W (partition dim) folds into the ADDR4 @+0 band bits
```

The partition dimension `Pattern[0] = W` is folded into the `ADDR4 @+0` band bits (the loop skips index 0); the free dims become *separate* stride and num arrays (the 4+4N rule, [2.5](mempattern-2d-3d.md)). Every dim of the fixed template beyond the access pattern's active count is unit-filled, never zeroed:

> **NOTE — unused free dims are `{step=1, num=1}`, not zero.** A 1-D access pattern packed into a `TENSOR4D` slot becomes `{real dim} + {1,1} + {1,1} + {1,1}`. A `num=0` word reads as a decode error downstream, not a no-op, so the degenerate dim must iterate exactly once. `assignStaticPattern<core_v4::TENSOR3D>` @ `0x150c390` writes `mov DWORD PTR [rax+0x4],0x10001` @ `0x150cb51` and `mov DWORD PTR [rax+0xa],0x10001` @ `0x150cb58`.

So `Pattern.size` (the "dim-count") drives **only** the unit-fill loop bound — it does *not* select the descriptor type. That selection was AXIS-A, fixed at compile time. Every `APPair` access is bounds-checked (`idx < size()`; `SmallVector.h:0x128 __assert_fail`), and `Pattern.size` is range-checked `[1,4]` by guard (G-b).

---

## 6. `assignAccessToType` — the dormant string-keyed router

`CoreV2GenImpl::assignAccessToType(string typeName, AP&)` @ `0x1237a50` is a literal type-NAME → encoder dispatcher: it builds an `" AP…"` prefix, then `std::string::compare` against the four `NEURON_ISA_TPB_TENSOR{1,2,3,4}D` names and an `IMM_VAL_INST_FIELD` arm, allocating `8`/`12`/`16`/`20` bytes respectively and tail-calling `assignAccess<T>`; an unknown name `reportError`s.

```c
function assignAccessToType(typeName, AP):                 // @0x1237a50
    if typeName.compare("NEURON_ISA_TPB_TENSOR1D") == 0:  buf = new(8);  assignAccess<TENSOR1D>(buf, AP)
    if typeName.compare("NEURON_ISA_TPB_TENSOR2D") == 0:  buf = new(12); assignAccess<TENSOR2D>(buf, AP)
    if typeName.compare("NEURON_ISA_TPB_TENSOR3D") == 0:  buf = new(16); assignAccess<TENSOR3D>(buf, AP)
    if typeName.compare("NEURON_ISA_TPB_TENSOR4D") == 0:  buf = new(20); assignAccess<TENSOR4D>(buf, AP)
    if typeName.compare("NEURON_ISA_TPB_IMM_VAL_INST_FIELD") == 0:  // immediate-pointer arm
        reportError("ISA immediate pointer should only be SB/...")
    else:  reportError("unknown type")
```

Despite reading like the encoder's front door, this function is **dormant**. The CoreV2 variant has no `MEM_PATTERN`, MX, or `INDIRECT` branch — those live on the CoreV4 `generate*` paths (§2) — and its only caller is its own PLT thunk (`0x5ea7e0 → 0x1237a50`, a type-19 self-reference). No live internal caller exists. It is a string-driven helper, most likely a JSON/replay or debug surface, not the runtime encoder dispatch; the real dispatch is the per-op `generate*`/`visit*` template instantiation of §2.

Its value to a reader is declarative: it enumerates, in one place, the type→width table and the type-name strings the rest of the encoder uses. The `mov edi,0x8`/`0xc`/`0x10`/`0x14` allocations bracketing the four `std::string::compare@plt` calls match the 4+4N width table exactly. The dormancy holds for `libwalrus` internals; a Python or cross-`.so` dynamic caller cannot be fully excluded.

> **GOTCHA —** do not model the encoder as a string-keyed router on `assignAccessToType`. It is the wrong mental model *and* dead code; the descriptor type is bound at compile time per (op, operand-slot).

---

## 7. The validation chain — encode → validate, by descriptor type

Each emitted descriptor is checked by a matching L2 wire validator, reached through the per-format gate (the L1/L2 dispatch of G04/G12):

```text
runSingleISACheck @0x1435010
  → neuron_isa_check_opcode_on_engine @0x1504700
  → is_valid_neuron_engine_instruction @0x1502120     (64-byte INST_UNION cascade)
  → is_valid_<format>(INST_UNION, coreVer)            (per-format gate)
  → the per-descriptor *_valid leaf
```

| Descriptor type | Validator (core_v4) | Format gate (caller) | Confidence |
|---|---|---|---|
| `ADDR4` (@+0 word) | `tensor_start_addr_valid` @ `0x127f590` | `is_valid_smx1d3_mm`:218/221 | CERTAIN |
| `TENSOR1D` | `tensor1d_valid` @ `0x1447330` | per-engine tensor gates | CERTAIN |
| `MEM_PATTERN2D` | `mem2d_valid` @ `0x14468c0` | dst format gates | CERTAIN |
| `MEM_PATTERN3D` (dst) | `mm_dst_mem3d_valid_nc_v4` @ `0x1447fe0` | `is_valid_matmul_regular` @ `0x14b5c60`; `is_valid_smx1d3_mm`:228/233 | CERTAIN |
| `MEM_PATTERN3D` (generic) | `mem3d_valid` @ `0x14467d0` | dbg path | CERTAIN |
| `MEM_PATTERN4D` | `mem4d_valid` @ `0x1446550` | dst 4D gates | CERTAIN |
| `MXMEM_PATTERN1D` | `mxmem1d_valid` @ `0x1447190` | `is_valid_s3dmx1_quant` @ `0x14c78f0`; `is_valid_smx1_lw` @ `0x14fef30`; `smx1d3_mm`:210 | CERTAIN |

> **NOTE — one format gate validates every descriptor the encoder emitted, in slot order.** `is_valid_smx1d3_mm` @ `0x1501ac0` calls, in slot order, `mxmem1d_valid` (the MX data+scale pair), `tensor_start_addr_valid` ×2 (the `ADDR4` bases), and `mm_dst_mem3d_valid_nc_v4` ×2 (the `MEM_PATTERN3D` dst) — the exact type↔validator pairing the matmul-MX encoder chose.

*Anchors: `call …mxmem1d_valid@plt` @ `0x1501e12`; `call …tensor_start_addr_valid@plt` @ `0x1501e5f`/`0x15020b8`; `call …mm_dst_mem3d_valid_nc_v4@plt` @ `0x1501e9c`/`0x1501ec2`.*

Each validator re-reads the same wire fields the encoder wrote: `mem{2,3}d_valid` check the region nibble (`byte3 & 0x60 == 0x20` PSUM), bank `≤ 0x3FFFFF`, and the `(step, num)` arrays; `mxmem1d_valid` checks the `+3.bit5 = 0x20` indirect marker the gather arm stamped (§3).

---

## 8. End-to-end flowchart

```text
┌─ bir::AccessPattern (E12): kind@+0x18, Dtype@+0x30, Pattern.data@+0x50 / size@+0x58,
│  DynamicAPINFO@+0x1D8, vtbl[0x80]=isTensorIndirectDynamicAP
▼
OP ENCODER (generate*/visit*) names the descriptor TEMPLATE per operand role     ── AXIS-A
│   matmul src/wt/dst → TENSOR3D   ·  copy → TENSOR4D
│   MX data+scale → MXMEM_PATTERN1D · MX moving → MEM_PATTERN3D
│   on-engine dst → MEM_PATTERN2D/3D · DMA gather → ADDR8 (separate path)
▼
Generator::assignAccess<T>  (guards: indirect-reject[TENSOR only] · [1,4] · SB/PSUM ·
│                            "N bytes to encode")  → tail vtbl-leaf (slot by type)
▼
ARCH LEAF ── isTensorIndirectDynamicAP()? ──┐                                     ── AXIS-C
│ no                                          │ yes
▼                                             ▼
assignStaticPattern<T>                    INDIRECT16B / MXINDIRECT16B inline
│ (4+4N pack, unit-fill {1,1})             (indices @+0, data @+4 [, scale @+8],
│                                           K-extent @+8, +3|=0x20 indirect bit)
▼
assignStartAddr<ADDR4> ── kind @ +0x18 ──┐                                        ── AXIS-B
│ ==1 Physical                            │ ==3 Register      else (2/0) → REJECT
▼                                          ▼                  "no SymbolicAP allowed"
static u32 addr                       reg-mode ADDR4
(a4: 0=getStartAddress DATA /         (+3|=0x80, byte0=RegId)
 1=getStartAddressForMXScale SCALE)
▼
WIRE DESCRIPTOR (ADDR4 @+0 + steps/nums + role fields)
▼
L2 VALIDATE: runSingleISACheck → is_valid_neuron_engine_instruction →
   is_valid_<fmt> → {tensor_start_addr_valid · mem{2,3,4}d_valid ·
                     mm_dst_mem3d_valid_nc_v4 · tensor1d_valid · mxmem1d_valid}
```

---

## 9. Cross-checks

> **GOTCHA — there is no runtime dim-count selector.** A `getMemPattern`-style "if dims==3 emit `TENSOR3D`" switch does not exist. The descriptor type is a compile-time template instantiation per operand role (AXIS-A); `Pattern.size` only feeds the unit-fill loop (§5).

How the three axes line up against the sibling descriptor pages:

- **AXIS-B kind dispatch** reads byte-for-byte on the core_v4 body @ `0x1508df0` (`kind@+0x18 == 1/3/else`, with the same three reject strings). This page adds that in the MX context the `a4` bool is the data/scale resolver (`Hwm` vtbl `+0x20` vs `+0x28`), not `includePartition`.
- **src `TENSOR` vs dst `MEM_PATTERN`** share both the vtbl leaf and the field math (both tail vtbl `+0x48`). This page adds the guard asymmetry: `TENSOR` wrappers reject the indirect field, `MEM_PATTERN` wrappers do not, and the CoreV4 `assignAccess3D` leaf is the inline `TENSOR3D ↔ INDIRECT16B` router.
- **MX self-route** — `assignAccessForMX` → `MXINDIRECT16B` on vtbl `+0x80`, data `a4=0` / scale `a4=1`, `% PE` base-part — reads verbatim.
- The non-MX `INDIRECT16B` string appears only in the CoreV4 `assignAccess3D`/`4D` leaves and the MX leaves. Whether the thin CoreV2/V3 `assignAccess1D/2D/4D` forwarders also carry an inline indirect arm was not swept individually; the string is absent from them, which is suggestive but not conclusive.

---

## Cross-References

- [TENSOR1D / 2D / 3D Descriptors — the 4+4N Rule](tensor-descriptors.md) — the SRC descriptor family AXIS-A selects; the 4+4N geometry the static packer writes
- [ADDR4 — the 32-Bit Address Word](addr4.md) — the `@+0` word AXIS-B writes (static u32 / reg-mode bit-31 + RegId)
- [MEM_PATTERN2D / 3D — the DST/PSUM Role](mempattern-2d-3d.md) — the DST byte-twin; the shared `+0x48` packer leaf; the guard asymmetry
- [TENSOR4D / MEM_PATTERN4D — the Spill Descriptor](tensor4d-mempattern4d.md) — the 20-byte template the copy op names (`TENSOR4D`×2)
- [The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — the `INST_UNION` the per-format `is_valid_<fmt>` gate validates
- [MXMEM_PATTERN1D — MX Data + E8M0 Scale](mxmem-pattern1d.md) — the MX data+scale descriptor `assignAccessForMX` packs (2.6)
- [Indirect-Gather Descriptors — INDIRECT16B / 20B / MXINDIRECT16B](indirect-descriptors.md) — the gather forms AXIS-C self-routes into (2.7)
- `bir/` (Part 7, forthcoming) — `codegenAccess` (I20): where the access pattern is lowered, `kind-2 → kind-3`, and the reg id minted before the encoder runs
- `walrus/` (Part 8, forthcoming) — the AP encoder backend (J23–J26): the `generate*`/`visit*` op encoders and the validator family
