# CUSTOM_OP Wire Byte-Layout

> *All symbols, addresses, and byte offsets on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 share an identical `libwalrus.so` `.text`, with a fixed per-build address delta). The encoder, validators, and bounded field-setters all live in `libwalrus.so` (file offset == VA; PT_LOAD 1:1). The BIR node `bir::InstCustomOp` and the `ModuleArtifactInfo` registry live in `libBIR.so` (linked into `libwalrus.so`). Evidence anchor: **D-Q08** (mined from the `CoreV2GenImpl::visitInstCustomOp` body and the `core_v2` custom-op validators). Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

A `bir::InstCustomOp` — the compiler's escape hatch for an arbitrary user kernel that runs on the 8-core Xtensa GPSIMD CPU cluster — does **not** encode to a single 64-byte ISA word. It encodes to a **chain** of them: one `CUSTOM_OP_HEADER` (opcode `0x85`) followed by one or more `CUSTOM_OP_PAYLOAD` words (opcode `0x86`), one payload per operand access pattern. The header carries the *counts and the kernel identity* (a `CustomOpFunctionId` byte plus `num_payloads` and `num_arguments`); each payload carries *one operand's access pattern*. The first payload is always the output AP; the remaining `N` payloads are the `N` input/argument APs, in order. The runtime reads `num_payloads` off the header to know how many `0x86` words to consume before the next instruction.

The two opcodes share the entire 64-byte-bundle skeleton with every other Tonga ISA instruction — `pxor`-zeroed in full, header-stamped by `setupHeader` (so `byte[0]=opcode`, `byte[1]=0x10`, giving the 16-bit instruction word `0x1085`/`0x1086`), a shared per-instruction header-init band, then the family-specific fields, then `fwrite(buf, 1, 0x40, bin)`. The lifecycle is documented in [2.1 The 64-Byte Instruction Bundle](../isa/instruction-bundle.md); this page maps only the **custom-op-specific** bytes: the header's three count/id fields and the payload's present-flag + ADDR4 access-pattern record.

The bar for this page: a reader can **byte-encode a `CUSTOM_OP_HEADER` / `CUSTOM_OP_PAYLOAD` pair by hand** — knowing each field's offset, width, value source, and the exact `num_payloads = N+2` arithmetic — and reconstruct the full word chain for a custom op with one output and `N` inputs. Every offset is pinned against a literal store in the `visitInstCustomOp` body (the `LOBYTE`/`LOWORD` opcode stamps, the `lea [r13+0Ch]` / `lea [r13+0Fh]` bounded setters that stamp `num_payloads` and `num_arguments`) or against the `core_v2` validator that re-checks it.

For reimplementation, the contract is:

- The **two-opcode chain** model: `0x85` header once, then `0x86` payload per operand AP (output first, then `N` args) — not a single self-describing word.
- The **header byte map**: `num_payloads` (u16 @ `+0x0C`), `CustomOpFunctionId` (u8 @ `+0x0E`), `num_arguments` (u8 @ `+0x0F`), zero @ `+0x10` — a contiguous count-band at `+0x0C..+0x10`.
- The **`num_payloads = N + 2`** arithmetic and what the `+2` accounts for.
- The **payload byte map**: present-flag (`1` @ `+0x0F`) + one ADDR4-rooted access-pattern descriptor @ `+0x10`.

| | |
|---|---|
| **Sole encoder** | `neuronxcc::backend::CoreV2GenImpl::visitInstCustomOp` @ `0x12613c0` |
| **Header opcode** | `0x85` (133) → 16-bit word `0x1085` (4229) |
| **Payload opcode** | `0x86` (134) → 16-bit word `0x1086` (4230) |
| **Bundle size** | `std::array<u8,64>`; `inst_word_len = 0x10` (16 dwords) |
| **Words per op** | `1 + num_payloads`; `num_payloads = N + 2` (N = `num_arguments`) |
| **Header validators** | `core_v2::is_valid_custom_op_header` @ `0x129b940`; `dbg_…` @ `0x129ba90` |
| **Payload validator** | `core_v2::dbg_is_valid_custom_op_payload` @ `0x129c610` |
| **Arch gate** | `birverifier::checkCustomOp` @ `0xfdbf90` — Sunda(20) ∨ Tonga(10) only |
| **Kernel identity** | `CustomOpFunctionId` (u8) → `ModuleArtifactInfo` lib/function side-table |
| **Evidence** | D-Q08 (`visitInstCustomOp` body + `core_v2` validators) |

