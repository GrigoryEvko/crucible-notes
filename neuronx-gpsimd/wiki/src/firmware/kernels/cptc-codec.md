# The CPTC Compressed-Tensor Codec Family

**CPTC** ("Computed Permutation Trellis Coding") is the GPSIMD device's
sub-byte compressed-tensor format, and this page documents both the on-wire
**format** and the firmware **decode algorithm**. The codec lives in one carved
EXTISA image (`CAYMAN_EXTISA_3`), is reached through the dual-purpose
`ConvLutLoad` opcode `0xe4` (and the `0xf0`/spec-7 extended-instruction bridge),
and consolidates **six** hand-tuned decode kernels `cptc_decode_impl<1..6>`
behind a single `callx8 a3` dispatch ladder. The compressed tensor stores its
elements at a 1..7-bit sub-byte width in a **bit-plane-permuted** layout; the
decode de-interleaves the bit-planes with a fixed step-32 permutation table,
scales by per-block constants, and reconstructs to an fp/int output datapath.

This page delivers six things: (1) the **dispatch chain** byte-exact
(`0xe4` → `kernel_info_table` → `pool_conv_lut_load` → the 6-arm ladder → an
impl); (2) the **operand struct** `NEURON_ISA_TPB_S2_CONVLUT` (64 B,
compile-verified) and its `is_cptc` discriminator; (3) the **6 decode kernels**
with their per-impl sign/dtype/fp-width legs and the **odd-impl self-guard map**
(`impl<1>→1`, `impl<3>→4`, `impl<5>→3`; the even impls 2/4/6 carry no self-guard);
(4) the **six byte-identical de-interleave tables** (proven identical by hashing
the regions); (5) the **CPTC dtype codes** `0x19..0x1F` and their agreement with
the committed [datatype model](dtype-model.md); and (6) the **env-gate** and
**per-generation presence** (CAYMAN+; MAVERICK interiors observed only at the
header level).

Everything below is re-grounded **this pass** against the shipped binaries. The
codec body is carved out of `libnrtucode_internal.so` (host sha256
`b7c67e89…`, re-hashed MATCH this pass) by `dd` identity-map and disassembled
with the shipped device-native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`,
FLIX/VLIW); the operand struct and dtype enum are read byte-for-byte (and
compile-verified with `gcc`) from the in-package `arch-isa` headers; the
ladder reloc table, the de-interleave tables, the `kernel_info_table`, and the
`.xt.prop` function VMAs are parsed directly from the carved blob; the env-gate
and DEBUG self-naming strings are read from the host lib's string sections.
Confidence tags per [the Confidence & Walls model](../../reference/confidence-model.md):
`[HIGH/OBSERVED]` = read-from-byte / disasm / header / hash; `[MED/INFERRED]` =
reasoned over OBSERVED (often across a FLIX/literal-pool desync); `[…/CARRIED]` =
re-used at a sibling report's confidence without re-reading this pass. The FLIX
methodology backing the bundle reads is [FLIX decoding](../../reference/flix-decoding.md).

> **NOTE — the EXTISA codec body is hand-scheduled FLIX VLIW and desyncs a
> linear disassembler.** Stock `xtensa-elf-objdump`'s linear sweep loses byte
> alignment inside the impl bodies because hand-scheduled 512-bit bundles
> interleave literal/selector bytes. Where a fact below is byte-exact it is
> pinned at a *resynced* bundle, a scalar prologue, a `.xt.prop`/reloc record, or
> a `.rodata` table; where it sits **inside** a desynced interior it is flagged
> `[MED]` or `[LOW]` and the desync is named. The single hardest residual — the
> three **even-impl** served dtype values — is a genuine static-recovery **wall**:
> the validated FLIX bundle decoder reaches the *same* wall the device objdump
> does (§9).

---

## 1. Carve provenance and the EXTISA codec container

The CPTC codec is **not** a standalone `.so`. It is a raw, uncompressed
`EM_XTENSA` ELF32 blob embedded in the `.rodata` of the host shim
`libnrtucode_internal.so`, surfaced at runtime by an `*_EXTISA_3_SO_get` getter
stub. Two arch copies were carved this pass by `dd` identity-map and sha256-verified: [HIGH/OBSERVED]

| blob | host file off | size | sha256 (16) | ELF |
|------|--------------|------|-------------|-----|
| `CAYMAN_EXTISA_3` | `0x2fbf00` | `0x6974` | `052ac31c4e096212` | `EXEC`, entry `0x01003c74` |
| `MARIANA_EXTISA_3` | `0x595ae0` | `0x6974` | `8477ff2690f30cc3` | `EXEC` |

`readelf -SW` on the carved CAYMAN blob (re-read this pass): `.text` **VMA
`0x01000000` == file off `0x100`**, size `0x3d4a`; `.rodata` **VMA `0x02000000`
== file off `0x3e80`**, size `0x740`. The `.xt.prop` per-function sections name
the codec surface in clear — six `cptc_decode_impl<ILh1..6EE>` symbols plus the
dispatcher `pool_conv_lut_load`. MARIANA's `.text` is `0x14` bytes longer
(`0x3d5e`) and every codec body relocates uniformly **+0xC**; the `.rodata`
tables are byte-identical across the two gens. [HIGH/OBSERVED]

> **NOTE — VMA == file offset only for `.text`/`.rodata`.** In the carved blob
> `.text` VMA `0x01000000` maps to file `0x100`, and `.rodata` VMA `0x02000000`
> maps to file `0x3e80`. The `.text` *payload* offset is `VMA − 0x01000000`
> (i.e. `text.bin[off] == byte at VMA 0x01000000+off`). Do not apply the
> `ncore2gp` config-DLL `.data` delta (`0x200000`) here — these are `.text`/`.rodata`
> only, where VMA==fileoff in-section.

---

## 2. The dispatch chain — byte-exact

A model reaches the codec by issuing opcode `0xe4` (`ConvLutLoad`) — the **same**
opcode that legacy-loads the PE-array 4-bit conversion LUT. The host resolver
only **stages** the EXTISA_3 library (lib UID 3) when the env-gate is set (§10);
once staged, the device dispatch is:

```
opcode 0xe4 / spec 0   ──▶  kernel_info_table[idx7]  ──▶  pool_conv_lut_load @0x01002258   (the CPTC dispatcher)
opcode 0xf0 / spec 7   ──▶  kernel_info_table[idx8]  ──▶  ExtendedInstCptcDecode @0x01003b64 (the ext-inst bridge → same body)
```

### 2.1 The EXTISA_3 `kernel_info_table`

The 9-entry table at **VMA `0x020008c8` (file `0x4748`)**, 9 × 8-byte records of
`{b0=0, b1=0, spec, opcode, funcVA(LE,4)}`, re-decoded byte-exact this pass: [HIGH/OBSERVED]

| idx | opcode | spec | funcVA | role |
|----:|:------:|:----:|--------|------|
| 0 | `0x7e` | 0 | `0x01000080` | iota |
| 1 | `0x7c` | 0 | `0x010003f8` | clr arith |
| 2 | `0x7d` | 0 | `0x01000410` | clr bitvec |
| 3 | `0x45` | 0 | `0x01000b90` | decode_pool |
| 4 | `0xbe` | 0 | `0x01000dac` | get_sequence_bounds |
| 5 | `0xf2` | 0 | `0x010013f4` | nonzero/seq-bounds |
| 6 | `0x7b` | 0 | `0x01001964` | decode_tensor_dequantize |
| **7** | **`0xe4`** | **0** | **`0x01002258`** | **`pool_conv_lut_load` = the CPTC DISPATCHER** |
| **8** | **`0xf0`** | **7** | **`0x01003b64`** | **`ExtendedInstCptcDecode` = the ext-inst bridge** |

MARIANA's table is structurally identical with funcVAs relocated `+0x8`/`+0x10`. [HIGH/OBSERVED, MARIANA CARRIED]

### 2.2 The `.xt.prop` codec roster

The six decode kernels plus the dispatcher are standalone `.xt.prop` symbols, so
their boundaries are pinned by the next symbol's start (unlike the inline
`proc_6bit` of the MX sibling — see [mx-dequant](mx-dequant.md)). The funcVA is
the first LE word of each `.xt.prop` first record; re-read this pass: [HIGH/OBSERVED]

```
void pool_conv_lut_load()                          @0x01002258   (= kernel_info idx7)
void cptc_decode_impl<(unsigned char)1>(…)         @0x010024b4
void cptc_decode_impl<(unsigned char)2>(…)         @0x01002934
void cptc_decode_impl<(unsigned char)3>(…)         @0x01002bf8
void cptc_decode_impl<(unsigned char)4>(…)         @0x010030cc
void cptc_decode_impl<(unsigned char)5>(…)         @0x010032c8
void cptc_decode_impl<(unsigned char)6>(…)         @0x01003794
```

The mangled name is
`_Z16cptc_decode_implILhNEEvj25_TIE_xt_ivp32_xb_vec2Nx8US0_ttthb`. Decoding it:

* `ILhNE` ⇒ a **non-type template parameter** of type `unsigned char`, value `N`.
  The six shipped instantiations are `N ∈ {1,2,3,4,5,6}`. **`<N>` is a
  compile-time codegen specialization index, not a CPTC level, bit-width, or
  dtype ordinal** (§6). [HIGH/OBSERVED — the mangled `N` range is byte-exact]
* `j` = `unsigned int` (the first scalar arg, `num_active_chans`).
* `25_TIE_xt_ivp32_xb_vec2Nx8U` = the 512-bit `64×u8` native vector type;
  `S0_` repeats it (the second vector arg).
* `tt t h b` = `unsigned short, unsigned short, unsigned short, unsigned char, bool`.

So the C signature, with the dispatcher's operand-field binding, is:

```c
/* cptc_decode_impl<N> : one of six hand-tuned decode kernels.
 * N (template) = the codegen specialization index, NOT the CPTC level.        [HIGH/OBSERVED sig]
 * arg-binding from the dispatcher's three l16ui reads is MED. */
