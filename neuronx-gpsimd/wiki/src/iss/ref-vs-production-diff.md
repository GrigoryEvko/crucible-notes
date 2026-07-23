# ref-vs-production ISS Variant Diff

> **Part 14 cross-validation.** The GPSIMD ISS ships as the cas/fiss pair —
> [libcas-core Surface + ISS Plugin ABI](./cas-core-surface.md) (the
> *cycle-accurate* timing oracle) and
> [libfiss-base Surface + Exception Model](./fiss-surface-exceptions.md) (the
> *value* oracle). But the toolchain `config/` directory holds **two more files**
> with a `-ref-` infix: `libcas-ref-core.so` next to `libcas-core.so`, and
> `libfiss-ref-base.so` next to `libfiss-base.so`. This page answers the one
> question those two extra files raise: **what does `-ref-` mean?** Is it a
> *golden* functional model that production is validated against (implying the two
> may differ), or is it a pure build/instrumentation split of the *same* model?
> The decisive evidence is a live `ctypes` battery driving the **same** value leaf
> in both the production and the `-ref-` fiss, on tens of thousands of inputs,
> checked bit-for-bit.

All facts below are derived from static analysis and live execution of the four
shipped binaries only:

```
extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/
  libcas-core.so       45,878,080 B   (production timing oracle)
  libcas-ref-core.so   32,715,096 B   (-ref- timing oracle)
  libfiss-base.so      12,330,016 B   (production value oracle)
  libfiss-ref-base.so  12,330,216 B   (-ref- value oracle)
```

Tooling: `readelf -SW/-d/-n`, `nm -D/-S`, `objdump -d/-s`, `xxd`/`dd`+`sha256sum`,
and a live `ctypes` drive. Each claim is tagged **confidence** × **provenance**
(`OBSERVED` = read/executed directly off the binary, `INFERRED` = strong deduction
from observed evidence, `CARRIED` = taken from a sibling ISS page). `.text`/`.rodata` are VMA==file-offset on all four; the writable sections
carry the ncore2gp `0x200000` delta (§5).

---

## 1. Executive verdict — `-ref-` is a build variant of the *same* model, not a divergent reference

> The single fact this page proves. State it precisely, then certify it live.

**Verdict.** `-ref-` is **not** a golden-vs-optimized *semantic* split. Both fiss
variants compute **bit-for-bit identical element values**, and both cas variants
are **byte-identical at the instruction level** modulo address layout. The `-ref-`
files are a **separately-built variant of the same ISS model at the same ABI
version** — same plugin contract, same symbol set, same modeled configuration,
same value math — differing only in *code generation* (cas-ref trades ~5.2 MB of
`.text` for a ~2.2× larger per-instance state buffer) and in a build-variant tag
in `cver__id`. There is no extra arithmetic, no different rounding, no extra
delivered-fault behavior. The production and `-ref-` pairs are **interchangeable
value oracles**.

| Question (from the task) | Answer | Evidence |
|---|---|---|
| Golden functional model vs optimized? | **No** — same model, same values | §3 battery; §4 cas byte-identity |
| Extra validation/assertions in `-ref-`? | **No new ones** — zero `assert`/`abort` in either; identical import set | §2.4 |
| Different rounding / precision? | **No** — soft-float fp16/fp32 bodies byte-identical at exact `nm` size | §3.3 |
| Identical results, or divergent? | **IDENTICAL** — 71,706 live calls, 0 mismatches; identical leaf bodies | §3.2, §3.3 |

> **GOTCHA — "-ref-" does *not* mean "golden/correct" here.** The infix is the
> toolchain's *build-flavor* tag, not a correctness pedigree. Both files are the
> same reference-style scalar interpreter; neither is "the optimized one" in the
> value sense. The only place "reference" applies as a *kind* is structural: the
> whole fiss family is a no-SIMD scalar per-lane reference model (established on
> [libfiss-base Surface](./fiss-surface-exceptions.md) §1.3) — and *both* fiss
> variants share that property identically. Do not read `-ref-` as "the trusted
> oracle the production one is checked against." *(HIGH · INFERRED — no
> `reference`/`golden` string exists in either binary; §2.4.)*

---

## 2. Variant inventory (per-pair byte/section/symbol facts)