> **NOTE — There is exactly one encoder.** `nm -DC` finds no `CoreV3`/`CoreV4` override of `visitInstCustomOp`; the gen3/gen4 backends inherit the `CoreV2` base path verbatim. Custom-op encoding is arch-shared. (The op is *gated* to Sunda/Tonga arch by a separate verifier, but the wire bytes are produced by the single CoreV2 emitter.)

---

## 1. The chain model — header once, payload per operand

A custom op is the only TPB instruction that emits a variable-length **chain** of 64-byte words. The shape is fixed by the operand count `N` (= `num_arguments`, the length of the `InstCustomOp` argument list at `inst+0xA0..+0xA8`):

```text
   CUSTOM_OP_HEADER   (0x85)   ── counts + FunctionId, no AP
   CUSTOM_OP_PAYLOAD  (0x86)   ── OUTPUT access pattern        (payload 0)
   CUSTOM_OP_PAYLOAD  (0x86)   ── INPUT arg[0] access pattern  (payload 1)
   CUSTOM_OP_PAYLOAD  (0x86)   ── INPUT arg[1] access pattern  (payload 2)
        …                                                       …
   CUSTOM_OP_PAYLOAD  (0x86)   ── INPUT arg[N-1] access pattern (payload N)
   ── next instruction ──
```

Total 64-byte words on the wire = `1 (header) + (N + 1) (payloads) = N + 2`. The header's `num_payloads` field is set to `N + 2` (§2.2). The decoder reads `num_payloads` off the `0x85` header, then consumes that many `0x86` words before resuming the instruction stream.

The emitter's per-argument loop walks the `InstCustomOp` argument list and emits one `0x86` word each:

```c
/* CoreV2GenImpl::visitInstCustomOp @0x12613c0 — chain skeleton (annotated C) */
void visitInstCustomOp(InstCustomOp &inst) {
    int N = arg_list_length(inst);                  /* inst+0xA0..+0xA8 (arg vector) */

    /* ---- one CUSTOM_OP_HEADER (0x85) ---- */
    array<u8,64> hdr = {0};                          /* emplace_back + pxor-zero      */
    setupHeader(&hdr, /*opcode=*/0x85);              /* hdr[0]=0x85, hdr[1]=0x10      */
    customop_header_init(&hdr, inst);                /* sub_122ED00 — shared band     */
    set_u16(&hdr[0x0C],/*name=*/"instr.num_payloads",  N ? N + 2 : 1);   /* sub_123CA60: lea [r13+0Ch] */
    hdr[0x0E] = function_id;                         /* CustomOpFunctionId (u8)       */
    set_u8 (&hdr[0x0F],/*name=*/"instr.num_arguments", N);              /* sub_1247BF0 */
    hdr[0x10] = 0;
    emit(&hdr);                                       /* fwrite 0x40 + COLLECT 133     */

    /* ---- one CUSTOM_OP_PAYLOAD (0x86) per operand: output first, then args ---- */
    array<u8,64> p0 = {0};
    setupHeader(&p0, /*opcode=*/0x86);
    p0[0x0F] = 1;                                     /* present flag                  */
    encode_access_pattern(&p0[0x10], getOutput_AP(inst, 0));   /* sub_1210900: out AP */
    emit(&p0);                                        /* fwrite 0x40 + COLLECT 134     */

    for (int i = 0; i < N; ++i) {
        array<u8,64> pi = {0};
        setupHeader(&pi, /*opcode=*/0x86);
        pi[0x0F] = 1;
        encode_access_pattern(&pi[0x10], getArgument_AP(inst, i));  /* sub_1210900    */
        if (i == N - 1) customop_payload_terminator(&pi, inst);    /* sub_122EA40    */
        emit(&pi);
    }
}
```