void cptc_decode_impl_N(
    unsigned int   num_active_chans,  /* = num_active_rows = 128 (PE-array rows)            */
    vec2Nx8U       src,               /* the compressed UINT32-transport block (512-bit reg)*/
    vec2Nx8U       dst,               /* the reconstructed output lane vector               */
    unsigned short s0, s1, s2,        /* multiplicative_const[0/1] + block_size pack;
                                         dispatcher reads l16ui a1,66 / a1,68 / a1,70       */
    unsigned char  uc,                /* a dtype/block flag byte                            */
    bool           b);                /* a mode bool                                        */
```

### 2.3 `pool_conv_lut_load` — the `0xe4` dispatcher

The dispatcher prologue + convergence are scalar and byte-exact (objdump,
`XTENSA_CORE=ncore2gp`): [HIGH/OBSERVED]

```
01002258: 36 41 00   entry  a1, 32         ; minimal frame — the dispatcher is a thin router
…
01002258..0x2418     ; reads the operand struct (l16ui a1,66/68/70 = the 3 ushort operands;
                     ;  movi a6,128 = num_active_rows/cols), builds the a0/a2 mask words from
                     ;  the lut_dtype-derived 3-bit field, then the 6-arm ladder (§3).
…
0100244c: 66 63 62   bnei   a3, 6, 0x10024b2   ; BOUND CHECK: gate the 6-way selection
…
010024ac: 50 a2 ff   mov.a  a10, a2            ; the convergence point (5 of 6 arms jump here)
010024af: e0 03 00   callx8 a3                 ; THE single indirect dispatch (a3 = selected impl VA)
010024b2: 1d f0      retw.n
```

`callx8 a3` is the **one** indirect call; `a3` is built per-arm by a `const16`
pair (the impl VA). The `bnei a3,6` bound check confirms a 6-way ceiling. [HIGH/OBSERVED]

> **GOTCHA — `0xe4 ConvLutLoad` is dual-purpose.** The same opcode legacy-loads
> the PE-array 4-bit conversion LUT *and* runs the CPTC decode. Which path
> executes is decided entirely by the operand's `lut_dtype` field via `is_cptc`
> (§4), not by a distinct opcode. A reimplementation must branch on `lut_dtype`
> inside the `0xe4` handler, exactly as the firmware does.

---

## 3. The 6-arm ladder — reloc-pinned PC order

The dispatcher builds each impl's VA via an `L32R`/`const16` pair pinned by the
`.xt.prop` literal-reloc records at **file `0x4ed4`** (12-byte records
`{call_site_PC, reloc_type, target_VA}`, all LE). Decoded byte-exact this pass: [HIGH/OBSERVED]

| reloc @file | call-site PC | type | target VA | impl |
|-------------|-------------|:----:|-----------|------|
| `0x4ed4` | `0x010023b1` | `0x14` | `0x02000100` | rodata table base ref |
| `0x4ee0` | `0x01002418` | `0x23` | `0x01002bf8` | **impl<3>** |
| `0x4eec` | `0x0100241b` | `0x14` | `0x01002bf8` | impl<3> (lo half) |
| `0x4ef8` | `0x0100244f` | `0x23` | `0x01003794` | **impl<6>** |
| `0x4f04` | `0x01002452` | `0x14` | `0x01003794` | impl<6> (lo half) |
| `0x4f10` | `0x0100245a` | `0x23` | `0x010024b4` | **impl<1>** |
| `0x4f1c` | `0x01002462` | `0x14` | `0x010024b4` | impl<1> (lo half) |
| `0x4f28` | `0x01002470` | `0x23` | `0x01002934` | **impl<2>** |
| `0x4f34` | `0x01002478` | `0x14` | `0x01002934` | impl<2> (lo half) |
| `0x4f40` | `0x01002486` | `0x23` | `0x010030cc` | **impl<4>** |
| `0x4f4c` | `0x0100248e` | `0x14` | `0x010030cc` | impl<4> (lo half) |
| `0x4f58` | `0x0100249c` | `0x23` | `0x010032c8` | **impl<5>** |
| `0x4f6c` | `0x010024a4` | `0x14` | `0x010032c8` | impl<5> (lo half) |

**The ladder PC-order is `impl3, impl6, impl1, impl2, impl4, impl5`** — *not*
index order, and the index→arm map is the codegen's, not a linear jump table. [HIGH/OBSERVED]

```c
/* The 6-arm dispatch ladder, reconstructed from the reloc table + the resynced
 * arm-head predicates. The 3-bit dtype field (= lut_dtype & 0x7 = CPTC bit-count)
 * is extracted into a mask word a0/a2 earlier in the dispatcher; each arm tests
 * a slice of it. PC-order impl3,6,1,2,4,5. */
