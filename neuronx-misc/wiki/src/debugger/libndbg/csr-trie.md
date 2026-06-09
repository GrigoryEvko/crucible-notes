# CSR-Name Trie

The library converts textual register names into compact enum IDs via
three families of resolver functions, one per naming level:

| level   | resolver                                       | output type                        | distinct enum entries (all-arch) |
|---------|------------------------------------------------|------------------------------------|--------------------------------:|
| block   | `ndbg_csr_block_<arch>_resolve_name`           | `ndbg_csr_block_name_t`            |                          3,265 |
| bundle  | `ndbg_csr_bundle_<arch>_resolve_name`          | `ndbg_csr_bundle_name_t`           |                            140 |
| csr     | `ndbg_csr_<arch>_resolve_name`                 | `ndbg_csr_name_t`                  |                          1,182 |

Each per-arch resolver is one giant generated function. The shape is
the same at all three levels: a switch on `length`, then within each
length-bucket a chained `memcmp` / `strncmp` ladder.

## Sample: `ndbg_csr_block_cayman_resolve_name`

```c
ndbg_error_code_t __fastcall
ndbg_csr_block_cayman_resolve_name(ndbg_backend_t *backend,
                                   const char *str, int length,
                                   ndbg_csr_block_name_t *out)
{
  switch (length) {
    case 18:
      if (!memcmp(str, "TPB_3_SP_LOCAL_REG", 18)) {
        *out = NDBG_CSR_BLOCK_TPB_3_SP_LOCAL_REG; return NDBG_SUCCESS;
      }
      if (!memcmp(str, "TPB_6_SP_LOCAL_REG", 18)) {
        *out = NDBG_CSR_BLOCK_TPB_6_SP_LOCAL_REG; return NDBG_SUCCESS;
      }
      ... // ~5 more `memcmp` short-circuits
      if (strncmp(str, "TPB_4_SP_LOCAL_REG", 18))
        if (strncmp(str, "TPB_6_PE_LOCAL_REG", 18))
          if (strncmp(str, "TPB_0_SP_LOCAL_REG", 18))
            ... // chained nested binary search
            ;
          else *out = NDBG_CSR_BLOCK_TPB_0_SP_LOCAL_REG;
        else *out = NDBG_CSR_BLOCK_TPB_6_PE_LOCAL_REG;
      else *out = NDBG_CSR_BLOCK_TPB_4_SP_LOCAL_REG;
      break;
    case 19:
      ...
  }
}
```

Within each `case <length>:` block the compiler emits:

1. A few unrolled `if (!memcmp(...))` short-circuits for the
   high-probability names — i.e. the ones the generated code believed
   most likely to be queried (probably in original-source order).
2. After that, a chained `strncmp` decision tree that returns the
   non-zero comparison result as an int, branching on it to descend
   left or right. This is GCC's emission of a binary search over a
   sorted string list.
3. A default fall-through that sets `result = NDBG_CSR_BLOCK_UNRECOGNIZED`
   (= 9).

The exact case counts in the libndbg shipped:

| function                                       | # `memcmp` / `strncmp` calls | size (bytes) |
|------------------------------------------------|----------------------------:|-------------:|
| `ndbg_csr_block_cayman_resolve_name`           |                       2,145 |       95,538 |
| `ndbg_csr_block_mariana_resolve_name`          |                       2,418 |          ~96 K |
| `ndbg_csr_block_sunda_resolve_name`            |                         465 |       19,245 |

(The cayman variant has fewer matching string-comparisons than total
case branches because many short blocks share length and are resolved
by a single `memcmp` at the head of the bucket.)

The `ndbg_csr_block_name_to_string` function is the inverse — a single
switch with 3,265 cases, one per unique block-name enum value, returning
a `const char *` literal. It is the same shape but inverted: switch on
the enum, return a string. It is arch-INDEPENDENT (it knows every block
ever defined by any arch).

## The suggestions / autocomplete trie

A second, fundamentally different shape is used for the
`ndbg_csr_<level>_<arch>_suggestions` functions — i.e. the interactive
"as the operator types `M2S_`, what are five plausible completions?"
path. Instead of one giant function with a length-bucketed switch, the
suggestions code is a real *trie of C++ functions*, each function
representing one trie node:

```
ndbg_csr_block_cayman_suggestions(backend, str, length, out)
 │
 ├─ for i = 0..59:                          // try suggestion at every offset
 │   ├─ ndbg_csr_block_cayman_suggestions_trie_T(str, len, 0, out)
 │   │     if str[1]='O' → suggestions_trie_TOP_SP_(...)
 │   │     if str[1]='P' → suggestions_trie_TPB_(...)
 │   ├─ ndbg_csr_block_cayman_suggestions_trie_APB_(...)
 │   ├─ ndbg_csr_block_cayman_suggestions_trie_PREPROC_(...)
 │   ├─ ndbg_csr_block_cayman_suggestions_trie_TPB_(str, len, i-1, out)
 │   ├─ ndbg_csr_block_cayman_suggestions_trie_TOP_SP_(...)
 │   ├─ ndbg_csr_block_cayman_suggestions_trie_TPB_{0..7}_(str, len, i-4, out)
 │   ├─ ndbg_csr_block_cayman_suggestions_trie_APB_SE_(...)
 │   ├─ ndbg_csr_block_cayman_suggestions_trie_APB_IO_(...)
 │   ├─ deeper: trie_TPB_0_SP_LOCAL_REG, trie_TPB_0_DVE_LOCAL_REG, ...
 │   └─ return immediately when out->length == 5
```