> **GOTCHA — the kernel name and `.so` are NOT on the wire.** The ISA stream is name-free. The header carries only the numeric `CustomOpFunctionId` (byte `+0x0E`); the human-readable kernel/library name and the `libbuiltincustomop_cpu{0..7}.stripped.so` (or user `.so`) reference are registered separately into `bir::ModuleArtifactInfo` (`addCustomOpLibFile` / `addCustomOpFunction` / `allocateCustomOpFunctionId`), serialized into the NEFF metadata, and resolved by the runtime loader against the FunctionId. The FunctionId↔(libfile, function) binding is the subject of **11.7** (FunctionId binding — `custom-ops/`, planned). STRONG (D-Q08 — the `registerCustomOp`-tagged registration path and the `inst+0x110/+0xF0/+0x130/…` string reads).

---

## 2. The `CUSTOM_OP_HEADER` (0x85) byte map

The header is the only word that carries the op's *shape*. Its custom-op-specific payload is three small fields plus a zero byte, sitting on top of the shared bundle header (`+0x00..+0x03`) and the shared per-instruction init band written by `customop_header_init` (`sub_122ED00`).

| off | Sz | field | value | source / store | Conf |
|---|---|---|---|---|---|
| `+0x00` | 1 | **opcode** = `0x85` (133) | const | `setupHeader`; `LOBYTE(hdr[0]) = -123` | CONFIRMED |
| `+0x01` | 1 | `inst_word_len` = `0x10` | const | `setupHeader` `a2[1]=16` → word `0x1085` | CONFIRMED |
| `+0x02` | 2 | reserved = `0` | const | `setupHeader` / `pxor`-zero | CONFIRMED |
| `+0x04` | 8 | header-init band (engine / sync / neuron-events) | per-inst | `customop_header_init` (`sub_122ED00`) | STRONG |
| `+0x0C` | 2 | **`num_payloads`** (u16) = `N + 2` | computed | `set_u16(hdr+0x0C, …)` `sub_123CA60` `"instr.num_payloads"`; dest `lea [r13+0Ch]` @0x1262f75 | CONFIRMED |
| `+0x0E` | 1 | **`CustomOpFunctionId`** (u8) | registry | `allocateCustomOpFunctionId()` → `hdr[14]` | CONFIRMED |
| `+0x0F` | 1 | **`num_arguments`** (u8) = `N` | arg count | `set_u8(hdr+15, …)` `sub_1247BF0` `"instr.num_arguments"` | CONFIRMED |
| `+0x10` | 1 | reserved = `0` | const | explicit `hdr[16] = 0` | CONFIRMED |
| `+0x11..+0x3F` | 47 | reserved = `0` | const | `pxor`-zero (no store) | INFERRED |

### 2.1 The contiguous `+0x0C..+0x10` count-band

> **NOTE — `num_payloads`, `FunctionId`, and `num_arguments` form a contiguous count-band at `+0x0C..+0x10`, clear of the header-init band.** Every other TPB instruction treats `+0x04..+0x0B` as a generic 8-byte `SyncInfo` / header overlay; the custom-op header leaves that band intact and writes its three count/id fields immediately after it. The bounded u16 setter `sub_123CA60` stamps `num_payloads` at `hdr+0x0C` (dest `lea rdi,[r13+0Ch]`, machine bytes `49 8d 7d 0c` @0x1262f75), then `CustomOpFunctionId` at `+0x0E` and the bounded u8 setter at `+0x0F` (`lea rdi,[r13+0Fh]`, `49 8d 7d 0f` @0x1262fbe) follow in order. Because `+0x0C` is past `+0x0B`, none of these fields overwrites the init band — a reimplementer runs the shared header init, then stamps the count-band, with no ordering hazard between them. CONFIRMED — `sub_123CA60` is the `utils.h:292` bounded setter (width `0x10`, raises if `value != (u16)value`) and its target is `hdr+0x0C`; the field-name string `"instr.num_payloads"` is verbatim `.rodata`.