void pool_conv_lut_load_ladder(uint32_t a0_mask, uint32_t a2_mask, uint32_t a4_fmt) {
    void (*impl)(/* … */) = 0;
    /* arm-head predicate forms (byte-exact, §3.1): */
    if (/* impl3 arm @0x2418 */ predicate3) impl = (void*)0x01002bf8;  /* const16 a3,0x2bf8 CLEAN */
    if (/* impl6 arm @0x244f */ predicate6) impl = (void*)0x01003794;  /* const16 a3,0x3794 CLEAN */
    if (/* impl1 arm @0x245a */ bany(a9, a2_mask)) impl = (void*)0x010024b4; /* bany a9,a2  = 27 89 99 */
    if (/* impl2 arm @0x2470 */ extui1(a0_mask, 18)) impl = (void*)0x01002934; /* extui a14,a0,18,1 = 00 e2 05 */
    if (/* impl4 arm @0x2486 */ bany(a9, a2_mask)) impl = (void*)0x010030cc; /* bany a9,a2  = 27 89 99 (IDENTICAL to impl1) */
    if (/* impl5 arm @0x249c */ extui1(a0_mask, 21)) impl = (void*)0x010032c8; /* extui a14,a0,21,1 = 00 e5 05 */
    /* … a3 = impl; bnei a3,6 bound check; … */
    impl(/* num_active_chans=128, src, dst, s0,s1,s2, uc, b */);   /* callx8 a3 @0x010024af */
}
```

### 3.1 The arm-head predicates (byte-exact)

Only **two** ladder arms resync as clean scalar `const16` builders — impl<3>
(`34 00 01 / 34 f8 2b` → `a3=0x01002bf8`) and impl<6> (`34 00 01 / 34 94 37` →
`a3=0x01003794`). The other four sit inside FLIX bundles. But their **arm-head
predicates** (the first scalar `x24` op after the `0x3f` wide-bundle marker)
resync cleanly and are **cross-gen byte-identical** CAYMAN↔MARIANA(+0xC): [HIGH/OBSERVED]

| arm | head bytes | op | form |
|-----|-----------|----|------|
| impl<1> @`0x245c` | `27 89 99` | `bany a9,a2,<lbl>` | **multi-bit mask test** (a SET of field values) |
| impl<2> @`0x2471` | `00 e2 05` | `extui a14,a0,18,1` | **single-bit test** (bit 18 of `a0`) |
| impl<4> @`0x2488` | `27 89 99` | `bany a9,a2,<lbl>` | multi-bit — **byte-identical to impl<1>** |
| impl<5> @`0x249d` | `00 e5 05` | `extui a14,a0,21,1` | single-bit (bit 21 of `a0`) |

This pairs impl<4> with impl<1> (both `bany`-RANGE form) and impl<2> with impl<5>
(both single-bit `extui`, at bits 18 vs 21 — a 3-bit stride) at the dispatcher
level. The pairing is byte-exact even though the served *values* it routes are
not (§9). [HIGH/OBSERVED the predicate forms; the field-value map LOW]

---

## 4. The operand struct — `NEURON_ISA_TPB_S2_CONVLUT` (64 B)

Source: `aws_neuron_isa_tpb_s2_convlut.h` (cayman), bound to
`OPCODE_CONV_LUT_LOAD = 0xe4`. **Compile-verified this pass** (`gcc -std=c11`):
`sizeof == 64`, and `offsetof` gives `in_dtype=32, num_active_rows=38,
multiplicative_const=40, block_size=46, lut_dtype=47`. [HIGH/OBSERVED]

| off | size | field | type | CPTC role |
|----:|-----:|-------|------|-----------|
| 0–3 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{ opcode=0xe4, inst_word_len }` |
| 4–11 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update sync |
| 12–15 | 4 | `reserved0[4]` | — | `==0` |
| 16–27 | 12 | `src_mem_pattern` | `TENSOR2D` | the **compressed source** (read-only, SBUF, non-indirect) |
| 28–31 | 4 | `reserved1[4]` | — | `==0` |
| 32 | 1 | `in_dtype` | `NEURON_ISA_TPB_DTYPE` | **`UINT32` for CPTC** (transport) \| `UINT16` legacy LUT |
| 33–37 | 5 | `reserved2[5]` | — | `==0` |
| 38 | 1 | `num_active_rows` | `uint8_t` | `==128` (`PE_ARRAY_NUM_ROWS`) |
| 39 | 1 | `num_active_cols` | `uint8_t` | `==128` |
| 40–43 | 4 | `multiplicative_const[2]` | `uint16_t[2]` | **the 2 CPTC scale/format ushorts** (legacy: `==0`) |
| 44 | 1 | `row_grp` | `uint8_t` | `==0xf` |
| 45 | 1 | `col_grp` | `uint8_t` | `==0xf` |
| 46 | 1 | `block_size` | `uint8_t` | **CPTC block size, `%8 == 0`** (legacy: `==0`) |
| 47 | 1 | `lut_dtype` | `NEURON_ISA_TPB_DTYPE` | **the CPTC DISCRIMINATOR + the CPTC code carrier** |
| 48–63 | 16 | `reserved4[16]` | — | `==0` |

