# cute_nvgpu Assembly Printer and Type Mnemonics

## Abstract

`cute_nvgpu` textual assembly is primarily a type surface. Atom and descriptor types print as compact mnemonics — `sm90.mma`, `atom.tma_load`, `SM120.mma_bs`. Parameterized types add angle-bracket payloads for descriptor views, universal copy atoms, or sub-byte integer fragments. The dialect provides no dialect-scoped attributes in text; operation attributes use ordinary builtin attribute syntax. This page covers the printer, the parser-facing mnemonics, their parameters, enum spellings, alias hints, and the 27-entry length-keyed packed-XOR perfect-hash dispatcher (`sub_1826CF0`, 6164 B) that resolves them all.

## Type Mnemonics

| Family | Mnemonics |
|---|---|
| MMA atoms | `atom.universal_fma`, `sm80.mma`, `sm80.sparse_mma`, `sm89.mma`, `sm90.mma`, `sm100.mma`, `sm100.mma_sp`, `sm100.mma_bs`, `sm100.mma_bs_sp`, `SM120.mma_bs` |
| SMEM descriptors | `smem_desc`, `smem_desc_view` |
| TMEM and copy atoms | `atom.tmem_load`, `atom.tmem_store`, `atom.s2t_copy`, `atom.universal_copy`, `atom.simt_async_copy`, `atom.ldsm`, `atom.stsm` |
| TMA descriptors and atoms | `tma_descriptor_tiled`, `tma_descriptor_im2col`, `atom.tma_load`, `atom.tma_store`, `atom.tma_reduce`, `atom.non_exec_tiled_tma_load`, `atom.non_exec_tiled_tma_store`, `atom.non_exec_tiled_tma_reduce` |

`SM120.mma_bs` is case-sensitive. It prints and parses with uppercase `SM`.

## Parameterized Types

### `smem_desc_view`

`smem_desc_view` wraps a source type and a layout attribute:

```mlir
!cute_nvgpu.smem_desc_view<memref<128xf16, 3>, #cute.layout<(4@0, 32@1)>>
```

The source type describes the shared-memory object. The layout attribute tells WGMMA or UMMA how to interpret that shared-memory tile.

### `atom.universal_copy`

Universal copy atoms carry value type, optional bit width, optional distributed
shared-memory allowance, and optional PTX-like memory order and scope:

```mlir
!cute_nvgpu.atom.universal_copy<f16>
!cute_nvgpu.atom.universal_copy<f16, 128 b>
!cute_nvgpu.atom.universal_copy<f16, 128 b, allow_dsmem>
!cute_nvgpu.atom.universal_copy<f16, mem_order=acquire, mem_scope=cluster>
```

The `b` suffix means bits. Keep the space before `b` to match this dialect's printer exactly.

### Atom Integer Type

Sub-byte and microscaling fragments print as integer widths with an optional
division factor:

```mlir
!cute_nvgpu.i4
!cute_nvgpu.i6
!cute_nvgpu.i8
!cute_nvgpu.i4<divby 2>
!cute_nvgpu.i2<divby 4>
```

The division factor controls packed-lane interpretation, not integer arithmetic.

## Enum Spellings

Universal copy atoms use PTX-like memory order and scope spellings.

| Enum | Spellings |
|---|---|
| Memory order | `relaxed`, `acquire`, `release`, `acq_rel`, `sc`, `mmio`, `constant`, `volatile` |
| Memory scope | `cluster`, `gpu`, `sys` |

An omitted order or scope is not the same as printing a default value. Printers elide absent fields rather than emit sentinel keywords.

## Alias Hints

The dialect may provide human-readable SSA aliases for common atom families.
Aliases are non-semantic and may be overridden by operation-level result naming.

```c
StringRef alias_for_type(Type type) {
    if (is_memref_family(type)) {
        return format("memref_%s_%d", element_name(type), rank(type));
    }

    if (is_copy_atom(type)) {
        return format("copy_%s", copy_atom_suffix(type));
    }

    if (is_mma_atom(type)) {
        return format("mma_%s_%s_%s_%s",
                      a_element_name(type),
                      b_element_name(type),
                      c_element_name(type),
                      shape_name(type));
    }

    return "";
}
```