> **CORRECTION (D-Q08) — `num_payloads` is at `+0x0C`, not `+0x06`.** An earlier draft of this page placed `num_payloads` at header `+0x06`. That was a decompiler-misread: Hex-Rays renders the store as `sub_123CA60((_WORD*)hdr + 6, "instr.num_payloads", …)`, and `hdr` is typed `_WORD*` (u16), so `+ 6` is **u16-pointer arithmetic = 6 × 2 = +0x0C bytes**, not a `+6`-byte offset. The byte-proven truth is `+0x0C`: the setter's destination is `lea rdi,[r13+0Ch]` (machine bytes `49 8d 7d 0c`) at 0x1262f75 in the encoder body (`visitInstCustomOp` @0x12613c0), with the COLLECT scratch arm at 0x1263201 stamping the same `[…+0Ch]`. The cross-check that proves it: the sibling field `num_arguments` is stored via a genuine `_BYTE*` `+ 15` → `lea [r13+0Fh]` → byte `+0x0F` — same `sub_123CA60`/setter family, *different pointer type*, which is exactly what flipped only `num_payloads`. The sibling [2.22 Collective/GPSIMD/CustomOp Encoding](../isa/collective-customop-encoding.md) §10 map (which placed `num_payloads` at `+0x0C`) is **CORRECT**; this page now agrees with it.

### 2.2 The `num_payloads = N + 2` arithmetic — what the `+2` is

`num_payloads` is **not** the number of arguments. It is the number of `0x86` payload words that follow the header:

```text
   num_payloads = N + 2
                = 1 (output AP payload)        ← payload 0, always present
                + N (input/argument AP payloads)
                + 1 (header word counted in the total)   ← see note
```

The cleanest decomposition consistent with the chain (§1) and the emitter's `N ? N+2 : 1` rule:

- `N` argument payloads — one `0x86` per input.
- `+1` for the **output** access pattern (payload 0, always emitted even for a single-output op).
- `+1` for the **header** word itself — `num_payloads` is the *total chunk count* of the instruction (header + all payloads), so a downstream chunk-reader advances exactly `num_payloads` 64-byte words to reach the next instruction.

> **NOTE — the `else 1` branch.** When `N == 0` (a custom op with no input arguments), the emitter sets `num_payloads = 1`, not `0 + 2 = 2`. This is the degenerate "header-only-ish" count for the no-argument case (the chunk-walker still needs a non-zero advance). The `(#args + 2) if args present, else 1` rule is read directly from the emitter; the exact semantics of the zero-argument / scalar-fill path (whether an output payload is still emitted when `N==0`) is the one OPEN edge in D-Q08 — treat `num_payloads = N + 2` as the rule for the common `N ≥ 1` case and `1` for `N = 0`. CONFIRMED (the `N+2` / `1` values); INFERRED (the precise per-chunk decomposition of the `+2`, since the runtime chunk-walker is not in `libwalrus`).

> **GOTCHA — `num_arguments` counts inputs only; the output is separate.** `num_arguments` (byte `+0x0F`) = the length of the `InstCustomOp` argument list (inputs). The single output is *not* counted in `num_arguments` — it rides payload 0. So a 3-input / 1-output op has `num_arguments = 3` and `num_payloads = 5` (3 inputs + 1 output + 1 header). A reimplementer must keep the two counters distinct: `num_arguments = N`, `num_payloads = N + 2`. CONFIRMED — the emitter counts arguments (`inst+0xA0..+0xA8` list) and the single output separately; the `"Custom ops cannot have more than 1 output"` legality check (`.rodata`) confirms the at-most-one-output invariant.

---

## 3. The `CUSTOM_OP_PAYLOAD` (0x86) byte map

Each payload is a near-empty bundle carrying a **present-flag** and **one** access-pattern descriptor. The same `0x86` layout is reused for the output AP (payload 0) and for every input arg AP — only the AP source differs.

| off | Sz | field | value | source / store | Conf |
|---|---|---|---|---|---|
| `+0x00` | 1 | **opcode** = `0x86` (134) | const | `setupHeader`; `LOBYTE(pl[0]) = -122` | CONFIRMED |
| `+0x01` | 1 | `inst_word_len` = `0x10` | const | `setupHeader` `a2[1]=16` → word `0x1086` | CONFIRMED |
| `+0x02` | 2 | reserved = `0` | const | `setupHeader` / `pxor`-zero | CONFIRMED |
| `+0x04` | 8 | header-init band | per-inst | shared header overlay | STRONG |
| `+0x0F` | 1 | **present flag** = `1` | const | explicit `pl[0x0F] = 1` | CONFIRMED |
| `+0x10` | 48 | **access-pattern descriptor** (ADDR4-rooted) | operand AP | `encode_access_pattern(pl+0x10, …)` `sub_1210900` | CONFIRMED (store); STRONG (AP internals) |