> **CORRECTION — the header comments `reserved4[16]` as "`18 (48 - 63)`".** That
> width annotation is a header typo: `48..63` is **16** bytes, and 16 is exactly
> what brings the struct to the asserted 64 B. The `ISA_STATIC_ASSERT(sizeof==64)`
> and the compile-verified `offsetof` are the witnesses; the field is 16 B. [HIGH/OBSERVED]

### 4.1 The `is_cptc` discriminator (verbatim from the header)

```c
/* aws_neuron_isa_tpb_s2_convlut.h — read byte-for-byte this pass. */
bool s2_convlut_is_cptc(Inst i) {
    return i.s2_convlut.lut_dtype != Dtype::Invalid    /* Invalid == 0  */
        && i.s2_convlut.lut_dtype != Dtype::FP32;      /* FP32    == 0xA */
}
bool s2_convlut_valid_block_size(Inst i) { return i.s2_convlut.block_size % 8 == 0; }
bool s2_convlut_valid_nc(Inst i, NeuronCoreVersion nc) {
    return s2_convlut_is_cptc(i) && nc >= NeuronCoreVersion::V3;     /* the NC-v3+ gate */
}
/* src element-count split: */
/*   legacy : in_dtype==UINT16, src num_elem[0]==16  (the 16-entry PE LUT)            */
/*   CPTC   : in_dtype==UINT32, src num_elem[0]==4   (a 4-unit compressed block)      */
```

So `is_cptc` is `true` **iff** `lut_dtype` is neither `Invalid(0)` nor `FP32(0xA)`
— and the legal CPTC codes are exactly `CPTC1..7 = 0x19..0x1F` (§5). The header
carries the designer's remark: *"Have to add FP32 here because the default value
of `Dtype` when unspecified is FP32"* — i.e. `FP32`/`Invalid` are the
"this-is-a-legacy-LUT-load" sentinels. The CPTC path additionally requires
`in_dtype == UINT32`, `src num_elem == 4`, and `NeuronCoreVersion >= V3`. [HIGH/OBSERVED]

---

## 5. The CPTC dtype codes `0x19..0x1F`

The compressed tensor rides **`UINT32` transport** (`in_dtype@32 == UINT32`); the
**logical** format is the `lut_dtype@47` CPTC code. From the **mariana**
`aws_neuron_isa_tpb_common.h` (read byte-for-byte this pass; CAYMAN's header has
**no** CPTC enum — §8): [HIGH/OBSERVED]

```c
NEURON_ISA_TPB_DTYPE_CPTC1 = 0x19,  // 32-bit aligned Computed Permutation Trellis Coding format space
NEURON_ISA_TPB_DTYPE_CPTC2 = 0x1A,  //   Dtype & 0x7 gives the bit count of the dtype. Reserved 0x1F so
NEURON_ISA_TPB_DTYPE_CPTC3 = 0x1B,  //   that `(dtype & 0xF8) == 0x18 && (dtype & 0x7) != 0` check always
NEURON_ISA_TPB_DTYPE_CPTC4 = 0x1C,  //   works to determine if a dtype is a CPTC dtype
NEURON_ISA_TPB_DTYPE_CPTC5 = 0x1D,
NEURON_ISA_TPB_DTYPE_CPTC6 = 0x1E,
NEURON_ISA_TPB_DTYPE_CPTC7 = 0x1F,
```

The **bit-count is `code & 0x7`** (`CPTC1`=1-bit … `CPTC7`=7-bit), and the
identity is `is_cptc = (code & 0xF8) == 0x18 && (code & 0x7) != 0`.

> **CROSS-CHECK — this MUST agree with [the datatype model](dtype-model.md),
> and it does, byte-for-byte.** `dtype-model.md` §3 commits exactly:
> `CPTC1..7 = 0x19..0x1F`, `bit-count = code & 0x7`, the same header comment
> verbatim, and the same `is_cptc(code) := (code & 0xF8) == 0x18 && (code & 0x7) != 0`
> (true for `0x19..0x1F`, false for the `0x18` "CPTC0" sentinel). That page tags
> the trellis *decode* as an open wall pending **this** page — which §6–§7 below
> now close (the decode is observed; the format's forward/encode packing remains
> open, §11). No divergence between the two pages. [HIGH/OBSERVED both pages]

---

## 6. The `<N>` template — what each impl IS, and the self-guard map

`<N>` is an **internal codegen specialization index** over the
(CPTC-source-bit-width × output-reconstruction) decode legs. Three facts pin
this:

1. The mangled `ILhNE` gives `N` as an `unsigned char` literal, `N ∈ {1..6}`.
   `N=7` is **not** instantiated despite `CPTC7` existing — so `<N>` is not the
   CPTC level. [HIGH/OBSERVED]
2. The three **odd** impls carry a **self-guard** that re-asserts the dtype they
   serve; the value the guard checks is **not** equal to `N`. [HIGH/OBSERVED]
3. The six impls have genuinely different bodies (distinct frame sizes, distinct
   IVP vocabularies), so they are hand-tuned kernels, not dtype-parameterized
   copies of one body. [HIGH/OBSERVED]

### 6.1 The odd-impl self-guard map

The guard is `extui a9, a4, 22, 3` (extract the 3-bit field `[bits 22..24]` of
the format word `a4`) followed by `bnei a9, N`. The extract signature **`40 96 25`
occurs at exactly three `.text` offsets** — `{0x24d9, 0x2c1d, 0x32ed}` = impls
1/3/5 — proven by an exhaustive byte scan this pass. The three `bnei` constants
are each the **sole** occurrence in the binary: [HIGH/OBSERVED]

| impl | guard `extui` @ | `bnei` @ | bytes | serves field | ⇒ CPTC level |
|------|----------------|----------|-------|:------------:|:------------:|
| impl<1> | `0x24d9` | `0x24e2` | `66 19 89` (`bnei a9,1`) | **1** | CPTC1, 1-bit |
| impl<3> | `0x2c1d` | `0x2c26` | `66 49 89` (`bnei a9,4`) | **4** | CPTC4, 4-bit |
| impl<5> | `0x32ed` | `0x32f6` | `66 39 89` (`bnei a9,3`) | **3** | CPTC3, 3-bit |