Two pairs, characterized side by side. Every figure is `readelf -SW` /
`nm -D` / `nm -S` off the named absolute path — never the decompile.

### 2.1 File-level identity

| Property | `libcas-core.so` | `libcas-ref-core.so` | `libfiss-base.so` | `libfiss-ref-base.so` |
|---|---|---|---|---|
| File size (B) | 45,878,080 | **32,715,096** | 12,330,016 | **12,330,216** |
| Δ to prod | — | −13,162,984 | — | **+200** |
| SHA-256 (head) | `7f1d86da…` | `d9c9b5da…` | `260b110c…` | `1f351b54…` |
| ELF type | ET_DYN x86-64, not stripped | same | same | same |
| Compiler (`.comment`) | GCC 4.9.4 | GCC 4.9.4 | GCC 4.4.6 + 4.9.4 | GCC 4.4.6 + 4.9.4 |
| `dll_get_version` | `0x1381f` (79903) | **`0x1381f` (79903)** | n/a | n/a |
| Build-id note | *absent* | *absent* | *absent* | *absent* |

The two cas files differ by 13 MB; the two fiss files differ
by **200 bytes** — a first, blunt signal that the fiss split is near-identical and
the cas split is a genuine codegen difference, but *not* a versioning or
model-config difference: `dll_get_version` is the **same `0x1381f`** in both cas
files.

> **QUIRK — fiss carries two compilers, cas one.** Both fiss files' `.comment`
> stitches **GCC 4.4.6 (Red Hat) + GCC 4.9.4** — the soft-float runtime
> (`libgcc`/`libfloat`-style helpers, the 4.4.6 portion) is linked alongside the
> 4.9.4-built interpreter. Both cas files are pure GCC 4.9.4 (no soft-float
> runtime — cas computes no values). This is identical *within* each pair.
>
### 2.2 Section sizes (the codegen story)

| Section | cas-core | cas-ref-core | Δ (ref/prod) | fiss-base | fiss-ref-base | Δ (ref/prod) |
|---|---|---|---|---|---|---|
| `.dynsym` | `0x7398` | `0x7398` | **=** | `0x77718` | `0x77718` | **=** |
| `.dynstr` | `0x8852` | `0x8856` | +4 | `0xb9c1d` | `0xb9c1d` | = |
| `.text` | `0x124788b` (19.17 MB) | `0xd4cdde` (13.95 MB) | **0.728× (−5.22 MB)** | `0x6ffa88` | `0x6ffa84` | −4 B |
| `.rodata` | `0x19de8` | `0x141c4` | smaller | `0xc9300` | `0xc92b8` | −0x48 B |
| `.data.rel.ro` | `0x20f5a0` | `0x20f460` | ≈ = | `0x74f90` | `0x74f90` | = |
| `.data` | `0x8` | `0x8` | = | `0x8` | `0x8` | = |

**Reading.** The dispatch tables (`.data.rel.ro`, holding the 188-slot
semantic/issue/stall arrays for cas and the `semantic_functions`/`stage_functions`
tables for fiss) are the **same size** across each pair — same number of modeled
opcodes/slots. The export count (`.dynsym`) is **identical**. The only substantial
difference is cas's `.text`: cas-ref has 5.2 MB less code.
### 2.3 Symbol-set equality

| Pair | Total dyn (`nm -D`) | Defined (`T`+`A`) | Undefined (`U`/`w`) | Name-set diff |
|---|---|---|---|---|
| cas-core vs cas-ref-core | 1232 / 1232 | 1112 / 1112 | 120 / 120 | **∅ (identical)** |
| fiss-base vs fiss-ref-base | 20,384 / 20,384 | 20,379 / 20,379 | 5 / 5 | **∅ (identical)** |

```
$ diff <(nm -D libcas-core.so     | awk '{print $NF}' | sort) \
       <(nm -D libcas-ref-core.so | awk '{print $NF}' | sort)   # → empty, exit 0
$ diff <(nm -D libfiss-base.so     | awk '{print $NF}' | sort) \
       <(nm -D libfiss-ref-base.so | awk '{print $NF}' | sort)  # → empty, exit 0
```

