# Pseudo-Instruction Expansion & the Adler-32 Macro Registry

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). Other versions differ.*

Some PTX instructions do not map 1:1 onto SASS. Instead ptxas expands them into a
sequence of *other* PTX (or internal pseudo-PTX) by instantiating a printf-style
template from its encrypted macro-expansion pool (see
[String-Pool Encryption](../reference/string-pool-cipher.md)). The dispatch from a
PTX mnemonic to its expansion handler is set up by a single registrar,
`sub_5D4190`, run during front-end initialization.

## The registrar

`sub_5D4190` calls a registration primitive — `sub_426150(dict, key, handler)` —
once per pseudo-instruction. It populates **two separate dictionaries** held on the
parser context:

| Dictionary | Context field | Keyed by | Count | Examples |
|---|---|---|---|---|
| Named pseudo-ops | `*(ctx+808)` | mnemonic string | 115 | `membar`, `div`, `div.full`, `rcp`, `cvt`, `tex.grad`, `vadd4`, `dp4a`, `wmma.load.a`, `mma`, `wgmma.mma_async`, `multimem.ld_reduce`, `tcgen05.mma`, `_tcgen05.guardrails.*` |
| Macro builtins | `*(ctx+816)` | **decimal number string** | 473 | `"1030557441"`, `"2644314910"`, `"605425506"`, … |

Each registration pairs the key with a dedicated expansion handler (e.g.
`multimem.ld_reduce` → `sub_58D8B0`, `"1030557441"` → `sub_50FCE0`). The named
dictionary holds the user-visible pseudo-ops; the numeric dictionary holds the
internal `__cuda_*` macro builtins those handlers expand into.

## What the numeric keys are

The 473 numeric keys look encrypted, but they are not. **Each decimal string is the
Adler-32 hash of a `__cuda_*` macro-builtin name, rendered in base 10.** Release
builds strip the symbolic macro name and keep only its hash as the dictionary key.

This is fully reversible and was confirmed against the binary: harvesting every
`__cuda_*` identifier from the decoded macro pool and the string tables, then
matching `adler32(name)` against the registry, resolves **all 473 keys with zero
unmatched and zero collisions**. The worked example:

```text
1030557441  =  0x3D6D0F01  =  adler32("__cuda_sm8x_tf32_wmma_m16n16k8_load_a_col")
```

Adler-32 stores `(B << 16) | A`, where `A = 1 + Σ bytes (mod 65521)` and
`B = Σ A (mod 65521)`. For the value above, `A = 0x0F01`, `B = 0x3D6D`. Because the
hashed names are long and share prefixes, families of related builtins land in
adjacent hash neighborhoods — which is why the keys *appear* to carry a structured
high-byte/low-byte pattern. They do not encode operand or vector types; the pattern
is purely an artifact of summing similar-length strings. The `tf32` WMMA load family
makes this concrete:

```text
0x3D6D0F01  __cuda_sm8x_tf32_wmma_m16n16k8_load_a_col
0x3D720F02  __cuda_sm8x_tf32_wmma_m16n16k8_load_b_col
0x3DA50F1B  __cuda_sm8x_tf32_wmma_m16n16k8_load_a_row
0x3DAA0F1C  __cuda_sm8x_tf32_wmma_m16n16k8_load_b_row
0xB1CF11D7  __cuda_sm8x_tf32_wmma_m16n16k8_load_a_col_shared
0xB1A311D1  __cuda_sm8x_tf32_wmma_m16n16k8_load_a_col_global
```

The bare `load_a_col` / `load_b_col` variants cluster at `0x3D**0F**`; appending the
`_shared` / `_global` address-space suffix lengthens the string and moves the hash to
`0xB1**11**` / `0xB2**11**`. Same instruction, different macro — different hash, by
construction.

### Recovering the names

Given the macro pool, recovering every name is a one-liner — hash the candidate
names and invert the map:

```python
import re, zlib
names = set(re.findall(rb"__cuda_[A-Za-z0-9_]{2,90}", open("macro_pool.bin","rb").read()))
table = {zlib.adler32(n) & 0xFFFFFFFF: n.decode() for n in names}
print(table[1030557441])   # b'__cuda_sm8x_tf32_wmma_m16n16k8_load_a_col'
```

The recovered builtins span the WMMA/TF32 fragment-load and accumulator families,
the `tcgen05` guardrail and allocation helpers, vote/ballot and active-mask queries
(`__cuda_sm70_query_activemask`), `wmma` up/down-convert paths, and the integer-WMMA
accumulator-pointer updaters — i.e. the typed leaf macros the named pseudo-ops lower
into.

## Why two dictionaries

The split mirrors the two layers of PTX lowering. A named pseudo-op (e.g.
`wmma.load.a`) is the *grammar-level* instruction the parser accepts; its handler
selects a concrete *typed* macro builtin (e.g.
`__cuda_sm8x_tf32_wmma_m16n16k8_load_a_col`) according to the parsed shape, layout,
type, and address space, then expands that builtin's template from the macro pool.
The numeric dictionary is the lookup from a chosen builtin's hash to its expansion
handler, keeping the (large) set of typed leaf expansions addressable without
shipping their names.

## Where the 1,080 prototypes come from (473 + 607)

The 473 expansion handlers should not be confused with the **1,080-case** dispatch in
the [Prototype Emitter](./prototype-emitter.md) — they are two different stages:

- **Declaration** — `sub_5FF700` is a 1,080-case dispatch that emits the
  `.weak .func` / `.FORCE_INLINE .func` PTX *prototype string* for a `__cuda_*` helper,
  indexed by a dense id (0–1,079) assigned by the body-template registrar `sub_5D7430`.
  Every helper referenced by emitted PTX needs its declaration appended before SASS
  lowering — e.g. `case 1078` emits the prototype for `__cuda_sm20_div_s16`. This is the
  full helper set: 1,080 = the entries in `embedded_ptx_intrinsics.json`.
- **Body** — the 473 numeric (Adler-32-keyed) handlers here provide a macro-pool PTX
  *expansion* for a helper. Only a subset of the 1,080 have one.

The 1,080 declared helpers decompose exactly by where their body lives:

| Body source | Count | Where |
|---|---|---|
| macro-pool PTX expansion | 473 | `sub_5D4190` numeric dict (Adler-32-keyed) |
| precompiled SASS function stub | 607 | the high-entropy `.rodata` stub region (607-entry offset index) |
| **total declared** | **1,080** | `sub_5FF700` prototype emitter / `embedded_ptx_intrinsics.json` |

All 473 numeric-handler names are a strict subset of the 1,080 (verified: 473 ∩ 1,080 =
473). So a case index like 1078 belongs to the *declaration* table — which spans every
helper — not the 473-entry *expansion* registrar; the two are different stages with
different sizes.

## Template anatomy

Every builtin — numeric and named — expands a brace-delimited template stored in the
macro pool. The numeric (Fermi-macro) form is scaffolded by a predicate guard:

```text
{
  %s bra __skip_<name>;        // %s = activation predicate (inverted)
  …shadow registers (__ptxMacroFuncsFermi_<name>_*)…
  <inner expansion block, %s operand slots filled at instantiation>
  __skip_<name>:
}
```

The `__skip_<name>` / `__ptxMacroFuncsFermi_<name>_` scaffolding is pervasive in the
decoded pool (≈950 skip labels, ≈10,700 shadow-register references). Helper
subroutines are forward-declared with `_gen_proto <name>;` and invoked via `call`, so
common address-generation logic is factored out of the leaf macros. Named pseudo-ops
use the same pool but are anchored by a `$..._cuda_<op>_DONE` label and dispatched by
op name. A recurring shape is an *aligned fast path* guarded against an
*unaligned convergence-synthesis* path (`match.all` / `bra.conv` / `_warpsync`).

## Builtin families