Examples:

```mlir
%copy_sm90_tma_load = ...
%mma_f16_f16_f32_16x8x16 = ...
%memref_f16_3 = ...
```

## Attribute Text

The dialect does not parse `#cute_nvgpu.*` attributes. Operation attributes
such as `a_type`, `b_type`, `shape_MNK`, cache modes, scale-vector size, and
thread or byte identifiers should be represented with ordinary builtin or
operation-specific attribute syntax.

```mlir
// Good: op-owned attributes.
%atom = cute_nvgpu.make_sm120_mma_bs
    {shape_MNK = [16, 8, 32], vec_size = 32}

// Not a supported dialect attribute surface.
// #cute_nvgpu.some_attribute<...>
```

## Parser Strategy

Any efficient mnemonic dispatch will do, as long as it behaves as if it recognises the exact case-sensitive set above. Unknown type mnemonics produce a clear diagnostic naming both the bad token and the `cute_nvgpu` dialect.

```c
Type parse_cute_nvgpu_type(Parser *parser) {
    StringRef mnemonic = parser->parse_keyword();

    if (mnemonic == "smem_desc_view") {
        return parse_smem_desc_view(parser);
    }

    if (mnemonic == "atom.universal_copy") {
        return parse_universal_copy_atom(parser);
    }

    if (starts_with_atom_integer_prefix(mnemonic)) {
        return parse_atom_integer_type(parser, mnemonic);
    }

    Type type = lookup_zero_parameter_type(mnemonic);
    require(type != NULL);
    return type;
}
```

## Mnemonic Perfect-Hash Dispatch

The compiled mnemonic dispatcher (`sub_1826CF0`, 6164 bytes) is a hand-written length-keyed perfect-hash walk. It fetches a single token via `parseOptionalKeyword` through the `AsmParser` vtable, then compares the token against 27 precomputed entries in a fixed order. Each entry is a tuple `(length, first_qword, second_qword, tail_bytes)`; the comparison uses 8-byte unaligned loads XORed against the stored qword literal — the classic packed-XOR `memcmp` collapse. The walk order is the order the entries appear in the table below, and preserving it matters: the printer's slot index must match the parser's branch order so type-name resolution round-trips through identical decision-chain offsets.

The hash is perfect because every distinct mnemonic in the set has either a unique length or a unique first qword keyed on length. The compiler emits a chained linear walk rather than a switch table: each arm gates on `len == LEN` first, then on a fused XOR of one or two qwords, then on any remaining dword, word, and byte tail. A miss falls through to the next arm; a hit calls the per-mnemonic builder and sets `HIBYTE(v44) = 1` as the success sticky bit.