### 3.1 The present flag

The byte at `+0x0F` is stamped to `1` on every payload. It marks the payload slot as occupied — the consumer treats `+0x0F == 1` as "this `0x86` word carries a valid AP". The payload validator `dbg_is_valid_custom_op_payload` (`0x129c610`) branches on `byte0 == 0x86` and validates the descriptor/event fields; the present flag is the per-payload occupancy marker. CONFIRMED — the `pl[0x0F] = 1` store is in the emit path of every payload arm.

> **NOTE — the present flag occupies the *same byte offset* (`+0x0F`) that the header uses for `num_arguments`.** This is not a collision: they are different opcodes (`0x85` vs `0x86`) with independent field meanings at `+0x0F`. In the header, `+0x0F` = `num_arguments` (u8 count); in the payload, `+0x0F` = present flag (`1`). A decoder dispatches on `byte[0]` first, then interprets `+0x0F` per-opcode.

### 3.2 The access-pattern descriptor — ADDR4-rooted, at `+0x10`

The payload's body (`+0x10` onward, up to 48 bytes) is **one** operand access pattern, encoded by the generic AP packer `sub_1210900`. The AP is the same on-chip tensor memory-pattern descriptor the rest of the ISA uses: it **opens with a 32-bit ADDR4 start-address word** (see [2.2 ADDR4 — the 32-Bit Address Word](../isa/addr4.md)), followed by the per-dimension loop words (`TENSOR1D`/`2D`/`3D`/`4D` shape, depending on the operand rank). The ADDR4 word at `+0x10` packs the operand's SBUF/HBM byte address, its mode nibble, and (for a register-mode AP) a colored-register id.

- **Payload 0** encodes the **output** AP: `getOutput<AccessPattern>(inst, 0)`.
- **Payload `i+1`** (for `i ∈ [0, N)`) encodes the **`i`-th input** AP: `getArgument<AccessPattern>(inst, i)`.

```c
/* CUSTOM_OP_PAYLOAD body @ +0x10 — one AP descriptor (sub_1210900) */
encode_access_pattern(&payload[0x10], operand_AP);
/*  payload[0x10..0x13] = ADDR4 word    (start addr; see 2.2 addr4.md)        */
/*  payload[0x14..]     = TENSORnD loop words (per-dim num/step; see 2.3)     */
```

> **GOTCHA — a custom-op operand AP cannot be a `TensorIndirect` (gather) AP.** The emitter raises `"Hardware Restriction: CUSTOM_OP instruction cannot have TensorIndirect AP"` (`.rodata`). So the ADDR4 word at `+0x10` is always a static or register-mode address (mode nibble `0b00` or RM=1) — never the indirect-gather mode (`0x20`). All operands must additionally be located in SBUF or HBM (`"All args to a customop must be located in SBUF or HBM"`, `"All of a customop's outputs must be located in SBUF or HBM"`). CONFIRMED — the three legality strings are verbatim in the emitter's `.rodata`.

> **NOTE — the AP descriptor internals are out of scope here.** The 48-byte AP layout (ADDR4 + the `TENSORnD` loop words, the per-dtype alignment, the SBUF/PSUM region split) is the subject of [2.2 ADDR4](../isa/addr4.md) and [2.3 Tensor Descriptors](../isa/tensor-descriptors.md). This page pins only that the payload's AP starts at `+0x10` and is ADDR4-rooted; the per-field bit map of the AP itself is deferred to those pages. D-Q08 leaves the per-field AP descriptor of `sub_1210900` OPEN (the AP-per-payload structure and the output-first ordering are confirmed; the internal offsets of the descriptor are the AP pages' subject).

---

## 4. Worked byte-encoding example — one output, one input (N = 1)

Encode a custom op with `CustomOpFunctionId = 0x07`, one input argument and one output, both small SBUF tiles. `N = num_arguments = 1`, so `num_payloads = N + 2 = 3`, and the chain is **three** 64-byte words: header, output payload, input payload.