```c
/* The odd-impl self-guard (impl<1>/<3>/<5>) — a defensive re-check of the
 * dispatcher's routing decision. byte-exact at the three offsets above. */
void cptc_decode_impl_odd_prologue(uint32_t a4_fmt /* the format word */) {
    uint32_t field = (a4_fmt >> 22) & 0x7;   /* extui a9,a4,22,3 — the 3-bit CPTC bit-count */
    if (field != /* impl<1>:1 | impl<3>:4 | impl<5>:3 */) goto reject;   /* bnei a9,N — bail to error */
    /* … the full vector decode … */
}
```

> **QUIRK — the index→served-value map is NON-IDENTITY.** `impl<1>→1`,
> `impl<3>→4`, `impl<5>→3`. The template index `<N>` and the CPTC level it
> handles do **not** coincide (except impl<1> by accident). A reimplementation
> dispatching on `(lut_dtype & 0x7)` must route to the impl whose *served value*
> matches the field, not to "impl number == bit-count". The mapping is
> codegen-internal. [HIGH/OBSERVED]

### 6.2 The even impls carry NO self-guard

Impls **2/4/6** are the alternate ("even") codegen class: minimal `entry a1,32`
frame, **no** `extui a9,a4,22,3` self-guard. The guard signature is **absent**
from each of their body spans (exhaustive scan, both gens). They are routed
**purely by the dispatcher ladder** — they trust the routing and never re-assert
the dtype. The odd impls additionally self-guard; the even impls do not. [HIGH/OBSERVED]

```
guard-sig 40 96 25 (extui a9,a4,22,3) — whole-text byte scan, this pass:
  hits @ {0x24d9, 0x2c1d, 0x32ed}  =  ONLY impls 1/3/5 (the odd impls)
  ABSENT from impl<2> [0x2934..0x2bf8), impl<4> [0x30cc..0x32c8), impl<6> [0x3794..0x3d4a)
```

### 6.3 The three frame sizes (one sole occurrence each)

The entry-frame immediate decodes by `imm = ((b2<<4)|(b1>>4))<<3`. The three
**odd** impls have three **distinct** large 64-byte-aligned frames; the three
**even** impls share the minimal frame: [HIGH/OBSERVED]

| impl | entry bytes | frame | class |
|------|------------|------:|-------|
| impl<1> | `36 81 06` | `0x340` (832 B) | ODD: 64B-aligned (`movi a10,-64; movsp a1,a8`), self-guarded |
| impl<3> | `36 81 03` | `0x1c0` (448 B) | ODD: 64B-aligned, self-guarded |
| impl<5> | `36 81 04` | `0x240` (576 B) | ODD: 64B-aligned, self-guarded |
| impl<2>/<4>/<6> | `36 41 00` | `0x20` (32 B) | EVEN: no align, no self-guard |

Three distinct frame sizes for the odd impls (and a heavy vector working-set for
impl<1>) confirm hand-tuned, genuinely different decode routines. [HIGH/OBSERVED]

---

## 7. The decode algorithm and the per-impl datapath legs

### 7.1 The common decode pipeline

Every impl runs the same skeleton; the legs differ in signedness, fp-width, and
which sub-byte unpack they carry. The IVP vocabulary is harvested by multi-phase
(0..7) disasm with all phases in agreement, and every named primitive is
confirmed as a real encoding in `libisa-core.so`: [HIGH/OBSERVED vocab; MED in-FLIX order]

```c
/* The end-to-end CPTC decode (common skeleton across all six impls). */
void cptc_decode_common(const uint32_t *compressed, uint16_t s0, uint16_t s1, uint16_t s2,
                        Dtype out_dtype, vec2Nx8U *dst) {
    /* STAGE A — PACKED LOAD: pull the bit-plane-interleaved sub-byte codes.   */
    /*   ivp_l2a4nx8_ip (group-of-8 4-bit packed) / ivp_lat2nx8_xp / ivp_la2nx8_ipi */
    vec v_packed = ivp_l2a4nx8_ip(compressed);

    /* STAGE B — SUB-BYTE UNPACK: extract the sub-byte codes (impls 1/3/4/5/6). */
    /*   ivp_sel2nx8i_s4 = 4-bit sub-byte unpack; impl<2>/<3> use a wider deal. */
    vec v_codes = ivp_sel2nx8i_s4(v_packed);

    /* STAGE C — COMPUTED-PERMUTATION DE-INTERLEAVE: spread the packed bit-planes
     *   back to per-element lanes via the FIXED step-32 table (§7.2).          */
    /*   ivp_sel2nx8i / ivp_dselnx16t by the .rodata index + ivp_dextrprn_2x32 gather */
    vec v_lanes = ivp_sel2nx8i(v_codes, /* index = */ DEINTERLEAVE_TABLE);
    packed_reg pr = ivp_dextrprn_2x32(v_lanes);

    /* STAGE D — SCALE-MAC: multiply by the per-block scale (pr<N>) + const re-bias. */
    /*   ivp_mulpan16xr16 (per-block scale by pr<N>); per-leg: signed mulsupn/dmulq,  */
    /*   unsigned muluupn/dmuluuq, x4 mul4t2n8, 16->32 widen muln_2x16x32.            */
    widevec wv = ivp_mulpan16xr16(v_lanes, /* scale */ pr, s0);

    /* STAGE E — RECONSTRUCT to the output dtype.                               */
    /*   FP16 : ivp_mulsonenxf16t / ivp_ultnxf16t                                */
    /*   FP32 : ivp_utruncn_2xf32t (narrow, shift24) / ivp_maxn_2xf32t / ivp_ultqn_2xf32t */
    /*   norm : ivp_bsubnormnx16 (subnormal) / ivp_baddnormnx16 (add-normalize)  */
    /*   widest: ivp_packvr2nx24_0 (24-bit packed-reg pack, impl<6> only)         */
    vec v_recon = reconstruct(wv, out_dtype);

    /* STAGE F — SATURATE / CLAMP.                                              */
    /*   signed   : ivp_bmin2nx8 / ivp_absssubnx16 / ivp_addsnx16t              */
    /*   unsigned : ivp_bminun_2x32 / ivp_bmaxun_2x32 / ivp_bminu2nx8 / ivp_babssub2nx8 */
    vec v_clamped = saturate(v_recon);

    /* STAGE G — STORE: signed ivp_svnx8s_i / unsigned-trunc ivp_svnx8ut_i.      */
    *dst = v_clamped;
}
```