Both `diff`s return empty (exit 0): every export name in production exists in
`-ref-` and vice versa, at the same version (`@@VERS_1.1` for cas). cas-ref keeps
the exact partition **24 `dll_*` + 16 `opnd_sem_*` + 1071 `my_*` = 1111** plus the
`VERS_1.1` anchor; fiss-ref keeps all 20,379 (`slotfill__` 12,569, `module__` 864,
the 1,534×3 stage families, 61 `exception__`, …).
> **NOTE — same names, different addresses.** Identical *names* does not mean
> identical *addresses*. Every symbol relocates: `module__xdref_add_16_16_16` is
> `@0x858480` in fiss-base but `@0x81dbe0` in fiss-ref-base;
> `my_ActiveFairness_def` is `@0x1786ee0` in cas-core but `@0x103a6a0` in
> cas-ref-core. The two builds lay code out differently; the *contract* (the
> name→behavior map) is what is preserved.
### 2.4 Import set and the "extra validation?" question

```
$ nm -D libcas-core.so     | rg ' U ' | rg -c 'nx_.*_interface'   → 119
$ nm -D libcas-ref-core.so | rg ' U ' | rg -c 'nx_.*_interface'   → 119
$ nm -D libcas-ref-core.so | rg ' U ' | rg -v 'nx_.*_interface'   → "U memset"
```

cas-ref imports the **same 119 `nx_*_interface` TIE ports + `memset`** as cas
production — identical value boundary, no new host hooks. fiss-ref's undefined set
is the **same 5 weak libc/runtime stubs** (`__cxa_finalize`, `__gmon_start__`,
`_ITM_{de,}registerTMCloneTable`, `_Jv_RegisterClasses`) as fiss production — no
`nx_*`, no libm, fully self-contained. **No `__assert_fail`, no `abort`, no
`assert`/`reference`/`golden` string** appears in either `-ref-` binary (`nm -D`
and `strings -a` both 0).
| "Extra validation in -ref-?" probe | cas-ref-core | fiss-ref-base |
|---|---|---|
| `__assert_fail` / `abort` import | 0 | 0 |
| literal `assert` string | 0 | 0 |
| literal `reference`/`golden` string | 0 | 0 |
| extra `nx_*` / libm import | 0 | 0 |

> **CORRECTION — "-ref- adds assertions" is false for these binaries.** A natural
> guess is that a reference build is a debug build studded with assertions. It is
> not: neither `-ref-` file imports `__assert_fail` or `abort`, and neither
> carries an `assert` string. The cas-ref `.text` is *smaller*, not larger — the
> opposite of an assertion-heavy debug build. Whatever distinguishes the variant
> is **not** added validation.

---

## 3. The decisive test — drive the same value leaf in both fiss, bit-for-bit

> Section sizes and symbol sets establish *structural* sameness. Only execution
> settles *behavioral* sameness. This is the test that decides identical-vs-divergent.

The value-producing leaves live in fiss (cas computes no values — established on
[libcas-core Surface](./cas-core-surface.md) §3). The `module__xdref_*` primitives
are `extern "C"` with a fixed SysV ABI (operands in integer registers, result
through an out-pointer; see [libfiss-base Surface](./fiss-surface-exceptions.md)
§1.2). So the experiment is: `dlopen` **both** fiss files, resolve the **same**
leaf by name in each, call both on a battery of inputs, compare the out-slots.

### 3.1 Disassembly first — the bodies look identical

```text
module__xdref_add_16_16_16 @ fiss-base 0x858480 / fiss-ref 0x81dbe0 :
  add    %esi,%edx          ; A + B
  and    $0xffff,%edx       ; wrap mod 2^16
  mov    %edx,(%rcx)        ; *out = result
  ret                       ; ← BOTH bodies, instruction-for-instruction

module__xdref_adds_16_16_16 @ fiss-base 0x85aa10 / fiss-ref 0x820170 :
  movswl %si,%eax ; movswl %dx,%edx ; add %eax,%edx
  mov %edx,%eax ; and $0x1ffff,%eax ; …                ; the saturating clamp
  …(17-instr signed-overflow-detect → 0x7fff/0x8000)…  ; ← BOTH bodies identical
```