```text
WORD 0 — CUSTOM_OP_HEADER (0x85), 64 bytes, little-endian:

 off : 00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F
 val : 85 10 00 00 [HH HH HH HH HH HH HH HH] 03 00 07 01
        │  │  └──┘  └──── header-init band ────┘  │  │  │  │
        │  │   resvd  (sub_122ED00 fills +04..+0B,│  │  │  └ num_arguments = 0x01 (N=1)
        │  │          left intact — count-band    │  │  └─── CustomOpFunctionId = 0x07
        │  │          starts past it at +0x0C)    │  └────── num_payloads hi = 0x00
        │  └ inst_word_len = 0x10 (word = 0x1085) └───────── num_payloads lo = 0x03
        └─── opcode = 0x85 (133)

   +0C..0D (num_payloads, u16 LE) = 03 00   ← N + 2 = 3   ⟵ count-band, clear of the init band
   +0E     (CustomOpFunctionId)   = 07
   +0F     (num_arguments)        = 01
   +10     = 00 ; +11..3F = 00 (pxor-zero)

WORD 1 — CUSTOM_OP_PAYLOAD (0x86), the OUTPUT AP:

 off : 00 01 02 03 04..0B   0F   10 11 12 13  14..
 val : 86 10 00 00  ……   …  01   <ADDR4 out> <TENSORnD loop words…>
        │  │              │
        │  │              └ present flag = 1   (byte +0x0F)
        │  └ word = 0x1086
        └─── opcode = 0x86 (134)

   +10..13 = ADDR4(output tile)  (start addr; mode 0b00 static or RM=1)
   +14..   = TENSORnD loop words for the output operand

WORD 2 — CUSTOM_OP_PAYLOAD (0x86), the INPUT arg[0] AP:

 off : 00 01 02 03 …  0F   10 11 12 13  14..
 val : 86 10 00 00 …  01   <ADDR4 in0> <TENSORnD loop words…>

   +10..13 = ADDR4(input arg[0] tile)
   +14..   = TENSORnD loop words for arg[0]
```

The runtime: reads `byte[0]=0x85` → custom-op header; reads `num_payloads=3` @ `+0x0C` → consume 2 more `0x86` words (payload 0 = output, payload 1 = arg[0]); resolves `CustomOpFunctionId=0x07` against the NEFF `ModuleArtifactInfo` registry to find the kernel `.so` and entry function; dispatches the kernel on the GPSIMD Xtensa cluster with the output and input tiles addressed by the two payload APs.

> **NOTE — bytes marked `HH` are header-init-band content, not free.** The `+0x04..+0x0B` band is filled by `customop_header_init` (`sub_122ED00`) with the shared engine/sync/neuron-events fields, and the count-band that follows (`num_payloads` @ `+0x0C`, `FunctionId` @ `+0x0E`, `num_arguments` @ `+0x0F`, zero @ `+0x10`) sits entirely past it — no init-band byte is overwritten by a count-band stamp. For a clean reimplementation, run the shared header init over `+0x04..+0x0B`, then stamp `num_payloads`, `FunctionId`, `num_arguments`, and the `+0x10` zero into the contiguous `+0x0C..+0x10` band. The validators (`is_valid_custom_op_header`) only assert `byte1 == 0x10`, `byte0 == 0x85`, `LOWORD == 0x1085`, valid neuron-events, and high descriptor bits zero — they do not constrain the `+0x04..+0x0B` init bytes.

---

## 5. Validation — what the round-trip checker requires

The `RUN_ISA_CHECKS` codegen mode re-encodes the same words onto scratch and runs a vtable-dispatched checker. The custom-op checkers:

```c
/* core_v2::is_valid_custom_op_header @0x129b940 */
bool is_valid_custom_op_header(word) {
    require(BYTE1(word) == 0x10);          /* inst_word_len high byte           */
    require(is_valid_enum(4, byte0));      /* opcode is a known opcode-class-4   */
    require(byte0 == 0x85);                /* the header opcode                  */
    require(has_valid_neuron_events(…));   /* the header-init events band        */
    require(high_descriptor_bits == 0);
    /* canonical LOWORD = 0x1085 = 4229 */
}

/* core_v2::dbg_is_valid_custom_op_payload @0x129c610 */
bool dbg_is_valid_custom_op_payload(word) {
    branch(byte0 == 0x86);                 /* the payload opcode                 */
    validate_payload_descriptor_and_events(…);
}
```