The repeated 3-bit-field extracts (`extui …,22,3` at the head; `extui …,17,3`
mid-body in impl<1>) are the **bit-width / "trellis" state steps** — each governs
the next unpack stride / permute index. The fp-narrow path (`utruncn_2xf32t`
narrows fp32 by a 24-bit shift) plus subnormal handling indicate a **soft-float**
reconstruction (the GPSIMD has no hardware FP). [HIGH stages + named ops; MED loop counts]

### 7.2 The six byte-identical de-interleave tables (proven by hashing)

The `.rodata` carries **six** 0x100-byte blocks at VMA `0x020001c0`, `0x2c0`,
`0x3c0`, `0x4c0`, `0x5c0`, `0x6c0` — one adjacent to each impl's literal window.
Hashing the regions this pass **proves byte-identity**: the de-interleave
permutation proper is the **first 0x80** of each block, and all six are
identical; blocks #1–#5 are also identical over the full 0x100 (block #6's tail
past 0x90 collides with the `kernel_info_table` that follows it in memory): [HIGH/OBSERVED]

```
de-interleave table first-0x80 sha256 (all six blocks, this pass):
  #1 @0x020001c0 = 9593eb7f81aa099d      #4 @0x020004c0 = 9593eb7f81aa099d
  #2 @0x020002c0 = 9593eb7f81aa099d      #5 @0x020005c0 = 9593eb7f81aa099d
  #3 @0x020003c0 = 9593eb7f81aa099d      #6 @0x020006c0 = 9593eb7f81aa099d
  ALL SIX first-0x80 IDENTICAL ✓
blocks #1..#5 full-0x100 sha256 = b36d8a175f2ea021  (IDENTICAL) ✓
```

So there is **one** de-interleave permutation, replicated 6× for per-impl
base-register locality — **not** six distinct per-impl permutations. The first 16
bytes are `01 21 41 61 03 23 43 63 05 25 45 65 07 27 47 67`, and the 64-lane rule
is byte-exact (verified over all of `j=0..63` this pass):

```c
/* The single, byte-identical step-32 4-deal de-interleave (verified j=0..63). */
uint8_t deinterleave_table[64];
for (int j = 0; j < 64; ++j)
    deinterleave_table[j] = ((j & 3) << 5) | (((j >> 2) << 1) | 1);
/* out-lane j reads SOURCE byte-lane (j%4)*32 + (1 + 2*(j/4)) — the ODD source
 * lanes {1,3,5,…} in 4-way step-32 groups: 0x01,0x21,0x41,0x61, 0x03,0x23,… .
 * This spreads 4 sub-byte bit-planes packed into adjacent lanes back out to
 * per-element lanes — the "Computed Permutation" de-interleave. */
```

> **CORRECTION — the closed-form is `((j&3)<<5)|((j>>2)<<1|1)`, not `((j&3)<<5)|(j>>2)`.**
> An early sibling read quoted the simpler form and framed the six blocks as
> "six distinct permutations". The listed *bytes* were always right; the formula
> was off by the `<<1 | 1` (the odd-lane selection), and the blocks are in fact
> byte-identical. Both are corrected here and confirmed by hash + per-lane
> verification. [HIGH/OBSERVED]

### 7.3 The per-impl sign / dtype / fp-width legs

Each impl occupies a distinct `(signedness × fp-width × unpack)` cell. The MAC
primitive names the signedness (`mulsupn`/`dmulq` = signed; `muluupn`/`dmuluuq`
= unsigned widening; `mulsupn`/`muluspn`/`dmulusq`/`mulsuqa` = mixed
signed×unsigned). The full family table: [HIGH where guard/MAC byte-pinned; MED where FLIX-desynced]

| impl | served field / CPTC | frame | codegen | datapath leg |
|------|:-------------------:|------:|---------|--------------|
| **impl<1>** | **1 / CPTC1, 1-bit** `[HIGH]` | `0x340` | ODD self-guard | **SIGNED / FP32**: `_s4` 4-bit unpack + `dextrprn` gather, FP32 narrow (`utruncn_2xf32t`/`maxn_2xf32t`), subtract-normalize (`bsubnormnx16`), signed saturate. |
| **impl<2>** | ? (even, ladder-routed) `[LOW]` | `0x20` | EVEN | **UNSIGNED / FP16**: `lvn_2x16u` + `muluupn`/`muluupan` upper-MAC, FP16 scale (`mulsonenxf16t`), `bmaxun`/`bminun` clamp, unsigned-trunc store (`svnx8ut`), x4 MAC; **NO `_s4`**. |
| **impl<3>** | **4 / CPTC4, 4-bit** `[HIGH]` | `0x1c0` | ODD self-guard | **SIGNED / FP16+24b**: signed upper-MAC (`mulsupn16xr16`) + `_s4` 4-bit unpack, FP16 predicate (`ultnxf16t`), add-normalize (`baddnormnx16`), 24-bit pack (`packvr2nx24_0`), `subn_2xf32t` fp32 intermediate. |
| **impl<4>** | ? (even, ladder-routed) `[LOW]` | `0x20` | EVEN | **MIXED-SIGN / `_s4` / FP16+FP32**: `_s4` unpack + `dextrprn` gather + `ulen_2xf32` (FP32) + 64-bit load (`lsn_4x64_i`); MAC vocabulary spans unsigned (`muluupn`/`muluuqn`/`mulussn`) + signed (`dmulq2n8d`/`mulsn`) + mixed s×u (`dmulusq`/`mulsuqa`/`mulus4tan`); also FP16 (`mulnxf16t`). |
| **impl<5>** | **3 / CPTC3, 3-bit** `[HIGH]` | `0x240` | ODD self-guard | **UNSIGNED / INTEGER / `_s4`**: unsigned upper-MAC (`muluupn16xr16`) + `_s4` 4-bit unpack + `l2a4nx8` packed load, unsigned compares/clamps (`ltu2nx8`/`ltunx16`/`bminu2nx8`), `rep2nx8t` scale broadcast; **NO fp op at all** (integer reconstruct). |
| **impl<6>** | ? (even, ladder-routed) `[LOW]` | `0x20` | EVEN | **MIXED / WIDEST**: BOTH signed (`mulsupn`+`dmulq2n8`) AND unsigned (`dmuluuq2n8`) quad-8 MAC + `_s4` unpack + 24-bit pack (`packvr2nx24_0`) + 16→32 widen + BOTH FP16 (`ultnxf16t`) & FP32 (`ultqn_2xf32t`/`ulen_2xf32`) + BOTH signed (`svnx8s`) & unsigned (`svnx8ut`) store. The full-width dual leg. |