| # | Length | First qword (LE)         | q0 literal       | Second qword / tail              | Mnemonic |
|--:|-------:|--------------------------|------------------|----------------------------------|----------|
|  0 |     18 | `0x696E752E6D6F7461` | `"atom.uni"` | q1 `0x665F6C6173726576` `"versal_f"`, w@+16 `"ma"`        | `atom.universal_fma` |
|  1 |      8 | `0x616D6D2E30386D73` | `"sm80.mma"` | —                                                          | `sm80.mma` |
|  2 |     15 | `0x6170732E30386D73` | `"sm80.spa"` | d@+8 `"rse_"`, w@+12 `"mm"`, b@+14 `'a'`                   | `sm80.sparse_mma` |
|  3 |      8 | `0x616D6D2E39386D73` | `"sm89.mma"` | —                                                          | `sm89.mma` |
|  4 |      9 | `0x7365645F6D656D73` | `"smem_des"` | b@+8 `'c'`                                                 | `smem_desc` |
|  5 |     14 | `0x7365645F6D656D73` | `"smem_des"` | d@+8 `"c_vi"`, w@+12 `"ew"`                                | `smem_desc_view` |
|  6 |      8 | `0x616D6D2E30396D73` | `"sm90.mma"` | —                                                          | `sm90.mma` |
|  7 |      9 | `0x6D6D2E3030316D73` | `"sm100.mm"` | b@+8 `'a'`                                                 | `sm100.mma` |
|  8 |     12 | `0x6D6D2E3030316D73` | `"sm100.mm"` | d@+8 `"a_sp"`                                              | `sm100.mma_sp` |
|  9 |     12 | `0x6D6D2E3030316D73` | `"sm100.mm"` | d@+8 `"a_bs"`                                              | `sm100.mma_bs` |
| 10 |     15 | `0x6D6D2E3030316D73` | `"sm100.mm"` | d@+8 `"a_bs"`, w@+12 `"_s"`, b@+14 `'p'`                   | `sm100.mma_bs_sp` |
| 11 |     12 | `0x6D6D2E3032314D53` | `"SM120.mm"` | d@+8 `"a_bs"`                                              | `SM120.mma_bs` |
| 12 |     14 | `0x656D742E6D6F7461` | `"atom.tme"` | d@+8 `"m_lo"`, w@+12 `"ad"`                                | `atom.tmem_load` |
| 13 |     15 | `0x656D742E6D6F7461` | `"atom.tme"` | d@+8 `"m_st"`, w@+12 `"or"`, b@+14 `'e'`                   | `atom.tmem_store` |
| 14 |     13 | `0x7432732E6D6F7461` | `"atom.s2t"` | d@+8 `"_cop"`, b@+12 `'y'`                                 | `atom.s2t_copy` |
| 15 |     20 | `0x637365645F616D74` | `"tma_desc"` | q1 `0x745F726F74706972` `"riptor_t"`, d@+16 `"iled"`       | `tma_descriptor_tiled` |
| 16 |     21 | `0x637365645F616D74` | `"tma_desc"` | q1 `0x695F726F74706972` `"riptor_i"`, d@+16 `"m2co"`, b@+20 `'l'` | `tma_descriptor_im2col` |
| 17 |     13 | `0x616D742E6D6F7461` | `"atom.tma"` | d@+8 `"_loa"`, b@+12 `'d'`                                 | `atom.tma_load` |
| 18 |     14 | `0x616D742E6D6F7461` | `"atom.tma"` | d@+8 `"_sto"`, w@+12 `"re"`                                | `atom.tma_store` |
| 19 |     15 | `0x616D742E6D6F7461` | `"atom.tma"` | d@+8 `"_red"`, w@+12 `"uc"`, b@+14 `'e'`                   | `atom.tma_reduce` |
| 20 |     19 | `0x696E752E6D6F7461` | `"atom.uni"` | q1 `0x635F6C6173726576` `"versal_c"`, w@+16 `"op"`, b@+18 `'y'` | `atom.universal_copy` |
| 21 |     20 | `0x6D69732E6D6F7461` | `"atom.sim"` | q1 `0x5F636E7973615F74` `"t_async_"`, d@+16 `"copy"`       | `atom.simt_async_copy` |
| 22 |      9 | `0x73646C2E6D6F7461` | `"atom.lds"` | b@+8 `'m'`                                                 | `atom.ldsm` |
| 23 |      9 | `0x7374732E6D6F7461` | `"atom.sts"` | b@+8 `'m'`                                                 | `atom.stsm` |
| 24 |     28 | `0x6E6F6E2E6D6F7461` | `"atom.non"` | q1 `0x69745F636578655F` `"_exec_ti"`, q@+16 `"led_tma_"`, d@+24 `"load"` | `atom.non_exec_tiled_tma_load` |
| 25 |     29 | `0x6E6F6E2E6D6F7461` | `"atom.non"` | q1 `0x69745F636578655F` `"_exec_ti"`, q@+16 `"led_tma_"`, d@+24 `"stor"`, b@+28 `'e'` | `atom.non_exec_tiled_tma_store` |
| 26 |     30 | `0x6E6F6E2E6D6F7461` | `"atom.non"` | q1 `0x69745F636578655F` `"_exec_ti"`, q@+16 `"led_tma_"`, d@+24 `"redu"`, w@+28 `"ce"` | `atom.non_exec_tiled_tma_reduce` |