| Checker | Address | Asserts | Conf |
|---|---|---|---|
| `is_valid_custom_op_header` | `0x129b940` | `byte0==0x85`, `byte1==0x10`, `LOWORD==0x1085`, valid events | CONFIRMED |
| `dbg_is_valid_custom_op_header` | `0x129ba90` | mirror (debug build) | CONFIRMED |
| `dbg_is_valid_custom_op_payload` | `0x129c610` | `byte0==0x86`, descriptor/events | CONFIRMED |

These wire-validators — the full set, the codegen-mode plumbing, and the `RUN_ISA_CHECKS` vs `GENERATE_ISACODE` vs `COLLECT_OPCODES` three-way switch — are the subject of **11.6** (wire validators — `custom-ops/`, planned). This page documents only the two custom-op-specific checkers and the byte assertions they pin.

> **NOTE — three codegen modes, one encoder.** `visitInstCustomOp` switches on the codegen mode (`gen+0x270`): **GENERATE_ISACODE** (mode 1) emplaces the `array<u8,64>`, stamps, encodes, and `fwrite`s `0x40` bytes to the engine `.bin`; **RUN_ISA_CHECKS** (mode 2) encodes on scratch and runs the validators with no `fwrite`; **COLLECT_OPCODES** (mode 0) inserts the opcode int (`133` header / `134` payload) into the per-instruction opcode set and bumps a `map<u32,u32>` histogram. Any other mode raises `"Wrong CodeGenMode. It must be one of GENERATE_ISACODE, RUN_ISA_CHECKS, or COLLECT_OPCODES"`. The byte stamps are identical across GENERATE and RUN_ISA_CHECKS — the validator checks the same bytes the generator writes. CONFIRMED (D-Q08).

---

## 6. Legality constraints the emitter enforces

The encoder raises (`reportError`) on these conditions, all verbatim `.rodata` in the `visitInstCustomOp` body:

| Constraint | Error string | Conf |
|---|---|---|
| At most one output | `"Custom ops cannot have more than 1 output"` | CONFIRMED |
| Inputs in SBUF/HBM | `"All args to a customop must be located in SBUF or HBM"` | CONFIRMED |
| Outputs in SBUF/HBM | `"All of a customop's outputs must be located in SBUF or HBM"` | CONFIRMED |
| No gather AP | `"Hardware Restriction: CUSTOM_OP instruction cannot have TensorIndirect AP"` | CONFIRMED |
| ≤ 255 unique functions | `"Number of unique custom op functions cannot exceed "` + 255 | CONFIRMED |
| Arch is Sunda/Tonga | `"Arch == ArchLevel::Sunda \|\| Arch == ArchLevel::Tonga"` (`checkCustomOp @0xfdbf90`) | CONFIRMED |

> **GOTCHA — `CustomOpFunctionId` is a u8, capped at 255 unique functions.** `allocateCustomOpFunctionId()` hands out ids `0..254`; the `ModuleArtifactInfo` function-count guard (`+456`) raises `"Number of unique custom op functions cannot exceed 255"` on overflow. The id is a single byte (`hdr+0x0E`), so a NEFF can reference at most 255 distinct custom-op functions. The id-to-(libfile, function-name) map is NEFF metadata, resolved by the runtime — the wire only carries the byte. CONFIRMED — the u8 width of `hdr[0x0E]` and the `allocateCustomOpFunctionId` `0..254` range both pin the 255 ceiling.

> **GOTCHA — the GPSIMD here is the Xtensa CPU cluster, NOT the Pool-alias "GPSIMD" mover.** The `0x85`/`0x86` pair dispatches a kernel onto the programmable 8-core Xtensa GPSIMD CPU array ([11.1 The GPSIMD CPUs: 8-core Xtensa ELF Layout](gpsimd-xtensa-layout.md)). That is a different subsystem from the `InstGPSIMDSB2SB` (`0xBF`) Pool-alias cross-core SBUF↔SBUF mover documented in [2.22 §5](../isa/collective-customop-encoding.md) and [1.13 GPSIMD Engine](../arch/gpsimd-engine.md). The three-way "GPSIMD" name collision is resolved in full on the [1.13](../arch/gpsimd-engine.md) page; the custom-op pair on this page is the Xtensa-cluster dispatch, and SORT (and arbitrary user NKI kernels) are its canonical occupants ([11.2 Bitonic SORT/TOPK](bitonic-sort-topk.md)).