> **NOTE — impl<4> is MIXED-SIGN, not "signed-leaning".** A FLIX bundle decoder
> resyncs MAC bundles that linear objdump cannot, and they are dominated by
> unsigned (`muluupn`) and mixed s×u (`mulus4tan`/`dmulusq`) MACs. The
> `_s4 + FP32` markers remain true, but the signedness is genuinely mixed. The
> cleanest bundle (`@0x01003172`, 4/5 slots clean) carries `ivp_muluupn16xr16`
> (unsigned), cross-gen stable. The mnemonic table cleanly distinguishes
> `mulsupn` (signed) from `muluupn` (unsigned), so this is a real unsigned MAC,
> not a naming ambiguity. [MED/OBSERVED — 1 undef slot remains; objdump does not
> oracle this body]

#### 7.3.1 Two resynced impl<1> bundles (the byte-exact anchors)

```
@0x010026d9 { ivp_la2nx8_ipi v13,u0,a0,1 ; ivp_mulpan16xr16 wv3,v0,v2,pr8 ;
              ivp_sel2nx8i v27,v28,v17,29 ; ivp_neqn_2x32 vb12,v6,v7 }
              <- align LOAD + SCALE-MAC(pr8) + de-interleave SELECT(idx 29) + mask
@0x010027ee { ivp_lv2nx8_i v20,a15,0x3940 ; ivp_mulnx16c wv2,v22,v0 ;
              ivp_utruncn_2xf32t v19,v3,24,vb13 ; ivp_absssubnx16 v19,v16,v3 }
              <- LOAD + const-mul + fp32 NARROW(shift24) + sat-abs-sub
```

These pin impl<1>'s signed/fp32 leg: per-block scale-MAC (`mulpan16xr16` ×
`pr<N>`), de-interleave SELECT by the `.rodata` index, and the fp32 narrow
(`utruncn_2xf32t` with a 24-bit shift = the exponent/mantissa reassembly). [HIGH/OBSERVED]

### 7.4 The `0xf0`/spec-7 extended-instruction bridge

The `kernel_info` idx8 (`opcode 0xf0`, `spec 7`) → `ExtendedInstCptcDecode`
@`0x01003b64` (`entry a1,32`, byte-exact). It forwards the SEQ-side `0xf0`/spec-7
escape to the **same** CPTC decode body, reading the operand and routing on the
dtype byte. The DEBUG build validates **both** dtypes (host-lib strings,
read this pass): [HIGH/OBSERVED]

```
0x269562  "P%i: Decode : ExtendedInstCptcDecode : num_active_chans = %d"   (the decode trace)
0x2695a0  "P%i: Error, unsupported in_dtype for cptc_decode : 0x%x"        (reject: in_dtype must be UINT32)
0x2695d9  "P%i: Error, unsupported out_dtype for cptc_decode : 0x%x"       (reject: the reconstructed type)
```

---

## 8. Per-generation presence

The codec (`EXTISA_3`: `0xe4` dispatcher + `0xf0`/spec7 bridge +
`cptc_decode_impl<1..6>` + the 6 de-interleave blocks): [HIGH/OBSERVED unless noted]

| gen (NC) | codec kernel | notes |
|----------|:------------:|-------|
| SUNDA (v2) | **ABSENT** | no `EXTISA_3`, no `0xe4`/`0xf0` in its 18-entry flat Q7 table (SUNDA EXTISA container is out-of-corpus) `[CARRIED]` |
| CAYMAN (v3) | **PRESENT** | `CAYMAN_EXTISA_3` sha `052ac31c`; all six impls + tables + guards re-read this pass |
| MARIANA (v4) | **PRESENT** | `MARIANA_EXTISA_3` sha `8477ff26`; bodies relocated **+0xC**, tables byte-identical |
| MARIANA_PLUS (v4+) | **PRESENT** | `EXTISA_3` byte-identical to MARIANA `[CARRIED]` |
| MAVERICK (v5) | **PRESENT (header-OBSERVED)** | `kernel_info` `0xe4`/`0xf0`-spec7 rows + the 3 CPTC DEBUG strings present in the host lib's 2nd arch block (`@0x502c83`/`0x502cc1`/`0x502cfa`); but the EXTISA images are **stripped `ET_DYN`** so the impl **names/bodies are not symbol-recoverable** → interiors **INFERRED** |

> **CORRECTION/NOTE — MAVERICK (v5) interiors are header-OBSERVED → INFERRED.**
> Presence is established from the `kernel_info` rows and the DEBUG strings
> (header-level evidence). The decode-body facts (the impl bodies, the per-impl
> datapaths, the `+0xC`-style relocation) are **not** re-read from MAVERICK — its
> EXTISA images are stripped. Everything claimed about MAVERICK's *interior* is
> INFERRED from CAYMAN/MARIANA structure, not OBSERVED on MAVERICK bytes. [HEADER-OBSERVED → INFERRED]

> **GOTCHA — the codec ships on CAYMAN (v3) but the CPTC dtype enum is MARIANA+.**
> `gcc` against the **cayman** header has **no** `NEURON_ISA_TPB_DTYPE_CPTC1`
> (verified: zero CPTC hits in the cayman `common.h` this pass), while the
> **mariana** header has `CPTC1..7`. So CAYMAN silicon ships the decoder
> machinery but the v3 ISA cannot legally name a CPTC `lut_dtype` to it; CPTC
> becomes a usable surface at **MARIANA**. This mirrors the per-gen split the
> [datatype model](dtype-model.md) records (`CPTC1..7` added at MARIANA). [HIGH/OBSERVED both legs]

---

## 9. The even-impl served values — the static-recovery wall

The **three odd** impls' served dtype values are byte-pinned by their self-guards
(§6.1). The **three even** impls (2/4/6) carry no self-guard; their 3-bit served
value is decided by the dispatcher's `const16 a3` selector, which sits in
FLIX-desync. A validated FLIX bundle decoder (regression-checked: it reproduces
every byte-exact CPTC bundle the disasm hand-pinned, zero disagreements) was run
against the four non-clean ladder arms and showed that they are **not** clean
FLIX bundles — they format-match but decode to vector-op + `undef` garbage — and
**the device-native objdump refuses to disassemble them too**. The validated
tool reaches the **same wall** the oracle does. [HIGH/OBSERVED — the negative result is byte-grounded]