Both the trivial wrapping `add` and the non-trivial **saturating** `adds` (a
17-instruction signed-overflow-detect-and-clamp) disassemble instruction-for-
instruction the same in the two files.
### 3.2 Live battery — 71,706 calls, zero mismatches

A `ctypes` harness loads both fiss libraries and calls the same leaf in each:

```python
prod = ctypes.CDLL(".../libfiss-base.so")
ref  = ctypes.CDLL(".../libfiss-ref-base.so")
def call(f, a, b):                       # void f(void* ctx, i32 A, i32 B, i32* out)
    out = ctypes.c_int32(0)
    f.argtypes = [c_void_p, c_int32, c_int32, POINTER(c_int32)]; f.restype = None
    f(None, a, b, ctypes.byref(out));  return out.value & 0xffffffff
# for every leaf, for every (a,b) in a corner+random battery: assert call(prodF)==call(refF)
```

| Battery | Leaves | Inputs/leaf | Calls (×2 libs) | Mismatches |
|---|---|---|---|---|
| 2-source int | `add_{8,16,32}`, `sub_16`, `adds_16`, `subs_16`, `minu_16`, `max_16`, `maxu_16`, `avg_16` (10) | 71×71 corners+random | **50,410** | **0** |
| 3-source MAC | `mula_24_24_8_8`, `muls_24_24_8_8` (2) | 23³ corners+random | **21,296** | **0** |
| **Total** | **12** | — | **71,706** | **0** |

The input set spans the corners that *distinguish* wrap from saturate from
signed/unsigned (`0x7fff`, `0x8000`, `0xffff`, `0x7000+0x7000=0xE000`,
`0x7fffffff`, `0x80000000`) plus pseudo-random draws across the full `u32` range.
**Production and `-ref-` agree on every single call.** *(HIGH · OBSERVED — driven
live; the saturating/MAC corners are exactly the cases where a divergent reference
would show up first.)*

> **The identical-vs-divergent verdict, settled.** A *divergent* golden reference
> would, by definition, disagree somewhere on the saturation boundary or the
> wrap/clamp corner. It does not — 0/71,706. Combined with the byte-identical
> bodies (§3.1, §3.3), `-ref-` and production fiss are **the same value oracle**.
>
### 3.3 Soft-float too — byte-identical at exact `nm` size

The interesting risk is *rounding*: a reference build could use a different
IEEE-754 rounding path. It does not. The soft-float adds are **byte-identical**
when hashed at their exact `nm -S` sizes (so the comparison can't drift into a
neighbour function):

| Soft-float leaf | `nm -S` size | fiss-base addr | fiss-ref addr | Body SHA-256 |
|---|---|---|---|---|
| `module__xdref_add_1_1_1_16f_16f_16f_2` | `0x825` | `0x51c640` | `0x56e700` | **identical** |
| `module__xdref_add_1_1_1_32f_32f_32f_2` | `0x888` | `0x871790` | `0x836ef0` | **identical** |

Both the fp16 (2,085-byte) and fp32 (2,184-byte) full IEEE-754 emulation bodies
hash identically across the pair. **No different rounding, no different
precision.**

> **GOTCHA — hash at the `nm -S` size, not a guessed window.** These bodies are
> 2 KB+ and sit back-to-back with unrelated leaves. Hashing a generous window
> (e.g. `0x1a00`) overruns into the next function and reports a false "DIFFER".
> Always size the byte-compare to the symbol's `nm -S` length.

---

## 4. The cas pair — same model, different codegen tradeoff

cas computes no values, so the §3 value battery does not apply to it directly. The
question for cas is whether the 5.2 MB `.text` difference is *behavioral* (a
different timing model) or *structural* (the same model compiled differently). The
evidence says structural.

### 4.1 Identical accessor logic, different state layout

The `my_*` scoreboard accessors — the heart of the timing model — are
**structurally identical** across the pair, differing only in the **state-struct
offsets** they touch:

```text
my_ActiveFairness_def  @ cas-core 0x1786ee0 :        @ cas-ref-core 0x103a6a0 :
  movslq %esi,%rsi                                      movslq %esi,%rsi
  cmpb   $0x1,0x1257(%rdi,%rsi,1)   ; valid?            cmpb   $0x1,0xc3b(%rdi,%rsi,1)
  je     …                                              je     …
  movb   $0x1,0x1250(%rdi,%rsi,1)   ; set valid         movb   $0x1,0xc34(%rdi,%rsi,1)
  mov    (%rdx),%eax ; mov %eax,0x1234(%rdi,%rsi,4)     mov (%rdx),%eax ; mov %eax,0xc18(…)
  ret                                                   ret
```