The 588 handlers (473 numeric + 115 named) classify cleanly by family and target arch
(recovered from the `__cuda_smXX_` prefixes and the pool templates; full table in the
repo at `decoded/ptxas-pseudo-instructions/macro_catalog.tsv`):

| Family | Count | Family | Count |
|---|---|---|---|
| WMMA fp16/bf16 | 220 | bit/int (bfind/brev/clz/popc) | 8 |
| WMMA int8 (sm72) | 114 | tex/surface | 7 |
| WMMA sub-byte (s4/u4) | 38 | cp.async / bulk | 7 |
| WMMA 1-bit (XOR-POPC) | 38 | match | 6 |
| tcgen05 | 27 | WMMA up/down-convert | 4 |
| WMMA fp64 | 24 | wgmma | 4 |
| video (`v*`) | 23 | createpolicy | 4 |
| barrier / bar | 14 | vote / shfl / multimem / dp4a | 3 each |
| WMMA tf32 | 12 | math (div/rcp/sqrt/tanh) | 9 |

Fragment shapes map to data type: `m16n16k16`/`m32n8k16`/`m8n32k16` = fp16/int8,
`m8n8k32` = sub-byte, `m8n8k128` = 1-bit, `m16n16k8` = tf32, `m8n8k4` = fp64. Arch
distribution skews to sm_7x (≈229: Volta/Turing tensor core) and sm_72 (114: integer
WMMA), with sm_8x (≈80: tf32/fp64), sm_70 (≈40: collectives), and sm_10x (9: tcgen05).

## Notable expansions

These were verified against the decoded pool, not assumed:

- **WMMA loads/stores are not hardware instructions** — they are manual lane→element
  gather/scatter sequences: compute a per-lane byte offset from `%laneid`, then a run
  of `ld`/`st`. The `*_update_ptr` variants emit *only* the address math (no load) and
  are `call`ed by the full load/store macros. `_shared`/`_global` specialize the
  address space; `_desc` threads a 64-bit descriptor. Only `wmma.mma` / `mma` /
  `ldmatrix` / `movmatrix` / `stmatrix` emit a real tensor-core instruction.
- **tcgen05 (Blackwell)** — TMEM state lives in a reserved shared region
  (`__nv_reservedSMEM_tcgen05_partition`); eight guardrail checks run inline before
  ld/st/cp/mma and `trap` on violation (phase validity, column allocation, warp-owner
  via subpartition `(addr & 0x600000) >> 21`, 512-column physical bounds, power-of-two
  granularity, datapath alignment, sparsity consistency/usage). Two of the eight exist
  only as inline pseudo-ops with no standalone shadow body.
- **wgmma (Hopper)** — `wgmma.fence`→`warpgroup.arrive`, `commit_group`→
  `warpgroup.commit_batch`, `wait_group`→`warpgroup.wait N`, `mma_async`→
  `mma.warpgroup` with an inverted C-init predicate.
- **createpolicy** packs a 64-bit L2 policy descriptor (range base `0x18..`,
  fractional base `0x10..`, block-size bits derived from the window size).
- **video `v*` ops** are software-emulated on sm_50+: `prmt.b32` is the byte
  pack/unpack engine, `lop3.b32` does masked merges, `dp4a` does cross-lane reduction.
- **`div`/`rem`/`rcp`** are not inlined — they `call` `__cuda_sm20_*` reciprocal
  subroutines (approx → Newton correction → sign fixup); `tensormap.replace` patches
  descriptor fields with a uniform read-modify-write (`ld → shl → lop3 → st`).

## Cross-References

- [String-Pool Encryption](../reference/string-pool-cipher.md) — the encrypted macro pool whose `__cuda_*` templates these handlers expand.
- [PTX Parser (Flex + Bison)](../pipeline/ptx-parser.md) — the grammar front-end and the instruction-registration tables.
- [Prototype Emitter](./prototype-emitter.md) — the typed-prototype dispatch that selects among instruction forms.

*The numeric-name puzzle was raised by redplait ([denvdis](https://github.com/redplait/denvdis)); this page documents the mechanism as recovered from the ptxas binary.*