> **QUIRK — this is a genuine wall, not a missing-effort gap.** The even-impl
> 3-bit served value is **not byte-recoverable by static decode**. The mask words
> `a0`/`a2` the predicates test are built from the 3-bit field earlier in the
> dispatcher's setup region (`0x2304..0x2418`), and that build is itself
> FLIX-desynced beyond both the decoder and objdump. Pinning the even bindings
> requires either a hand-resync of the setup-region bundles or dynamic dispatch
> instrumentation. Honestly flagged LOW.

What **is** byte-exact is the predicate **form** (§3.1) and a structural
elimination. Domain `{1..7}`, three byte-pinned `{1→impl<1>, 3→impl<5>, 4→impl<3>}`,
leaving `{2,5,6,7}` for `{impl<2>,impl<4>,impl<6>}`; `impl<7>` is not instantiated
so one even arm must absorb `CPTC7`. The byte-exact predicate forms pair
impl<4>↔impl<1> (`bany`-RANGE) and impl<2>↔impl<5> (single-bit at bit 18 vs 21),
and the datapath widths place impl<6> (widest, 24-bit pack, dual MAC) at the
highest levels. The **best-estimate** routing map: [LOW/INFERRED for the even rows]

| 3-bit value | CPTC | dtype | bits | → impl | evidence |
|:-----------:|:----:|:-----:|:----:|--------|----------|
| 1 | CPTC1 | `0x19` | 1 | **impl<1>** | `[HIGH/OBSERVED]` self-guard `bnei a9,1`; arm `bany a9,a2` |
| 3 | CPTC3 | `0x1B` | 3 | **impl<5>** | `[HIGH/OBSERVED]` self-guard `bnei a9,3`; arm `extui a0,21,1` |
| 4 | CPTC4 | `0x1C` | 4 | **impl<3>** | `[HIGH/OBSERVED]` self-guard `bnei a9,4`; `const16 a3,0x2bf8` clean |
| 2 | CPTC2 | `0x1A` | 2 | impl<2> | `[LOW/INFERRED]` even arm `extui a0,18,1`; unsigned/fp16; sibling-form to impl<5> |
| 5 | CPTC5 | `0x1D` | 5 | impl<4> | `[LOW/INFERRED]` even arm `bany a9,a2` (==impl<1> form); `_s4`+mixed-MAC; mid level |
| 6 | CPTC6 | `0x1E` | 6 | impl<6> | `[LOW/INFERRED]` `const16 a3,0x3794` clean; MIXED/widest datapath |
| 7 | CPTC7 | `0x1F` | 7 | impl<6> | `[LOW/INFERRED]` `impl<7>` not instantiated; CPTC7 folds into the widest arm |

---

## 10. The env-gate — `NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE`

The whole CPTC leg is **off by default**. The host resolver stages lib UID 3
(`EXTISA_3` = the CPTC lib) **only** when
`getenv("NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE")` is set, on a PERF
coretype. The env string is read at host-lib **`0x4d15`** this pass; without it,
lib 0 is staged (no CPTC), and SUNDA can never reach lib 3. CPTC is therefore an
**experimental / unstable, opt-in** feature. [HIGH/OBSERVED string; CARRIED RT-10 for the staging logic]

```c
/* host resolver gate (RT-10 byte-exact CARRIED; string OBSERVED this pass) */
if (coretype_is_perf && getenv("NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE"))
    stage_library(/* UID */ 3);   /* EXTISA_3 = the CPTC codec; opcode 0xe4 + 0xf0/spec7 */
else
    stage_library(/* UID */ 0);   /* no CPTC decode available */
```

---

## 11. "Trellis vs permutation", and honest residuals

> **NOTE — the OBSERVED decode is a deterministic computed-permutation
> de-interleave + scale + reconstruct; no Viterbi trellis is disassembled.** The
> ISA-header naming is "Computed Permutation Trellis Coding", but across all six
> bodies the mechanism is the one byte-identical step-32 de-interleave table +
> per-block scale-MAC + fp/int reconstruct, with repeated 3-bit-field state
> steps. No add-compare-select path-metric loop / survivor-path memory is present
> (the `ivp_counteqmz4nx8` in impl<6> is a state/predicate count, not a
> path-metric). The deterministic bit-width state stepping is consistent with a
> "trellis" walk, but a Viterbi metric is **not** observed. [MED/INFERRED]

Open residuals, honestly flagged:

* **R1 [LOW]** — the **even-impl served values** (§9): not byte-recoverable by
  static decode; the validated decoder and the oracle both hit the wall.
* **R2 [MED]** — the **legal `out_dtype` set**: the DEBUG validates one, but the
  legal-list compare chain is FLIX-desynced. From the observed reconstruct
  datapaths it spans FP16 (impls 2/3/6), FP32 (impls 1/4/6), and integer (the
  signed/unsigned 8-bit stores); the exact enum is not byte-pinned.
* **R3 [LOW]** — the **forward (encode) path** and the exact per-`CPTC1..7`
  bit-plane packing (how the 1..7-bit codes pack into the UINT32 transport before
  the step-32 de-interleave): inferable from the de-interleave inverse but not
  byte-traced per level; the encoder is not in this corpus.
* **R4 [carry]** — MAVERICK bodies stripped; SUNDA out-of-corpus (§8).

---

## Cross-references

* [The Unified Datatype Model](dtype-model.md) — the `NEURON_ISA_TPB_DTYPE` enum,
  the CPTC codes `0x19..0x1F`, and the `is_cptc` identity this page cross-checks (§5).
* [MX Dequantize](mx-dequant.md) — the sibling compressed-format decoder
  (`proc_4bit_mx_8` / `proc_6bit`) sharing the EXTISA image and the unsigned
  widening dequant MAC vocabulary; CPTC is a **distinct** codec.
* [Tensor Dequantize](tensor-dequantize.md) — the UINT32-transport + dequant-fmt
  model CPTC mirrors (the CPTC code rides `lut_dtype`, not `in_dtype`).
* [ConvLutLoad / POOL ext `0xf0`](../pool/pool-ext-0xf0.md) — the SEQ-side `0xf0`/spec-7
  escape and the legacy ConvLutLoad LUT path that shares opcode `0xe4`.
* [FLIX decoding](../../reference/flix-decoding.md) — the bundle-decode
  methodology backing the resynced-bundle reads and the desync flags.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the
  `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` tagging used throughout.