Entries 4, 15, and 16 (`smem_desc`, `tma_descriptor_tiled`, `tma_descriptor_im2col`) are zero-parameter sugared types that build straight through the MLIR type uniquer with a fixed `TypeID` global. Entries 5 and 20 (`smem_desc_view`, `atom.universal_copy`) carry inline sub-parsers for layout, scope, and order. Every other entry routes to a per-mnemonic builder trampoline whose address acts as a typed nonce in the dialect's registration table — the trampoline itself is a 3-byte `xor eax, eax; ret`, and the linker pins the function pointer as the opcode tag.

## `atom.i<N>_divby_<M>` Prefix Branch

Once all 27 literal arms miss, the dispatcher checks whether the token starts with `'i'` (0x69). If it does, the walk switches into a four-step sub-walk that reads the bare `i<decimal>` form first, then optionally consumes the `divby` keyword followed by a second decimal. This is the dialect's generic packed sub-byte fragment encoding — `i4`, `i6`, `i8` for plain widths; `i4_divby_2`, `i2_divby_4` for NVFP4-style microscaling fragments where the divisor controls packed-lane interpretation rather than arithmetic.

The walk is:

1. Read `N` as a decimal suffix after the leading `'i'`. The digit run is delimited by
   `std::find_if_not(tail, end, isdigit)` and converted with
   `StringRef::getAsInteger(10, &value)`.
2. Check that the remainder of the token is exhausted and that `N` fits in `uint32_t`.
3. Probe `parseOptionalKeyword("divby", 5, &cursor)` through the parser vtable. If the
   keyword is absent, the result is a plain `i<N>` atom integer type.
4. Read `M` as a second decimal and attach both integers to the `OperationState` via the
   `OperationState::addAttribute("divby", i<M>)` helper.

The bare `divby` keyword path lives entirely inside step 3 — not a separate dispatcher arm, but a sub-keyword that only ever follows a successful `i<N>` consumption. Reimplementers must place this prefix branch after every literal arm: anything starting with `'i'` followed by an all-digit tail lands here regardless of whether the digits form a semantically valid bit-width.

## SM120 Uppercase Quirk

Every SM70/80/90/100 arm uses a lowercase prefix (`sm80.`, `sm89.`, `sm90.`, `sm100.`). The SM120 arm uppercases it. The packed first-qword literal for entry 11 is `0x6D6D2E3032314D53`, decoding little-endian as `"SM120.mm"` — the bytes `4D 53` ('M', 'S') sit at offsets 0 and 1 of the qword, encoding the uppercase `SM` head. This is a genuine binary quirk preserved through the CUDA 13.1 release and confirmed against the verbatim qword constant in the decompilation. Any reimplementation must key the SM120 arm case-sensitively — never case-fold the prefix.

The table has exactly one SM120 variant: `SM120.mma_bs`. No `SM120.mma`, no `_sp`, no `_bs_sp` — the SM120 code path is gated to block-scaled MMA only, matching the consumer-Blackwell FP4 surface where sparse MMA is not exposed.

## Diagnostic

When all 27 arms and the `'i'`-prefix branch miss, the dispatcher emits:

```text
unknown  type `<mnem>` in dialect `<dialect>`
```

The literal carries two spaces between `unknown` and `type` — verbatim in the binary at `.rodata` offset `0x04CF76F7`. A compatible reimplementation must preserve it. Bad token and dialect name are both wrapped in backticks; the diagnostic is stitched from three separate `.rodata` fragments concatenated through the `InFlightDiagnostic::operator<<(StringRef)` chain.

## Perfect-Hash Compare Pseudocode