Each terminal trie node has its own C++ symbol, e.g.
`_Z40ndbg_csr_block_cayman_suggestions_trie_TPKciiP26ndbg_csr_block_suggestions`
(`ndbg_csr_block_cayman_suggestions_trie_T(const char*, int, int, ndbg_csr_block_suggestions*)`).
A representative node body:

```c
ndbg_error_code_t
ndbg_csr_block_cayman_suggestions_trie_T(const char *str, int length,
                                         int offset,
                                         ndbg_csr_block_suggestions_t *out)
{
  char prefix[2] = "T";
  if (strncmp(str, &prefix[offset], min(length, 1 - offset)))
    return NDBG_SUCCESS;        // not our prefix → just return, do not add suggestions
  if (1 - offset >= length) {   // string is exhausted right here; emit a fixed set
    ndbg_csr_block_name_t names[5] = {449, 2761, 2762, 2763, 2764};
    memcpy(&out->suggestions[out->length], names,
           4 * (5 - out->length));
    out->length = 5;
    return NDBG_SUCCESS;
  }
  switch (str[1]) {              // descend by next char
    case 'O': return ndbg_csr_block_cayman_suggestions_trie_TOP_SP_(str+1, length-1, 0, out);
    case 'P': return ndbg_csr_block_cayman_suggestions_trie_TPB_   (str+1, length-1, 0, out);
    default:  return NDBG_SUCCESS;
  }
}
```

The output struct is just five enum IDs:

```c
typedef struct {
  uint32_t length;                              // 0..5
  ndbg_csr_block_name_t suggestions[5];
} ndbg_csr_block_suggestions_t;                  // 24 bytes total
```

Because the trie is unrolled into one function per node, the library
ships *16,284 such trie functions* across the three backends:

| backend  | # `_suggestions_trie_*` symbols |
|----------|--------------------------------:|
| cayman   |                          7,256 |
| mariana  |                          7,920 |
| sunda    |                          1,108 |

Each leaf is typically 200–400 bytes; the deeper interior nodes can be
larger. Mariana having more trie symbols than cayman matches the
mariana block namespace being larger (2,630 unique blocks vs cayman's
2,356) plus mariana's longer common prefix runs from its denser
`APB_IO_*` hierarchy.

The cap of 5 suggestions is enforced at every node via the
`if (out->length == 5) return NDBG_SUCCESS;` early-exit. The outer
`for (i = 0..59)` loop in the entrypoint represents trying each
possible *offset within the input string* — i.e. supporting
"find me block names whose substring matches my input starting at any
position", not just prefix matches. The upper bound 60 matches the
60-char limit on block names (the longest names like
`APB_IO_0_USER_FIS_IO_D2D_SUBSYS_0_DEBUG_FIS_0_INTERNAL_ELA` come close).

## Conditional suggestions

The `*_suggestions_conditional` variants take an extra `ndbg_csr_name_t
csr` or `ndbg_csr_block_name_t block` argument and emit only those
suggestions whose parent / child is compatible:

```c
ndbg_error_code_t
ndbg_csr_block_cayman_suggestions_conditional(ndbg_backend_t *backend,
                                              const char *str, int length,
                                              ndbg_csr_name_t csr,
                                              ndbg_csr_block_suggestions_t *out);
```

i.e. "given the operator has already pinned this CSR field, which
blocks could host it?" The cayman conditional variant is the largest
function in the entire library at 264,185 bytes / 12,932 basic
blocks — a fully cross-multiplied table of (block, csr) compatibility
walked as the trie descends. The mariana / sunda conditional variants
are smaller in absolute terms but follow the same shape.

## Per-string length distribution

A peek at how the resolver buckets cluster:

- The shortest block names are 17 chars (`APB_IOFAB_RDM_NTS`).
- The longest cross all 3 backends approach 95 chars
  (`APB_IO_1_USER_FIS_PEB_DFX_FIS_0_USER_ERRTRIG_TRIG_1` and similar).
- The single most populous length-bucket is around the 30–50 char range
  where the bulk of `APB_SE_<i>_SDMA_<j>_*` names live (these dominate
  the cayman/mariana namespaces, with 16 SDMA engines × 4 SE clusters
  × a handful of sub-block suffixes per SDMA = on the order of 100
  block names per "family").

The per-length first-cmp/binary-search choice means that lookup time
is bounded by `O(log N_length)` where `N_length` is the number of
block names sharing the same string length — typically ≤ 200 — i.e.
~8 comparisons in the worst case, all working on small strings.

## Source provenance

The internal source path embedded in the library:
`/opt/brazil-pkg-cache/packages/KaenaDebuggerLib/KaenaDebuggerLib-2.29.11.0/AL2_x86_64/generic-flavor/src/src/{cayman,sunda,debug_info,dma,ndrv}.c`
suggests the resolver tables are generated by a build-time script
(probably reading a register-database YAML and emitting `<arch>.c`
files containing the switch tables). The compiler's choice to chain
`strncmp` after the initial `memcmp` short-circuits is typical of
gcc's `-O2` switch-on-string lowering.