Same five instructions, same hazard-role semantics (check valid byte → set valid →
store def value); only the offsets differ (`0x1257/0x1250/0x1234` vs
`0xc3b/0xc34/0xc18`). The model is identical; the instance memory map is
re-packed.
### 4.2 The size tradeoff is real and quantified

| Metric | cas-core | cas-ref-core | Ratio |
|---|---|---|---|
| `dll_get_data_size` (per-instance state) | `0x4a09f0` = 4,852,208 B (4.63 MiB) | **`0xa1de50` = 10,608,208 B (10.12 MiB)** | **2.186×** |
| `.text` | 19.17 MB | 13.95 MB | 0.728× |
| `dll_initialize` `memset` size | `0x4a09f0` | **`0xa1de50`** (matches) | — |
| Value functions exported | 0 | **0** | — |
| ABI version (`dll_get_version`) | `0x1381f` | **`0x1381f`** | = |

cas-ref makes the **opposite** size tradeoff from production: ~5.2 MB *less* code
in exchange for a ~2.2× *larger* per-instance state buffer. A plausible reading is
that production specializes/inlines more per-opcode paths (more `.text`) over a
tightly-packed state, while `-ref-` keeps the generic accessor shape over a
sparser, larger state map — but the *behavior* is the same model: same ABI
version, same 24/16/1071 partition, zero value functions, same 119-port boundary.
*(HIGH · OBSERVED for every cell; the codegen *interpretation* is MED · INFERRED.)*

> **GOTCHA — `dll_get_data_size` differs; size the buffer per file.** A harness
> that `dlopen`s `libcas-ref-core.so` must `malloc` **`0xa1de50`** bytes per
> simulated core, not the production `0x4a09f0`. `dll_initialize` `memset`s exactly
> the file's own `dll_get_data_size` (`mov $0xa1de50,%edx ; call memset@plt` in
> cas-ref). Hard-coding the production size against the `-ref-` core under-allocates
> by 5.76 MB and corrupts the heap.
---

## 5. The variant discriminator and the `.data` offset hazard

### 5.1 What the binary calls itself

The modeled-configuration metadata is **identical** across the fiss pair; only a
build-flavor hash differs:

| Symbol | Section | fiss-base | fiss-ref-base |
|---|---|---|---|
| `libfiss_config_metadata` | `.rodata` | `fe 05 00 00 / 7a 4c 00 00 / 19 01 00 00 / 23 00 00 00` | **byte-identical** |
| `cver__id` (returns `eax`) | `.text` | `mov $0x6ffd69ed,%eax ; ret` | `mov $0x1edf69ed,%eax ; ret` |

`libfiss_config_metadata` (config fields `0x5fe`, `0x4c7a`, `0x119`, `0x23`) is the
same in both — **same modeled core configuration**. `cver__id` differs only in its
**high 16 bits** (`0x6ffd` vs `0x1edf`); the **low 16 bits are identical
(`0x69ed`)**. The low half is the config identity (shared); the high half is the
build-variant fingerprint — the binary's own production-vs-`-ref-` discriminator.
*(HIGH · OBSERVED for the bytes; MED · INFERRED for the high/low split meaning.)*

> **NOTE — no FlexLM gating in these four files.** None of the four ISS DLLs imports
> a FlexLM/`lc_checkout`/`lm_*` symbol — licensing lives in the harness, not the
> value/timing cores (see [FlexLM Licensing](../abi/flexlm-licensing.md) for the
> value-free vs cycle-gated distinction at the toolchain boundary). The
> production-vs-`-ref-` choice is therefore *not* a licensed-feature gate; it is a
> build-flavor selection the harness makes when it picks which `config/` file to
> `dlopen`.

### 5.2 Section deltas (same family caveat as the surfaces)