---

## 7. Confidence ledger

- **CONFIRMED** (byte-store / opcode / validator disassembled in the `visitInstCustomOp` body and the `core_v2` checkers): both opcodes (`0x85`=133 / `0x86`=134, from `LOBYTE = -123 / -122`, COLLECT records 133/134, validators `v12==-123 / v10==-122`); the `0x1085`/`0x1086` 16-bit words (`setupHeader a2[1]=16`, validator `LOWORD=4229`); `num_payloads` u16 @ `+0x0C` (`sub_123CA60`, dest `lea [r13+0Ch]` `49 8d 7d 0c` @0x1262f75, `"instr.num_payloads"`); `CustomOpFunctionId` u8 @ `+0x0E`; `num_arguments` u8 @ `+0x0F` (`sub_1247BF0`, dest `lea [r13+0Fh]` `49 8d 7d 0f` @0x1262fbe, `"instr.num_arguments"`); the `hdr[0x10]=0` store; the payload present flag `=1` @ `+0x0F`; the AP-per-payload structure (output via `getOutput`, args via `getArgument`, encoded by `sub_1210900` @ `+0x10`); the `num_payloads = N+2 | 1` rule; all six legality strings + the 255-function u8 cap; the Sunda/Tonga arch gate.
- **STRONG**: the `+0x04..+0x0B` header-init band sub-layout (shared overlay written by `sub_122ED00`, not re-decoded here); the AP descriptor being ADDR4-rooted (cross-referenced to [2.2 ADDR4](../isa/addr4.md), not independently store-pinned in this body); the FunctionId↔registry binding path (`registerCustomOp` tag + string reads, the subject of 11.7).
- **INFERRED / DEFER**: the precise per-chunk decomposition of the `+2` (header vs output vs the runtime chunk-walker's advance semantics, since the walker is not in `libwalrus`); the `N == 0` zero-argument edge (`num_payloads = 1`); the 48-byte AP descriptor internals written by `sub_1210900` ([2.2 ADDR4](../isa/addr4.md), [2.3 Tensor Descriptors](../isa/tensor-descriptors.md)); the `+0x0C..+0x0D` init-band byte meanings.

The encoder body is authoritative for every offset cited. No SPECULATIVE claims.

## Cross-References

- [2.22 Collective / GPSIMD / CustomOp Encoding](../isa/collective-customop-encoding.md) — the sibling ISA-encoding page; its §10 first mapped the `0x85`/`0x86` pair and correctly placed `num_payloads` at `+0x0C`; this page gives the full byte-encoding contract and agrees on `+0x0C`.
- [2.1 The 64-Byte Instruction Bundle & Header Skeleton](../isa/instruction-bundle.md) — the `pxor`-zero / `setupHeader` / `fwrite(0x40)` lifecycle every word here shares.
- [2.2 ADDR4 — the 32-Bit Address Word](../isa/addr4.md) — the access-pattern start word that opens every payload's operand descriptor at `+0x10`.
- [2.3 Tensor Descriptors](../isa/tensor-descriptors.md) — the `TENSORnD` loop words that follow the ADDR4 word in the payload body.
- [11.1 The GPSIMD CPUs: 8-core Xtensa ELF Layout](gpsimd-xtensa-layout.md) — the Xtensa CPU cluster these `0x85`/`0x86` words dispatch a kernel onto.
- [11.2 The Bitonic SORT / TOPK Builtin Algorithm](bitonic-sort-topk.md) — SORT, the canonical custom-op occupant (no dedicated opcode; rides `0x85`/`0x86`).
- **11.6 Custom-Op Wire Validators** *(custom-ops/, planned)* — the full `is_valid_custom_op_*` checker set and the codegen-mode plumbing.
- **11.7 CustomOpFunctionId Binding** *(custom-ops/, planned)* — how the header's FunctionId byte maps to a `(libfile, function-name)` via `ModuleArtifactInfo`.
- [1.13 GPSIMD Engine — the Pool-Alias Cross-Core SB2SB Mover](../arch/gpsimd-engine.md) — the *other* "GPSIMD" (the `0xBF` Pool-alias mover); resolves the three-way name collision.
