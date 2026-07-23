# Indirect-Gather Descriptors — INDIRECT16B / INDIRECT20B / MXINDIRECT16B

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 share the C++ core, but `libwalrus.so` is rebuilt per wheel — re-confirm any raw offset against cp311/cp312). The three encoders, their static siblings, and the wire-validators live in `libwalrus.so` (build-id `92b4d331…`, `.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset); the `bir::AccessPattern` indirect accessors live in `libBIR.so` (build-id `a9b1ea38…`). See [Build & Version Provenance](../reference/versions.md).*

## Abstract

An **indirect-gather descriptor** is a `MEM_PATTERN` slot whose addressing is supplied at runtime by an *index vector* rather than by static strides. It is how the Tonga backend encodes a `gather`/`scatter`: instead of `out[p][i] = data[p][stride·i]`, the descriptor names an *index* operand and a *data* operand, and the hardware reads `out[p][i] = data[p][index[p][i]]`. Three on-wire forms exist — `INDIRECT16B`, `INDIRECT20B`, and `MXINDIRECT16B` — and they are not a separate struct family: each one **reuses the byte width of a plain static pattern** (`TENSOR3D` = 16 B, `TENSOR4D` = 20 B, `MXMEM_PATTERN1D` = 16 B) and overwrites its leading fields with gather operands. The descriptor is built atop [ADDR4](addr4.md) (2.2): the gather is signalled by setting the **bit-29 indirect marker** (`0x20`) on byte 3 of the *index* ADDR4 word — the exact mode-nibble value (`0b01`) whose resolution ADDR4 §"the mode nibble" already pins.

The three forms differ on two orthogonal axes. **Width** — `INDIRECT16B` (3-D AP) vs. `INDIRECT20B` (4-D AP): the indirect payload (`index@+0`, `data@+4`, `num@+8`) is byte-identical; the only difference is the slot width of the static sibling the *same* encoder tail-calls on its non-indirect branch. **MX-ness** — `MXINDIRECT16B` is reached only when an MX operand (an fp8/fp4-x4 tensor carrying an E8M0 block-scale stream) is itself gathered; it inserts a third ADDR4 (the scale word) in front of the data, pushing every later field by 4. There is **no** `MXINDIRECT20B` in 2.24 — the MX gather is 1-D only.

The bar for this page is that a reader can **encode any of the three indirect descriptors by hand** — placing the ADDR4 words, stamping the bit-29 marker, writing the gather count — and knows **which BIR op and which kernel** consumes each variant. Everything is recovered from the `libwalrus.so` encoder/decoder bodies (the dynamic symbol table survives, so `nm -DC` demangles them); the byte offsets are pinned against the literal `mov`/`or`/`shl` constants in the disassembly, not inferred. The reimplementation contract is:

- **The byte layout** of all three slots — every ADDR4 offset, the count field, the MX scale-partition control byte, and which bytes are inert.
- **The encoder fork** — every `assignAccess*` function handles *both* the static and the indirect case off one `isTensorIndirectDynamicAP()` test; the indirect branch is a 3-field record, not a static-pattern fill.
- **The bit-29 marker** as the encoder↔decoder contract, and the per-descriptor quadrant invariant the decoder enforces.
- **The op→kernel bindings** — which BIR op emits each descriptor, and which gather/scatter kernel consumes it.

## At a glance

| | |
|---|---|
| **Non-MX 3-D encoder** | `CoreV4GenImpl::assignAccess3D` (`= assignIndirectPattern<INDIRECT16B>`) `0x150ccf0` |
| **Non-MX 4-D encoder** | `CoreV4GenImpl::assignAccess4D` (`= assignIndirectPattern<INDIRECT20B>`) `0x150d8e0` |
| **MX encoder** | `CoreV4GenImpl::assignIndirectPatternForMX<MXINDIRECT16B>` `0x150de90` |
| **MX routing leaf** | `CoreV4GenImpl::assignAccessForMX<MXMEM_PATTERN1D>` `0x150e2f0` |
| **CoreV2 twins** | `CoreV2GenImpl::assignAccess3D` `0x1176de0` / `assignAccess4D` `0x1177620` |
| **MX validator** | `core_v4::mxmem1d_valid` `0x1447190` |
| **3-D quadrant validator** | `core_v4::indirect_quadrant_check_src3d` `0x1448450` |
| **4-D quadrant validator** | `core_v4::indirect_quadrant_check_src4d` `0x14484a0` |
| **DMA-indirect validator** | `core_v3::has_dma_indirect_valid_idx_start_addr` `0x13707c0` |
| **Gather count accessor** | `bir::*AccessPattern::getNumIndirectIndices` (vtbl `+0x90`; base `0x20fed0`) |
| **Slot widths** | `INDIRECT16B` 16 B · `INDIRECT20B` 20 B · `MXINDIRECT16B` 16 B |
| **Indirect marker** | `or byte [INDEX_ADDR4+3], 0x20` — bit 29, mode nibble `0b01` (per [ADDR4](addr4.md) 2.2) |

> **NOTE — the descriptor name encodes the *slot width*, not a distinct struct.** `INDIRECT16B`/`20B`/`MXINDIRECT16B` are template instantiations of `assignIndirectPattern<…>` keyed on the static pattern they share a slot with (`TENSOR3D`/`TENSOR4D`/`MXMEM_PATTERN1D`). On the wire an indirect record is *narrower* than its named width — it writes only 3 (non-MX) or 4 (MX) fields and leaves the rest of the slot inert. The width comes from the AP's free-dim count, which fixes which slot the record occupies.

## The byte map — three slots, one indirect payload

```text
 INDIRECT16B  (16 B; non-MX 3-D)      MXINDIRECT16B (16 B; MX 1-D)
 +0x00  ADDR4  INDEX   (bit29 set)    +0x00  ADDR4  INDEX   (bit29 set)
 +0x04  ADDR4  DATA                   +0x04  ADDR4  DATA
 +0x08  u16    NUM                    +0x08  ADDR4  SCALE   (a4=1, MX scale)
 +0x0A..0x0F   inert                  +0x0C  u16    x4-NUM
 (= TENSOR3D static slot, unfilled)   +0x0E  -       (count high half, spare)
                                      +0x0F  u8      SCALE base-partition % PE_count

 INDIRECT20B  (20 B; non-MX 4-D)
 +0x00  ADDR4  INDEX   (bit29 set)
 +0x04  ADDR4  DATA
 +0x08  u16    NUM
 +0x0A..0x13   inert  (= TENSOR4D static slot, unfilled)

   in EVERY form:  byte (INDEX_ADDR4 + 3).bit5 = 0x20  ← the TENSOR-INDIRECT marker
```

| Field | Off (16B/20B) | Off (MX) | Type | Source | Confidence |
|---|---|---|---|---|---|
| INDEX ADDR4 | `+0x00` | `+0x00` | u32 | `getTensorIndirectIndicesAP()` (vtbl `+0x68`), `a4=0` | CERTAIN |
| DATA ADDR4 | `+0x04` | `+0x04` | u32 | the gathered data AP, `a4=0` | CERTAIN |
| SCALE ADDR4 | — | `+0x08` | u32 | the E8M0 scale AP, `a4=1` | CERTAIN |
| NUM / x4-NUM | `+0x08` (u16) | `+0x0C` (u16) | u16 | `getNumIndirectIndices()` (vtbl `+0x90`); ×4 on MX iff Dtype∈{2,8,9} | CERTAIN |
| SCALE base-part | — | `+0x0F` (u8) | u8 | `scalePart % PE_count` | CERTAIN |
| indirect marker | `+0x03`.bit5 | `+0x03`.bit5 | bit | `or [+3],0x20` on the INDEX ADDR4 | CERTAIN |

> **QUIRK — three full 32-bit ADDR4s fit in a 16-byte MX slot with room for the count.** `3 × ADDR4 = 12 B` (`+0`/`+4`/`+8`) `+ u16 count` (`+0xC`/`+0xD`) `+ 1 control byte` (`+0xF`) = exactly 16. The words are **contiguous and non-overlapping** — nothing is bit-packed. Byte `+0xE` is the spare high half of the `+0xC` count word. This is the plain `MXMEM_PATTERN1D` (data@+0 / scale@+4 / num@+8 / step@+0xA / part@+0xB) with the INDEX ADDR4 inserted at the front, shifting every later field `+4`. The `MXMEM_PATTERN1D` step-direction byte at `+0xA` is *not* re-emitted on the gather path — the index vector supplies the ordering. *(`+0xE` being a spare count byte is INFERRED from the u16 store; everything else is disasm-pinned.)*

## INDIRECT16B — the non-MX 3-D gather (16 bytes)

### Purpose

The generic on-chip gather descriptor with no microscaling: an index vector and a data tensor, both SBUF- or PSUM-resident. It is produced when an op with a **3-free-dim** access pattern carries a tensor-indirect data operand — a per-partition `out[p][i] = data[p][index[p][i]]`.

### Algorithm

`assignAccess3D` handles **both** the static-3D and the indirect-3D case off one fork. The mangled symbol is `assignAccess3D`, but the body is the template `assignIndirectPattern<NEURON_ISA_TPB_INDIRECT16B>` — the `.rodata` context-name `0x1d71fa8` `"assignIndirectPattern<…INDIRECT16B>"` is loaded at entry:

```c
// CoreV4GenImpl::assignAccess3D(void *slot, bir::AccessPattern const& ap, bool)  @ 0x150ccf0
// = assignIndirectPattern<core_v4::NEURON_ISA_TPB_INDIRECT16B>
void assignAccess3D(u8 *B, AccessPattern const& ap, bool out) {
    if (!ap.isTensorIndirectDynamicAP())          // call [r12+0x80]  @0x150cd35 -> fork
        return assignStaticPattern<TENSOR3D>(B, ap, out);  // jmp 0x150c390  @0x150cf0b
                                                  //   16-byte 4+4N static fill

    // --- the INDIRECT branch ---
    AccessPattern* idxAP = ap.getTensorIndirectIndicesAP();      // call [r12+0x68]  @0x150cdbf
    assignStartAddr<ADDR4>(&B[0], idxAP, /*a4=*/0);              // INDEX @+0       @0x150cdc6
    assignStartAddr<ADDR4>(&B[4], ap,    /*a4=*/0);              // DATA  @+4       @0x150cdda
    u16 num = ap.getNumIndirectIndices();                       // call [r12+0x90] @0x150cdef
    *(u16*)&B[8] = num;                                          // NUM @+8 (NO x4) @0x150ce0b

    u8 gran = archModel->granularity;     // movzx r12d,[rax+0x38]                  @0x150ce17
    assert((gran & B[3]) == 0,  "TODO: support Tensor Indirect Indices AP to have dynamic offset");
    assert((gran & B[7]) == 0,  "TODO: support Tensor Indirect Data AP to have dynamic offset");
    B[3] |= 0x20;                         // or byte [rax+3],0x20  ← bit29 marker    @0x150ced8
    return;                               // ret  — does NOT fill the 3D stride/num  @0x150ceea
}
```

> **GOTCHA — the indirect branch does not fall through to the `TENSOR3D` static fill.** It writes only `{INDEX@+0, DATA@+4, NUM u16@+8, +bit29@+3}` and **returns** at `0x150ceea`; only the *non-indirect* branch tail-calls `assignStaticPattern<TENSOR3D>` (`0x150c390`). So on the wire an `INDIRECT16B`'s bytes `+0xA..+0xF` are inert, not stride/num-filled. The "16B" suffix names the static sibling's slot width — the slot the indirect record sits in — not its byte content.

The count comes from `getNumIndirectIndices()` (vtbl `+0x90`), **not** the partition-element count, and is **not** ×4-scaled — there is no `shl eax,2` between the `call [+0x90]` (`0x150cdef`) and the `mov [rcx+8],ax` (`0x150ce0b`). See §"Decoder validation" for why this slot is `+0x90` and not `getNumElementsPerPartition`.

There are exactly **two** dynamic-offset asserts (index `+3`, data `+7`) and **no** scale ADDR4 — that is precisely what distinguishes `INDIRECT16B` from `MXINDIRECT16B`.

## INDIRECT20B — the non-MX 4-D gather (20 bytes)

### Purpose

The 4-D sibling of `INDIRECT16B`. Identical *indirect payload*, identical fork, but the **static fallback** tail-calls `assignStaticPattern<TENSOR4D>` (the 20-byte 4-D pattern), so the slot is 20 bytes. A 4-free-dim AP routes here because one dim cannot be folded into the partition axis and spills to a fourth descriptor dim — exactly the `TENSOR4D` spill rule (see [tensor descriptors](tensor-descriptors.md), 2.3).

### Algorithm

```c
// CoreV4GenImpl::assignAccess4D(void *slot, bir::AccessPattern const& ap, bool)  @ 0x150d8e0
// = assignIndirectPattern<core_v4::NEURON_ISA_TPB_INDIRECT20B>   (rodata 0x1d72038)
void assignAccess4D(u8 *B, AccessPattern const& ap, bool out) {
    if (!ap.isTensorIndirectDynamicAP())          // call [+0x80]    @0x150d925
        return assignStaticPattern<TENSOR4D>(B, ap, out);  // jmp 0x150cf60  @0x150dafb
                                                  //   20-byte 4-D fill, unused dims 1-filled

    AccessPattern* idxAP = ap.getTensorIndirectIndicesAP();      // call [+0x68]  @0x150d9af
    assignStartAddr<ADDR4>(&B[0], idxAP, 0);                     // INDEX @+0     @0x150d9be
    assignStartAddr<ADDR4>(&B[4], ap,    0);                     // DATA  @+4     @0x150d9d3
    *(u16*)&B[8] = ap.getNumIndirectIndices();                  // NUM @+8 (NO x4) @0x150d9fb
    // index assert [B+3], data assert [B+7]  vs archModel->granularity
    B[3] |= 0x20;                          // or byte [rax+3],0x20               @0x150dac8
}
```

⇒ `INDIRECT20B = INDIRECT16B + 4 bytes of static-fallback slot width`. The indirect payload is byte-identical (`index@+0`, `data@+4`, `num u16@+8`, marker `+3`); the 16-vs-20 choice is the AP free-dim count (3-D → `assignAccess3D` → 16 B; 4-D → `assignAccess4D` → 20 B). Same two asserts, no scale. The same C-1 correction applies: the indirect branch returns without filling the 4-D stride/num region.

## MXINDIRECT16B — the MX DGE gather (16 bytes)

### Purpose

The microscaling gather. Reached **only** when an MX operand — a `QuantizeMx` output, or a `MatmultMx` ifmap/weights — is itself tensor-indirect. This is the MoE expert/token data-gather-engine (DGE) path: the routed-token / expert index vector selects which fp8/fp4-x4 rows (and their E8M0 block scales) feed the quantized matmul. The MX leaf self-routes: `assignAccessForMX<MXMEM_PATTERN1D>` (`0x150e2f0`) tests `dataAP.isTensorIndirectDynamicAP()` (vtbl `+0x80`) and tail-jumps here when the data operand is indirect.

### Algorithm

```c
// CoreV4GenImpl::assignIndirectPatternForMX<core_v4::NEURON_ISA_TPB_MXINDIRECT16B>  @ 0x150de90
void assignIndirectPatternForMX(u8 *B, AccessPattern const& dataAP, AccessPattern const& scaleAP) {
    // --- four asserts (strings recovered verbatim, §Evidence) ---
    assert(scaleAP.parentInstr == dataAP.parentInstr,
           "MX DataAP and ScaleAP are not from the same parent instruction");
    assert(dataAP.isTensorIndirectDynamicAP(),  "DataAP must be a Tensor Indirect AP");
    assert(scaleAP.isTensorIndirectDynamicAP(),
           "If the argument of matmult MX is indirect, the corresponding scale should also have indirection");
    assert(scaleAP.indexArgId == dataAP.indexArgId,   // DynamicAPINFO+0xf0
           "Furthermore, the corresponding scale must have exactly the same indirection as the argument");

    // --- the triple-ADDR4 packing ---
    AccessPattern* idxAP = dataAP.getTensorIndirectIndicesAP();   // call [rdx+0x68] @0x150e092
    assignStartAddr<ADDR4>(&B[0], idxAP,   /*a4=*/0);             // INDEX @+0       @0x150e099
    assignStartAddr<ADDR4>(&B[4], dataAP,  /*a4=*/0);             // DATA  @+4       @0x150e0aa
    assignStartAddr<ADDR4>(&B[8], scaleAP, /*a4=*/1);             // SCALE @+8 (MX)  @0x150e0c5

    // --- scale base-partition modulo PE_count ---
    u64 q = scalePart;  u32 pe = archModel->PE_count;            // mov ecx,[rdx+0x64] @0x150e0e2
    B[0xF] = (u8)(q % pe);              // div rcx; mov [rax+0xF],dl                  @0x150e0e7/ee

    // --- the x4 K-extent ---
    u32 n = dataAP.getNumIndirectIndices();          // call [+0x90]                  @0x150e108
    if (dataAP.dtype == 2 || (u32)(dataAP.dtype - 8) <= 1)   // Dtype in {2,8,9}      @0x150e10e
        n <<= 2;                                     // shl eax,2  ← x4-packed dtypes  @0x150e11e
    *(u16*)&B[0xC] = (u16)n;                          // x4-NUM @+0xC                  @0x150e139

    // --- three dynamic-offset asserts + the indirect bit ---
    assert((gran & B[3])  == 0, "TODO: support Tensor Indirect Indices AP to have dynamic offset");
    assert((gran & B[7])  == 0, "TODO: support Tensor Indirect Data AP to have dynamic offset");
    assert((gran & B[0xB])== 0, "TODO: support MX scale AP to have dynamic offset");
    B[3] |= 0x20;                       // or byte [rax+3],0x20  ← bit29 marker        @0x150e26b
}
```

> **GOTCHA — the x4-NUM is the *only* place a gather count is multiplied.** The x4 gate is `Dtype == 2 || (unsigned)(Dtype − 8) ≤ 1` ⇔ `Dtype ∈ {2, 8, 9}` = `{float4_e2m1fn_x4, float8_e4m3fn_x4, float8_e5m2_x4}` — the four-element-packed MX dtypes. Their gather count is in *packed groups*, so the hardware K-extent is `4 × getNumIndirectIndices()`. The non-MX `INDIRECT16B`/`20B` `NUM@+8` is **never** ×4 (no `shl` in `assignAccess3D/4D`). A reimplementation that ×4s the non-MX count, or that fails to ×4 the MX count for these dtypes, mis-sizes the gather. `0x150e11e: c1 e0 02 shl eax,2` exists only in the MX encoder; the gate `cmp edx,2 / sub edx,8 / cmp edx,1 / ja` is at `0x150e10e–11c`.

### The a4 scale-stream selector

The third `bool a4` to `assignStartAddr<ADDR4>` routes the address resolver (see [ADDR4](addr4.md) 2.2, §"the data-vs-scale resolver fork"): `a4=0` → Hwm `vtbl+0x20` (`getStartAddress`, DATA path); `a4=1` → `vtbl+0x28` (`getStartAddressForMXScale`, SB-only). INDEX and DATA pass `a4=0`; **SCALE passes `a4=1`** (`mov ecx,1` at `0x150e0c5`) — the only thing distinguishing the scale ADDR4 from the data ADDR4 in the shared word emitter. There is **no `MXINDIRECT20B`** in 2.24 — the symbol occurs zero times in the binary, so the MX gather is 1-D only.

> **NOTE — all three MX operands must carry static base addresses.** The three high-byte asserts (`+3` index, `+7` data, `+0xB` scale) `AND` each ADDR4 byte 3 against the arch granularity mask `archModel[+0x38]` and reject a register/dynamic byte-offset on *any* operand. In 2.24 the index vector is the sole dynamic-addressing source; the three ADDR4 bases are static. The `"TODO: …"` strings are the assert messages, not dead code — they fire if a caller hands in a dynamic-offset AP.

## Which BIR ops + kernels consume each variant

The selection is three orthogonal axes, all decided at descriptor-encode time:

| Axis | Values | Decided by | Source |
|---|---|---|---|
| MX vs non-MX | MX → `MXINDIRECT16B`; non-MX → `INDIRECT16B/20B` | `assignAccessForMX` self-routes on vtbl `+0x80` | CERTAIN |
| 16B vs 20B (non-MX) | 3-free-dim → 16B; 4-free-dim → 20B | AP free-dim count picks `assignAccess3D`/`4D` | CERTAIN |
| indirect vs static | every `assignAccess*` | `isTensorIndirectDynamicAP()` (vtbl `+0x80`) | CERTAIN |

| Descriptor | BIR op(s) | Kernel / codegen leaf | Confidence |
|---|---|---|---|
| `MXINDIRECT16B` | `InstMatmultMx` / `InstQuantizeMx`, when an MX operand AP is tensor-indirect → `assignAccessForMX` → `MXINDIRECT16B` | MoE expert/token DGE gather: `backend::ExpertMLPsTKGKernel` (`lowerKernel` `0xd88640`) — `computeGateUpProj`/`DownProj` `For{Tokens,Experts}` | HIGH |
| `INDIRECT16B` (2-ADDR4, 3-D) | `InstGather` (92) ← `klr::NcNGather`; `InstIndirectCopy` (26) ← `klr::NcLocalGather`; `InstIndirectLoad/Save` (43/45); `InstGenericIndirectLoad/Save` (42/44) | per-partition `int32` row gather (`NcNGather`); 16-partition-block `u16`-offset SBUF gather (`NcLocalGather` → `IndirectCopy`) | HIGH |
| `INDIRECT20B` (2-ADDR4, 4-D) | same op family with a 4-free-dim index/data AP | same kernels, 4-D index/data geometry | HIGH |
| *(scatter)* | `InstIndirectSaveAccumulate` (46) — opcode `0xCA`, plain-ADDR4 RMW (no `0x20` mode bit; CoreV2) | MoE scatter-add / embedding-bag back-write — the scatter mirror of the gathers | HIGH |

The MoE expert-routing chain: a router top-k yields an expert-id / routed-token **index tensor**; the gather of selected tokens/experts is `NcNGather → InstGather(92)` (`out[p][i] = params[p][indices[p][i]]`, the `INDIRECT16B/20B` record) or `NcLocalGather → InstIndirectCopy(26)` (a 16-partition-block `u16`-offset SBUF gather, where an index `> num_valid_indices` is a `−1` skip). The MX-quantized expert matmul (a `MatmultMx` with a gathered ifmap/weights operand) is the `MXINDIRECT16B` path. The scatter back is `InstIndirectSaveAccumulate(46)` — a plain ADDR4 RMW that carries no `0x20` indirect mode bit and is emitted by the CoreV2 family.

> **NOTE — `GenericIndirectLoad/Save` are concretized *before* reaching these encoders.** `GenericIndirectLoad(42)`/`Save(44)` are expanded by a `LowerGenericIndirect` pass (`codegenIndirectLoadSave` `0xb580e0`) into concrete offset arithmetic + an indirect DMA. By contrast `Gather(92)`/`IndirectCopy(26)` are **already** concrete (a single resolved index axis) and flow straight to their `CoreV*GenImpl::visitInst*` emitters, which call `assignAccess3D/4D` to materialize the `INDIRECT16B/20B` record. The DMA-engine indirect descriptor (opcode `0xBB`) takes a different validator (see below). The op-number bindings are assembled from the codegen-leaf symbols and the MX-operand routing; no emitted `.neff` gather descriptor was diffed against them.

## Decoder validation + encoder↔decoder byte agreement

There is **no** dedicated `indirect16b_valid`/`indirect20b_valid` symbol. An indirect record is validated as a `MEM_PATTERN` slot: the bit-29 marker selects the indirect arm *inside* a shared validator, then a per-descriptor quadrant check enforces the gather invariant.

### The mode-nibble selector

```asm
; core_v4::mxmem1d_valid @ 0x1447190  (validates MXMEM_PATTERN1D and MXINDIRECT16B)
0x144719c:  49 c1 ee 18    shr  r14, 18h        ; byte3 of the first (INDEX/DATA) ADDR4
0x14471a9:  83 e0 60       and  eax, 60h        ; isolate bits[30:29] (the mode nibble)
0x14471b4:  3c 20          cmp  al, 20h         ; ⭐ == 0b01 INDIRECT? → take the gather arm
;   then up to 3× tensor_start_addr_valid on the INDEX / DATA / SCALE words
```

The same 16-byte struct decodes as `MXMEM_PATTERN1D` (nibble 0) or `MXINDIRECT16B` (nibble `0x20`); each ADDR4 (data, scale, and the index when indirect) is re-validated through `tensor_start_addr_valid` (see [ADDR4](addr4.md) 2.2) for addr/align/PSUM-window/register-mode.

### The same-quadrant invariant

```asm
; core_v4::indirect_quadrant_check_src3d @ 0x1448450  (INDIRECT16B)
0x1448457:  c1 e8 18       shr  eax, 18h        ; byte3 of the INDEX ADDR4
0x144845a:  83 e0 e0       and  eax, 0E0h       ; isolate bits[31:29]  (mode AND register-mode)
0x144845d:  3c 20          cmp  al, 20h         ; ⭐ == 0x20 (indirect, register-mode clear)?
;   if (!indirect) return 1            ; not a gather descriptor — skip
;   data = slot[+4] & 0x1FFFFFFF
0x1448480:  3d ff ff 3f 00 cmp  eax, 3FFFFFh    ; DATA in PSUM window? → ok
;   else index = slot[+0] & 0x1FFFFFFF
0x1448491:  e9 …           jmp  addresses_in_same_sbuf_quadrant(index, data)   ; ⭐ same quadrant
```

For an SBUF gather the INDEX ADDR4 and the DATA ADDR4 must live in the **same SBUF quadrant** — the hardware gather engine indexes within one quadrant. `indirect_quadrant_check_src4d` (`0x14484a0`) is the 20-byte twin: it reads the marker at struct byte `+0xB` and runs the same PSUM-window + same-quadrant test on the `index@+8` / `data@+0xC` region of the `MEM_PATTERN4D` it is handed.

### The DMA-indirect path

`has_dma_indirect_valid_idx_start_addr` (`0x13707c0`, the DMA-engine indirect descriptor, opcode `0xBB`) validates the index ADDR4 (`start_addr_active_channels` + `tensor_start_addr_valid`), gates the data/secondary slots with a register-mode test (`(byte3 & 0xa0) == 0x80` reject), and reads the `DMA_INDIRECT_FLAGS` (`& 3 ∈ {1,2}`) to select the load/save direction.

### Byte agreement (both ways)

- The encoder OR's bit-29 onto the INDEX ADDR4 byte 3 (`or [B+3],0x20`); every decoder reads `(byte3 >> 0x18) & {0x60, 0xE0} == 0x20` on that **same byte** to select the indirect arm. (`assignAccess3D/4D/MX +3` ↔ `mxmem1d_valid` `shr 0x18; and 0x60` ↔ `quadrant_check_src3d/4d` `and 0xE0`.)
- The encoder writes `INDEX@+0` / `DATA@+4` (/ `SCALE@+8`); the decoders re-validate those exact ADDR4 offsets via `tensor_start_addr_valid` and read `index@+0` vs `data@+4` (`src3d`) / `index@+8` vs `data@+0xC` (`src4d`) for the quadrant equality.
- The MX `x4-NUM@+0xC` and `scale-part@+0xF` the encoder writes are exactly where `mxmem1d_valid`'s partition/region tests read.
- The three (MX) / two (non-MX) dynamic-offset asserts the encoder runs guarantee the emitted ADDR4s carry **no** register-mode byte — so the decoders' register-mode gates (`& 0xa0 == 0x80` reject) never fire on a legally-encoded gather.

> **GOTCHA — the `+0x90` vtable slot is `getNumIndirectIndices`, not `getNumElementsPerPartition`.** The two are easy to conflate, but `libBIR.so` separates them cleanly: the base `getNumIndirectIndices() @0x20fed0` is `31 c0 c3` — `xor eax,eax; ret`, a virtual overridden in `PhysicalAccessPattern` / `SymbolicAccessPattern` — while `getNumElementsPerPartition() const @0x202060` is a **non-virtual** `add rdi,0x50; jmp …` accessor of `Pattern+0x50`, an entirely different function that occupies no vtable slot. Since the indirect branch is reached only when `isTensorIndirectDynamicAP() == true`, the count read there is the **gather index count**.

## CoreV2 / CoreV4 and arch-universality

The indirect encoders are CoreV4-family (`assignAccess3D/4D` `0x150ccf0`/`0x150d8e0`, `assignIndirectPatternForMX` `0x150de90`). CoreV2 has its own `assignAccess3D/4D` (`0x1176de0`/`0x1177620`) for the CoreV2 indirect opcode family (`IndirectCopy` `0xE7`, `IndirectSaveAccumulate` `0xCA`, DMA-indirect `0xBB`). **MX descriptors are CoreV4-only** — the `MXMEM`/`MXINDIRECT16B` and `QuantizeMx` encoders are gated `core_v4`. Each ADDR4 word's internal bit packing (29-bit address / region class / register-mode bit / per-dtype alignment) is `assignStartAddr<ADDR4>`'s job (see [ADDR4](addr4.md) 2.2), shared with every CoreV4 op; the indirect encoders add only the descriptor-level bit-29 marker on the INDEX ADDR4 and the `{num, scale-part}` control fields. The static siblings `assignStaticPattern<TENSOR3D/4D>` supply the stride/num arrays on the non-indirect branch *only*; the indirect branch leaves that region inert because the index vector is the addressing source.

> **NOTE — `archModel[+0x38]` (granularity mask) and `archModel[+0x64]` (PE_count) are constructor constants.** They live in the per-arch Target constructors compiled into `libwalrus.so`, not in any JSON config. The page treats them as opaque per-arch constants; a reimplementation must source the same granularity mask and PE count from its own arch model. Their roles are read from the encoder's uses of those offsets; the constants themselves were not decoded out of the Target constructors.

## Evidence anchors

`libwalrus.so` / `libBIR.so` cp310; VA == file offset; `objdump -M intel` / IDA.

**Encoders.** `assignAccess3D` (`INDIRECT16B`) `0x150ccf0`: fork `call [r12+0x80]; test al,al; je` (`0x150cd35`); INDEX@+0 (`0x150cdc6`); DATA@+4 (`0x150cdda`); `call [r12+0x90]` (`0x150cdef`); `mov [rcx+8],ax` NUM@+8 — **no `shl`** (`0x150ce0b`); `or [rax+3],0x20` (`0x150ced8`); `ret` (`0x150ceea`); static fallback `jmp assignStaticPattern<TENSOR3D>` (`0x150cf0b` → `0x150c390`); context-name rodata `0x1d71fa8`. — `assignAccess4D` (`INDIRECT20B`) `0x150d8e0`: same shape; `or [rax+3],0x20` (`0x150dac8`); static fallback `jmp assignStaticPattern<TENSOR4D>` (`0x150dafb` → `0x150cf60`); context-name `0x1d72038`. — `assignIndirectPatternForMX<MXINDIRECT16B>` `0x150de90`: INDEX@+0 / DATA@+4 / SCALE@+8 (`a4=1`, `mov ecx,1` `0x150e0c5`); `div rcx; mov [rax+0xF],dl` scale-part (`0x150e0e7`/`ee`); x4 gate `cmp edx,2 / sub edx,8 / cmp edx,1 / ja` (`0x150e10e–11c`); `shl eax,2` (`0x150e11e`); `mov [r15+0xC],ax` x4-NUM (`0x150e139`); `or [rax+3],0x20` (`0x150e26b`).

**Assert strings (rodata, verbatim, all present).** `"must be a Tensor Indirect AP"` (non-MX); `"DataAP must be a Tensor Indirect AP"` (MX); `"MX DataAP and ScaleAP are not from the same parent instruction"`; `"If the argument of matmult MX is indirect, the corresponding scale should also have indirection"`; `"Furthermore, the corresponding scale must have exactly the same indirection as the argument"`; `"TODO: support Tensor Indirect Indices AP to have dynamic offset"`; `"TODO: support Tensor Indirect Data AP to have dynamic offset"`; `"TODO: support MX scale AP to have dynamic offset"`.

**Decoders.** `mxmem1d_valid` `0x1447190`: `shr r14,0x18; and 0x60; cmp 0x20` (`0x144719c–b4`). — `indirect_quadrant_check_src3d` `0x1448450`: `shr eax,0x18; and 0xE0; cmp 0x20` (`0x1448457–5d`), PSUM-window `cmp 0x3FFFFF` (`0x1448480`), `jmp addresses_in_same_sbuf_quadrant` (`0x1448491`). — `indirect_quadrant_check_src4d` `0x14484a0` (marker byte `+0xB`). — `has_dma_indirect_valid_idx_start_addr` `0x13707c0`.

**libBIR AP accessors.** `getNumIndirectIndices` base `0x20fed0` = `31 c0 c3` (`xor eax,eax; ret`); `isTensorIndirectDynamicAP` `0x20feb0`; `getTensorIndirectIndicesAP` `0x20fe80`; `getNumElementsPerPartition() const` `0x202060` (`add rdi,0x50; jmp` — DISTINCT, not the `+0x90` slot). `PhysicalAccessPattern` overrides at `0x175100`/`0x17bee0`/`0x179c60`.

**Ops/kernels.** `bir::InstGather` / `CoreV2GenImpl::visitInstGather` `0x12532e0`; `bir::InstIndirectCopy` / `visitInstIndirectCopy` `0x1275c40`; `klr::NcNGather` / `klr::NcLocalGather` (RTTI present); `backend::ExpertMLPsTKGKernel` (RTTI `0x1de7b60`, vtable `0x3d8fb40`); `codegenIndirectLoadSave` `0xb580e0`. `MXINDIRECT20B`: **zero occurrences** in the binary (confirms 1-D-only MX gather).

## Cross-References

- [ADDR4 — the 32-Bit Address Word](addr4.md) (2.2) — the bit-29 indirect mode (`0x20`) the INDEX ADDR4 carries, and the `a4` data-vs-scale resolver fork the SCALE word uses; this page's `or [B+3],0x20` is the marker ADDR4 §"the mode nibble" pins as `0b01`.
- [TENSOR1D / 2D / 3D Descriptors — the 4+4N Rule](tensor-descriptors.md) (2.3) — the `TENSOR3D`/`TENSOR4D` static patterns whose slot widths name the 16B/20B variants and whose static fill the non-indirect branch emits.
- [MEM_PATTERN2D / 3D — the DST/PSUM Role](mempattern-2d-3d.md) (2.5) — the `MEM_PATTERN` family these indirect records are validated as.
- *MXMEM_PATTERN1D — the MX twin* (2.6, planned) — the plain MX 1-D pattern (`data@+0` / `scale@+4` / `num@+8`) that `MXINDIRECT16B` shifts `+4` by inserting the INDEX ADDR4.
- *Penguin middle-end — `TensorCopyDynamic`* (5.24, [Part 5](../penguin/)) — the dynamic-copy op that lowers to the indirect gather/scatter family.
- *libwalrus backend — `lower_dynamic_dma` / DGE* (Part 8, [walrus](../walrus/)) — the DMA-indirect lowering and the data-gather-engine path the `MXINDIRECT16B` MoE gather feeds.