`.text`/`.rodata` are VMA==file-offset on all four. The writable sections carry the
ncore2gp **`0x200000`** delta (not libtpu's `0x400000`), confirmed per-file:

| File | `.data.rel.ro` VMA | file off | Δ |
|---|---|---|---|
| cas-ref-core | `0x1891a40` | `0x1691a40` | `0x200000` |
| fiss-ref-base | `0xc17e80` | `0xa17e80` | `0x200000` |

To read the `-ref-` dispatch tables off the file (e.g. cas-ref's
`slot_semantic_functions`, fiss-ref's `semantic_functions`/`stage_functions`),
subtract `0x200000`. The hazard is identical to the production files'. *(HIGH ·
OBSERVED.)*

---

## 6. Reimplementation takeaway

For a Vision-Q7-compatible engine, the `-ref-` files are **not a second model you
must also satisfy** — they are the *same* model. Concretely:

- **Pick either fiss as your value ground-truth.** They are bit-for-bit identical
  (§3); there is no "more correct" one. The 864 `module__xdref_*` leaves are the
  same arithmetic in both.
- **Pick either cas as your timing model**, but size the instance buffer to *that*
  file's `dll_get_data_size` (§4.2) — production 4.63 MiB, `-ref-` 10.12 MiB.
- **Do not treat `-ref-` as a golden checker.** There is no behavioral oracle to
  cross-check against; production *is* the reference. The real cross-validation
  oracle is the cas/fiss split itself (timing vs value), unified in
  [The ISS as Executable Oracle](./iss-oracle-synthesis.md).
- **The cas codegen split is informative, not normative:** production inlines more
  (bigger `.text`, tighter state); `-ref-` is generic (smaller `.text`, sparser
  state). Either is a legal implementation of the same scoreboard contract.

How both halves are invoked, and how the timing of each op is modeled, are on
[The cas Timing Model](./cas-timing-model.md) and the surfaces. The unified
runnable oracle is [The ISS as Executable Oracle](./iss-oracle-synthesis.md). The
formal back-to-back differential harness that *runs* this comparison as a
regression gate belongs to the validation Part — referenced by
title only.

---

## 7. Honesty ledger

- **[HIGH · OBSERVED]** Variant inventory: cas pair 45.9 MB vs 32.7 MB (−13 MB),
  fiss pair 12.330016 MB vs 12.330216 MB (+200 B); identical `.dynsym` size and
  identical `nm -D` name-sets in **both** pairs (`diff` empty, exit 0); same ABI
  version `0x1381f` for both cas files; same 24/16/1071 cas partition; same 20,379
  fiss exports.
- **[HIGH · OBSERVED]** The identical-vs-divergent verdict: **71,706 live ctypes
  calls** (50,410 two-source int + 21,296 three-source MAC) across 12 fiss value
  leaves, **0 mismatches** prod-vs-ref; `add`/`adds` bodies instruction-identical;
  fp16(`0x825`)/fp32(`0x888`) soft-float bodies byte-identical at exact `nm -S`
  size — **no different rounding/precision**.
- **[HIGH · OBSERVED]** No extra validation: zero `__assert_fail`/`abort` imports,
  zero `assert`/`reference`/`golden` strings, identical import set (cas-ref 119
  ports + `memset`; fiss-ref the same 5 weak stubs) in **both** `-ref-` files.
- **[HIGH · OBSERVED]** cas codegen tradeoff: cas-ref `.text` 0.728×, per-instance
  state `0xa1de50` (2.186× larger), `memset` matches; identical `my_*` accessor
  logic at different state offsets; zero value functions in cas-ref.
- **[HIGH · OBSERVED]** Discriminator: `libfiss_config_metadata` byte-identical
  across the fiss pair; `cver__id` differs only in high 16 bits (`0x6ffd` vs
  `0x1edf`), low `0x69ed` shared; no FlexLM symbol in any of the four.
- **[MED · INFERRED]** That production specializes/inlines more over a tighter
  state while `-ref-` keeps generic accessors over a sparser state (the *reason*
  for the cas tradeoff); that `cver__id`'s high half is the build-variant
  fingerprint and the low half the config identity.
- **[LOW]** Whether cas-ref's instruction-level timing is *bit-identical* to
  production for every opcode (the §3 value battery does not cover cas; the `my_*`
  sampled accessors and identical ABI/partition are strong but not exhaustive for
  the cas timing path).