```c
typedef struct HashEntry {
    size_t      length;
    uint64_t    q0;
    uint64_t    q1;
    const char *tail;
    void      (*build)(ParseContext *);
} HashEntry;

int parseCuteNvgpuMnemonic(StringRef m, ParseResult *out) {
    static const HashEntry table[27] = {
        {18, 0x696E752E6D6F7461ULL, 0x665F6C6173726576ULL, "ma",     build_atom_universal_fma},
        { 8, 0x616D6D2E30386D73ULL, 0ULL,                  "",       build_sm80_mma},
        {15, 0x6170732E30386D73ULL, 0ULL,                  "rse_mma",build_sm80_sparse_mma},
        { 8, 0x616D6D2E39386D73ULL, 0ULL,                  "",       build_sm89_mma},
        /* ...22 more rows in walk order... */
        {12, 0x6D6D2E3032314D53ULL, 0ULL,                  "a_bs",   build_sm120_mma_bs},
        /* ...remaining rows... */
        {30, 0x6E6F6E2E6D6F7461ULL, 0x69745F636578655F ULL,
                                                          "led_tma_reduce",
                                                                    build_atom_non_exec_tiled_tma_reduce},
    };

    for (size_t i = 0; i < 27; ++i) {
        const HashEntry *e = &table[i];
        if (m.size != e->length)                                                   continue;
        uint64_t q0 = unaligned_load_u64(m.data + 0);
        if ((q0 ^ e->q0) != 0)                                                     continue;
        if (e->length > 8) {
            uint64_t q1 = unaligned_load_u64(m.data + 8);
            if ((q1 ^ e->q1) != 0)                                                 continue;
        }
        size_t tail_len = e->length > 16 ? e->length - 16 : 0;
        if (tail_len && memcmp(m.data + 16, e->tail, tail_len) != 0)               continue;
        e->build(out);
        return 1;
    }

    if (m.size > 0 && m.data[0] == 'i' && all_digits(m.data + 1, m.size - 1)) {
        return parse_atom_integer_with_optional_divby(m, out);
    }

    emit_error("unknown  type `%.*s` in dialect `cute_nvgpu`",
               (int)m.size, m.data);
    return 0;
}
```

The length gate collapses the 27-way decision into a constant-time per-bucket lookup. The compiler chains the arms as `if/else if` rather than building a switch table because the length-OR-first-qword key is collision-free across the entire mnemonic set, and the walk order must stay stable to preserve the printer-to-parser slot correspondence the dialect's round-trip self-test depends on.

## Invariants

- Type mnemonics are case-sensitive.
- `SM120.mma_bs` prints with uppercase `SM`; the packed qword literal
  `0x6D6D2E3032314D53` enforces this case-sensitively.
- The 27-entry perfect-hash walk order is preserved across builds so that printer slot
  indices match the parser's branch order.
- The `'i'`-prefix branch runs only after every literal arm has missed.
- Parameterized type printers and parsers are symmetric.
- Operation attributes do not require a dialect-scoped attribute parser.
- Alias hints are deterministic but never semantic.
- The unknown-type diagnostic uses two literal spaces between `unknown` and `type` and
  wraps both the bad token and the dialect name in backticks.

## Reimplementation Checklist

1. Implement all 27 zero/parameterized type mnemonics in the documented walk order.
2. Generate the length-keyed packed-XOR compare sequence per entry; loads may be unaligned.
3. Add parameter printers and parsers for `smem_desc_view`,
   `atom.universal_copy`, and atom integer types.
4. Implement the `atom.i<N>[_divby_<M>]` sub-walk after every literal arm.
5. Key the SM120 arm case-sensitively against the uppercase `"SM120.mm"` qword.
6. Keep memory order and memory scope fields optional.
7. Reject `#cute_nvgpu.*` dialect attributes unless a future dialect version
   explicitly adds them.
8. Add deterministic alias hints for memrefs, copy atoms, and MMA atoms.
9. Preserve the verbatim two-space `unknown  type` diagnostic and the backtick wrapping.
