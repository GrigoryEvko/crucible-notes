# CUDA Pragma & NVVM Annotation Registry

CUDA programs steer cicc through three orthogonal channels: source-level pragma directives (`#pragma unroll`, `#pragma nv_diag_suppress`, `#pragma nv_abi`, `#pragma nvopt`), declaration-attached attributes (`__launch_bounds__`, `__cluster_dims__`, `__grid_constant__`, `__maxnreg__`), and the resulting NVVM-IR-level metadata (`nvvm.annotations`, `!llvm.loop.*`, function-attached `"nvvm.maxntid"` attributes). This page documents the complete pipeline by which an EDG parse hit becomes an IR-level annotation, the byte-tag table that EDG uses internally, every `nvvm.annotations` tag the back-end consumes, and the dispatch function (`sub_A84F90`) that walks the metadata at IR-import time and decomposes each tuple into per-function attribute bags. Target audience: a senior compiler engineer who needs to reimplement the EDG→NVVM bridge or write a tool that produces cicc-compatible bitcode without going through the C++ front-end. Take-away: cicc's annotation surface is far wider than the NVPTX user manual documents — there are at least 14 EDG-internal attribute slots, 9 distinct NVVM annotation keys, 4 function-attached attribute strings shared with the annotation keys, and 7 well-formed `nvopt<...>` loop tags. All are recoverable from the binary by tracing four string clusters.

## At a Glance

| Channel | Lives in | Read by | Persists across |
|---|---|---|---|
| `#pragma` directive | EDG parser state | EDG attribute mapper `sub_5C79F0` | One declaration |
| `__attribute__` / `__launch_bounds__` | EDG declaration record | EDG IR emitter | One function |
| `nvvm.annotations` named metadata | NVVM IR module | `sub_A84F90` (dispatcher) → per-tag accessors `sub_CE8D40` family | Whole module, survives LTO |
| Function string attribute `"nvvm.maxntid"` etc. | `Function::Attributes` | Code generator, register allocator | One function, set late |
| `llvm.loop.*` MDNode chain | LLVM IR metadata on latch branch | Loop optimizer (`sub_19BB5C0` `computeUnrollCount`) | One loop |
| `nvopt<...>` loop tag | Pass-manager scheduling metadata | Pipeline assembler `sub_226C400` | One loop body |

The three lanes are tied together by a single invariant: **every user-facing pragma or attribute is materialized as either a `nvvm.annotations` tuple, a function `Attribute` string, or an `llvm.loop.*` metadata node before LLVM optimization begins.** No optimizer pass ever re-parses pragma text. This is enforced by EDG's IR emitter, which lowers attributes to metadata during NVVM IR generation (see [NVVM IR Generation](./ir-generation.md)).

## 1. EDG-Side Attribute Byte Tags

EDG stores each declaration attribute as a 1-byte tag in the symbol's flag bits. Function `sub_5C79F0` is the inverse map — given a byte tag, it returns the human-readable keyword used in diagnostics. Decompilation of the full switch yields the following table (HIGH — every keyword is a string literal cross-referenced by this function only):

| Tag (hex) | Tag (ASCII) | Keyword | Where applied |
|---|---|---|---|
| `0x56` | `V` | `__host__` | Function declarations |
| `0x57` | `W` | `__device__` | Function/variable declarations |
| `0x58` | `X` | `__global__` | Kernel function declarations |
| `0x59` | `Y` | `__tile_global__` | Tile-shared kernel functions (HIGH — string exists; semantics inferred) |
| `0x5A` | `Z` | `__shared__` | Variable declarations |
| `0x5B` | `[` | `__constant__` | Variable declarations |
| `0x5C` | `\` | `__launch_bounds__` | `__global__` functions |
| `0x5D` | `]` | `__maxnreg__` | `__global__` functions |
| `0x5E` | `^` | `__local_maxnreg__` | `__global__` functions (function-local override) |
| `0x5F` | `_` | `__tile_builtin__` | Compiler-emitted tile functions |
| `0x66` | `f` | `__managed__` | Variable declarations |
| `0x6B` | `k` | `__cluster_dims__` | `__global__` functions, sm_90+ only |
| `0x6C` | `l` | `__block_size__` | `__global__` functions (cluster sibling) |
| `0x72` | `r` | `__nv_pure__` | `__device__` functions (NVIDIA's pure-function attribute) |

```c
/* Reconstructed body of sub_5C79F0 — EDG attribute byte → keyword map. */
const char *edg_attr_keyword(const edg_attr_t *attr) {
    /* attr+0   : qualified-name pointer        */
    /* attr+8   : tag byte (this switch)        */
    /* attr+16  : raw spelling (printable name) */
    /* attr+24  : qualifier prefix (e.g. "C::") */
    const char *prefix = attr->qual_prefix;
    const char *raw    = attr->raw_spelling;
    if (prefix) {                                        /* qualified case */
        int n = sprintf(g_attr_scratch, "%s::%s", prefix, raw);
        raw = intern(g_attr_scratch, n);                 /* canonicalise   */
    }
    switch (attr->tag) {
        case 'V': return "__host__";
        case 'W': return "__device__";
        case 'X': return "__global__";
        case 'Y': return "__tile_global__";
        case 'Z': return "__shared__";
        case '[': return "__constant__";
        case '\\':return "__launch_bounds__";
        case ']': return "__maxnreg__";
        case '^': return "__local_maxnreg__";
        case '_': return "__tile_builtin__";
        case 'f': return "__managed__";
        case 'k': return "__cluster_dims__";
        case 'l': return "__block_size__";
        case 'r': return "__nv_pure__";
        default:  return raw ? raw : "<anonymous-attr>";
    }
}
```

> ⚡ **QUIRK — non-contiguous tag range**
> The byte tags occupy `0x56..0x6C` but with two visible gaps (`0x60..0x65`, `0x67..0x6A`, `0x6D..0x71`). Those slots are not stray free space — they belong to attributes that exist in EDG's internal model but have no diagnostic string (compiler-internal flags such as auto-`__device__` propagation from `-default-device`). When implementing a bitcode producer that round-trips EDG behaviour, you must never reuse these byte values for new attributes: the EDG semantic analyser also reads them, and any tag outside the documented map degrades to "anonymous attribute" rather than producing an error.

## 2. The `#pragma nv_abi` ABI-Override Slot

`#pragma nv_abi` is unique in cicc: it is the only pragma that **mutates the per-function ABI** before code generation, distinct from `__launch_bounds__` (which sets metadata) or `__nv_pure__` (which sets a function attribute). All ten diagnostic strings cluster at `0x3A53C90..0x3A53EC8`, which gives a complete picture of the validator. (HIGH — strings verbatim; ordering of checks inferred from typical EDG semantic-action layout.)

```c
/* Reconstructed semantic-action body for `#pragma nv_abi <arg>`. */
edg_diag_t process_pragma_nv_abi(edg_decl_t *decl, edg_token_t *tok) {
    if (decl == NULL)                       return DIAG("must appear immediately before a function declaration, function definition, or an expression statement");
    if (!(decl->space_flags & SPACE_DEVICE))return DIAG(decl->space_flags & SPACE_HOST
                                                       ? "is not supported inside a host function"
                                                       : "must be applied to device functions");
    if (tok == NULL || tok->kind == TOK_END)return DIAG("requires an argument");
    edg_expr_t *e = parse_expr(tok);
    if (!edg_is_icexpr(e))                  return DIAG("argument must evaluate to an integral constant expression");
    int64_t v = edg_icexpr_value(e);
    if (v < 0)                              return DIAG("argument value must be a positive value");
    if (v < INT32_MIN || v > INT32_MAX)     return DIAG("argument value exceeds the range of an integer");
    if (!nv_abi_option_valid(v))            return DIAG("contains an invalid option");
    if (nv_abi_already_set(decl, v))        return DIAG("contains a duplicate argument");
    if (!nv_abi_value_valid_for(decl, v))   return DIAG("contains an invalid argument value");
    decl->abi_overrides |= (1ULL << v);     /* set bit, attach to decl */
    return DIAG_OK;
}
```

The bit-packed `abi_overrides` field is what survives into NVVM IR — typically encoded into the function's `"nvvm.abi.opts"` string attribute. The exact bit-meaning of each option index is gated by the host-tool driver and not visible from cicc alone (LOW).

## 3. EDG `#pragma nvopt` — Per-Loop Optimisation Override

`#pragma nvopt N` (where `N` is an integer constant) attaches a numeric optimisation strength to the immediately-following loop. It is cicc's only mechanism for **per-loop O-level override** and the only way for source code to opt a single loop into the `Ofast-compile` tier. The diagnostic key cluster `nvopt_pragma_*` (in `cicc_strings.json` at `0x3A0F6DF..0x3A0F756`) and the human strings at `0x3A505A0..0x3A50720` together specify the full validator. The metadata key `"nvopt"` at `0x4281726` is then written into the loop's MDNode list (HIGH — both producer (`sub_12C35D0`, `sub_225D540`) and consumer (`sub_226C400`) are anchored on the same key).

| Diagnostic key | Trigger |
|---|---|
| `nvopt_pragma_no_loop` | Pragma not directly preceding a loop |
| `nvopt_pragma_no_arg` | Missing argument |
| `nvopt_pragma_bad_format` | Junk trailing tokens after the integer |
| `nvopt_pragma_not_constant` | Argument not an integer constant expression |
| `nvopt_pragma_value_overflow` | Argument exceeds 32-bit range |
| `nvopt_pragma_negative_value` | Argument is negative |

```c
/* Reconstructed loop-attribute emission for `#pragma nvopt N`. */
void emit_nvopt_loop_mdnode(loop_t *loop, int level) {
    /* The optimisation level is packed as a 6-tag enum, not the raw int. */
    static const char *kNvoptTags[] = {
        "nvopt<O0>", "nvopt<O1>", "nvopt<O2>", "nvopt<O3>",
        "nvopt<Ofcmin>", "nvopt<Ofcmid>", "nvopt<Ofcmax>",
    };
    nvopt_level_t lv = clamp_nvopt(level);                /* user int → enum */
    md_t  *tag = md_string(kNvoptTags[lv]);
    md_t  *node = md_tuple2(md_string("nvopt"), tag);
    loop->latch_branch->md_loopid = md_loopid_append(loop->latch_branch->md_loopid, node);
}
```

`sub_226C400` (the pipeline assembler) reads the tag back, and selects one of seven internal `default<...>` pass pipelines per loop. The mapping is fixed:

| Source pragma | MDNode tag | Pipeline assembled |
|---|---|---|
| `#pragma nvopt 0` | `nvopt<O0>` | `default<O0>` |
| `#pragma nvopt 1` | `nvopt<O1>` | `default<O1>` |
| `#pragma nvopt 2` | `nvopt<O2>` | `default<O2>` |
| `#pragma nvopt 3` | `nvopt<O3>` | `default<O3>` |
| `-Ofast-compile=min` global | `nvopt<Ofcmin>` | Minimal `Ofast-compile` |
| `-Ofast-compile=mid` global | `nvopt<Ofcmid>` | Mid-tier `Ofast-compile` |
| `-Ofast-compile=max` global | `nvopt<Ofcmax>` | Aggressive `Ofast-compile` |

The trailing diagnostic at `0x4364520` (`Cannot specify -O#/-Ofast-compile=<...> and --passes=/--foo-pass, use -passes='default<O#>,other-pass' or -passes='default<Ofcmax>,other-pass'`) makes the mutual-exclusion rule explicit: once a `--passes=` is given on the command line, `#pragma nvopt` is silently respected only if it matches the requested default; otherwise the front-end diagnostic at line 737 of `sub_226C400` fires.

> ⚡ **QUIRK — `nvopt<Ofcmax>` is a per-loop kill switch**
> The naïve reading is that `-Ofast-compile` (a CLI flag) and `#pragma nvopt` (a source directive) are independent. They aren't. The same enum drives both: `sub_226C400` reads the *loop-attached* `nvopt<Ofcmax>` tag and switches the pipeline used for just that loop's body to `default<Ofcmax>`, even when the module was compiled with `-O3`. This effectively means a single `#pragma nvopt 6` (the Ofcmax index) inside an otherwise `-O3` translation unit will demote a hot loop to the lightweight pipeline. The error path is symmetrical: setting `--passes=...` globally and then leaving `#pragma nvopt` in source produces no diagnostic — the loop tag is silently overridden by the CLI override.

## 4. `#pragma unroll` — The Loop-Unroll Metadata Bridge

Unlike `nvopt`, `#pragma unroll` is not a cicc invention — it lowers to the standard LLVM loop unrolling metadata family at `llvm.loop.unroll.*`. `sub_19BB5C0` (`computeUnrollCount`) is the consumer; the lowering producer lives in EDG's IR emitter. The full set of metadata strings recovered from the binary (HIGH — `0x4281800..` cluster, all read by `sub_19BB5C0`):

| Source form | MDNode key | Effect in `computeUnrollCount` |
|---|---|---|
| `#pragma unroll N` (N ≥ 2) | `llvm.loop.unroll.count` + `i32 N` | Forces `unroll-by-N`, 2nd-priority branch |
| `#pragma unroll 1` | `llvm.loop.unroll.disable` (no operand) | Hard kill — loop is excluded from `loop-unroll` |
| `#pragma unroll` (no arg) | `llvm.loop.unroll.full` (no operand) | Forces full unrolling, 3rd-priority branch |
| (none, runtime trip) | `llvm.loop.unroll.runtime.disable` | Disables the runtime-unroll fallback only |
| (compiler-injected) | `llvm.loop.unroll.enable` | Whitelist back into unrolling after a `disable` |
| (downstream chaining) | `llvm.loop.unroll.followup_unrolled`, `..._remainder`, `..._all` | Metadata to attach to the unrolled body / remainder |

The priority order observed in `sub_19BB5C0` (from decompiled if/else chain at lines 469–700):

1. **`llvm.loop.unroll.count`** present → unroll exactly by that count, never re-attempt full unroll.
2. **`llvm.loop.unroll.disable`** present → silently mark as done; log `"Unrolling is disabled by source code \"#pragma unroll 1\""`.
3. **`llvm.loop.unroll.full`** present → try full unrolling; falls back to partial if the trip count is unknown or the estimated size exceeds the threshold.
4. **None of the above** → try cost-model-driven unrolling, then partial, then runtime unrolling (5th-priority branch).

> ⚡ **QUIRK — `#pragma unroll 1` is not "unroll once"**
> Programmers occasionally read `#pragma unroll 1` as "unroll once" (i.e. unroll-by-2). It isn't — it is encoded as `llvm.loop.unroll.disable` (no count operand), not `llvm.loop.unroll.count i32 1`. The lowering happens in EDG before LLVM ever sees it, so even a custom pass that reads the metadata cannot tell the two intents apart. The remediation, if you ever need real unroll-by-1 semantics, is `#pragma unroll 2` followed by `#pragma unroll` (full) on the residual.

## 5. `nv_diag_*` — The Diagnostic Steering Pragmas

The `nv_diagnostic`, `nv_diag_suppress`, `nv_diag_warning`, `nv_diag_error`, `nv_diag_remark`, `nv_diag_once`, and `nv_diag_default` keywords (strings cluster at `0x3AC71A7..0x3AC7202`) gate per-diagnostic severity. None of these emit IR metadata — they mutate EDG's diagnostic-severity stack, which is consulted purely at the front-end. Hence they do not survive into the bitcode at all.

The stack is pushed and popped lexically by `#pragma push_macro`/`#pragma pop_macro` (the diagnostic store is implemented as a stack-of-stacks: outer stack pushed by `#pragma diagnostic push`, inner per-diag stack indexed by diagnostic ID). The error message at `0x3AC...` (`no '#pragma diagnostic push' was found to match this 'diagnostic pop'`) confirms a strict balanced-stack discipline; an unbalanced pop logs and is ignored rather than rejected.

| Keyword | Action | Argument |
|---|---|---|
| `nv_diag_suppress`  | Suppress diagnostic; no rendering | Diagnostic number or `error_number` literal |
| `nv_diag_warning`   | Override severity to *warning* | Diagnostic number |
| `nv_diag_error`     | Override severity to *error* | Diagnostic number |
| `nv_diag_remark`    | Override severity to *remark* | Diagnostic number |
| `nv_diag_once`      | Render at most once per TU | Diagnostic number |
| `nv_diag_default`   | Reset to compile-default | Diagnostic number |
| `nv_diagnostic`     | Stack control | `push` / `pop` |

## 6. The `nvvm.annotations` Named-Metadata Tuple Format

After EDG produces NVVM IR, all surviving per-function attributes are encoded into a single named metadata node `nvvm.annotations` (string at `0x3F256xx`, written by every per-attribute path under `sub_A84F90`). Each tuple is one MDNode of the form:

```
!{ <Function*> | <GlobalVariable*>,  <Tag-String>,  <Operand>... }
```

The full tag inventory recovered from string xrefs (HIGH — every key is a string literal anchored on `sub_A84F90` directly):

| Tag string | Operand types | Sets |
|---|---|---|
| `"kernel"` | `i32 1` | Marks the function as a `__global__` kernel entry point |
| `"maxntidx"` / `"maxntidy"` / `"maxntidz"` | `i32` | `__launch_bounds__` thread-count, per axis |
| `"reqntidx"` / `"reqntidy"` / `"reqntidz"` | `i32` | `__launch_bounds__(blockDim, _, _)` strict variant |
| `"minctasm"` | `i32` | Min CTAs per SM (from `__launch_bounds__(_, _, minctasm)`) |
| `"maxnreg"` | `i32` | Per-function register cap (`__maxnreg__`) |
| `"cluster_dim_x"` / `"cluster_dim_y"` / `"cluster_dim_z"` | `i32` | `__cluster_dims__` sm_90+ |
| `"cluster_max_blocks"` | `i32` | `__cluster_dims__` max-blocks-per-cluster operand |
| `"grid_constant"` | (no operand; appears with parameter index list) | Each `__grid_constant__` parameter index, on kernel function |
| `"managed"` | `i32 1` | Globals declared `__managed__` |
| `"texture"` / `"surface"` | (variable-only) | `texture<>` / `surface<>` typed globals |

### Bit-Layout of the MDNode Header

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │ MDNode header (16-byte aligned)                                     │
  ├──────────┬──────────┬──────────┬──────────┬──────────┬──────────────┤
  │ tag-byte │ flags    │ refcount │ pad      │ num-ops  │ small-storage│
  │  (1B)    │  (1B)    │  (2B)    │  (4B)    │  (4B)    │   inline ops │
  └──────────┴──────────┴──────────┴──────────┴──────────┴──────────────┘
                                                              ↓
                                                       if num-ops > 1:
                                                       external array
```

The `flags` byte's bit `1` (`& 0x02`) distinguishes inline-storage from external-storage tuples — the dispatch code at line 132 of `sub_A84F90` reads it as `v10 & 2` to decide whether the operand pointer is at offset `-32` (large MDNode) or computed in-place from `-16 - 8*((v10 >> 2) & 0xF)` (small MDNode). This packing is the same scheme LLVM uses for `MDNode::isInline()`, but the byte position is shifted by NVIDIA's modifications to support the heavily-tagged annotation traffic; a tool generating bitcode for cicc may produce either form as long as the standard LLVM bitcode reader emits the canonical layout.

## 7. The Central Dispatcher `sub_A84F90`

`sub_A84F90` (3 020 bytes, 153 basic blocks) is the single entry point that decomposes the entire `nvvm.annotations` tuple list into per-function `Attribute` strings. It is called once per module immediately after IR parsing and before any optimisation pass runs (callers: `sub_1068BA0`, `sub_1214F10`, `sub_225A270`, `sub_9FEAF0`, `sub_A85B60` — all bitcode loader paths).

```c
/* Reconstructed contract of sub_A84F90, the nvvm.annotations dispatcher. */
void nvvm_annotations_to_function_attrs(Module *M) {
    NamedMDNode *N = M->getNamedMetadata("nvvm.annotations");
    if (!N) return;

    GlobalValue *seen[8];                       /* small inline dedup set     */
    unsigned    seen_n = 0;
    for (unsigned i = 0, e = N->getNumOperands(); i != e; ++i) {
        MDNode *tup = N->getOperand(i);
        /* op[0] = GV*, op[1] = tag string, op[2..] = operands */
        Value *gv_meta = tup->getOperand(0);
        if (!gv_meta) continue;                 /* dropped GV — skip         */
        GlobalValue *gv = mdNodeToGV(gv_meta);
        if (gv->getValueID() != GlobalValueValueID) continue;

        /* Tag dispatch — strings interned in BSS, compared by length+memcmp.  */
        StringRef tag = cast<MDString>(tup->getOperand(1))->getString();
        if      (tag == "maxntidx")            forward_to(sub_CE8B40, gv, tup);
        else if (tag == "maxntidy")            forward_to(sub_CE8B80, gv, tup);
        else if (tag == "maxntidz")            forward_to(sub_CE8BC0, gv, tup);
        else if (tag == "nvvm.maxntid")        forward_to(sub_CE7350, gv, tup); /* fused tuple form */
        else if (tag == "reqntidx" || ... )    forward_to(sub_CE7350, gv, tup);
        else if (tag == "nvvm.cluster_dim")    forward_to(sub_CE8EA0, gv, tup);
        else if (tag == "cluster_dim_x" /*..*/) forward_to(sub_CE8C00 /*..*/, gv, tup);
        else if (tag == "nvvm.maxclusterrank") forward_to(sub_CE9030, gv, tup);
        else if (tag == "nvvm.minctasm")       forward_to(sub_CE90E0, gv, tup);
        else if (tag == "nvvm.maxnreg")        forward_to(sub_CE9180, gv, tup);
        else if (tag == "nvvm.kernel")         set_kernel_calling_conv(gv);
        else if (tag == "grid_constant")       set_grid_constant_param(gv, tup);
        else if (tag == "managed")             set_managed_global(gv);
        else                                   /* unknown — silently drop  */;

        /* Dedup: keep a small open-addressed set, fall through to a growable */
        /* heap allocation when seen_n > 8 (see v94/v95/v100 in decompilation). */
        if (seen_n < 8 && !contains(seen, seen_n, gv)) seen[seen_n++] = gv;
    }
}
```

The per-tag accessors (`sub_CE8D40` family) are the *write* side. They expand a fused MDNode like `("nvvm.maxntid", i32 X, i32 Y, i32 Z)` into three separate function string attributes `"nvvm.maxntid.x"="X"`, `"nvvm.maxntid.y"="Y"`, `"nvvm.maxntid.z"="Z"` and similar for `cluster_dim`. When the annotation is in the *split* form (`maxntidx` + `maxntidy` + `maxntidz` as three tuples), the same accessor instead reads the three child accessors `sub_CE8B40`/`sub_CE8B80`/`sub_CE8BC0` and produces the same attribute strings — both forms converge on a single canonical representation. The cluster path takes the same shape:

```c
/* sub_CE8EA0 — exact decompilation, paraphrased. */
void apply_cluster_dim(Function *F, MDNode *md) {
    if (md_has_inline_xyz(md, "nvvm.cluster_dim")) {       /* fused form */
        F->addFnAttr("nvvm.cluster_dim",
                     int_to_str(md->getOperand(2)) + "," +
                     int_to_str(md->getOperand(3)) + "," +
                     int_to_str(md->getOperand(4)));
    } else {                                                /* split form */
        F->addFnAttr("nvvm.cluster_dim.x", int_to_str(sub_CE8C00(md)));
        F->addFnAttr("nvvm.cluster_dim.y", int_to_str(sub_CE8C40(md)));
        F->addFnAttr("nvvm.cluster_dim.z", int_to_str(sub_CE8C80(md)));
    }
}
```

`sub_CE9030` (the `maxclusterrank` reader) shows the same union pattern, additionally accepting the legacy tag `cluster_max_blocks` as a synonym for `nvvm.maxclusterrank`. The two strings are not aliased anywhere — they are read by separate code paths in three callers (`sub_2C771D0`, `sub_3022E70`, `sub_3074D80`).

> ⚡ **QUIRK — silent acceptance of unknown tags**
> Unknown annotation tags do **not** trigger a diagnostic. `sub_A84F90`'s fall-through path is empty (no `report_fatal_error`, no `errs() << ...`). This is by design: NVIDIA uses `nvvm.annotations` as a forward-compatible extension mechanism. Bitcode produced for a newer SM target can carry tags the current cicc does not understand, and the bitcode reader will accept the module rather than fail. The downside is that a typo (`maxnitdx` instead of `maxntidx`) yields *no warning* and silently disables the launch-bound — exactly the kind of bug that is hard to debug without explicit knowledge of this fall-through.

## 8. Function String Attributes (Post-Dispatch)

After `sub_A84F90` runs, the same information lives in two places simultaneously: the original `nvvm.annotations` MDNode (kept for round-trip purposes and ThinLTO summary emission) and a set of `Function::Attribute` strings consumed by the back-end code generator. Strings that appear directly on functions (HIGH — distinct xrefs from `sub_A84F90` family):

| Attribute string | Operand encoding | Read by |
|---|---|---|
| `"nvvm.kernel"` | none (presence ⇒ kernel) | NVPTX calling-conv selection; PTX `.entry` emission |
| `"nvvm.maxntid"` | comma-separated `x,y,z` integers | NVPTX `.maxntid` directive emitter |
| `"nvvm.maxntid.x"` / `.y` / `.z` | single integer | Legacy split form; same emitter |
| `"nvvm.reqntid"` (and `.x`/`.y`/`.z`) | integers | NVPTX `.reqntid` directive |
| `"nvvm.minctasm"` | integer | NVPTX `.minnctapersm` directive |
| `"nvvm.maxnreg"` | integer | Register allocator hard cap; PTX `.maxnreg` |
| `"nvvm.cluster_dim"` (and `.x`/`.y`/`.z`) | integers | sm_90 cluster launch encoding |
| `"nvvm.maxclusterrank"` | integer | sm_90 `.maxclusterrank` |
| `"nvvm.annotations_transplanted"` | sentinel | Marks a function whose annotations were lifted from inlined callees |

The transplant sentinel `"nvvm.annotations_transplanted"` is the inliner's bookkeeping mechanism: when an `__device__` callee with attached annotations is inlined into a `__global__` caller, the inliner must decide whether to inherit annotations such as `"maxnreg"`. The sentinel marks the caller as having absorbed annotations so the loader does not re-process them on a subsequent re-link. `sub_CE9220` is the producer (HIGH — string at `0x3F25...`, single xref).

## 9. End-to-End Dataflow

```
        SOURCE                       EDG FRONT-END                          NVVM IR                          LLVM PIPELINE
   ────────────────             ──────────────────────                ──────────────────────              ────────────────────
   __launch_bounds__   ──┐
   __maxnreg__         ──┤      attribute byte tags                  ┌─→  nvvm.annotations           ┌─→  Function string attrs
   __cluster_dims__    ──┼─────►    (V..r in 0x56..0x6C)   ─────────┤                                │       (set by sub_A84F90)
   __grid_constant__   ──┤      sub_5C79F0 (decoder)                 │                                │
   __managed__         ──┘                                           │                                │
                                                                     │                                │
   #pragma nv_abi      ──────► validator (sub_~1000B)  ──── abi_overrides bitmask  ──────────────────►  "nvvm.abi.opts"
                                                                                                       
   #pragma nvopt N     ──────► sub_12C35D0 / sub_225D540  ──── nvopt MDNode  ──────────────────► loop-attached !nvopt<O…>
                                                                                                  read by sub_226C400
                                                                                                  pipeline assembler

   #pragma unroll N    ──────► EDG IR emitter  ──── llvm.loop.unroll.count  ──────────────────► sub_19BB5C0 computeUnrollCount
   #pragma unroll 1                            ──── llvm.loop.unroll.disable
   #pragma unroll                              ──── llvm.loop.unroll.full

   #pragma nv_diag_*   ──────► diagnostic-severity stack (front-end-only; never leaves EDG)
```

Three observations from this diagram:

- The **only** pragma whose effect is invisible in the bitcode is `nv_diag_*` — every other steering directive has at least one IR-level shadow.
- `nvopt` is the **only** pragma whose effect is selected at the *pipeline-assembly* phase rather than during a specific pass — it changes which passes run, not how they behave.
- `nv_abi` is the **only** pragma that produces a *bit-packed* attribute (`abi_overrides`), as opposed to a flat tuple. All other attributes survive as integers, strings, or sentinels.

## 10. Validation Checklist (for Bitcode Producers)

A toolchain that emits cicc-compatible NVVM IR without going through the C++ front-end (think: a custom DSL compiler, or a CUDA-Rust back-end) must produce annotations the dispatcher will accept. The minimum compliance set:

1. Emit a `nvvm.annotations` named metadata node, even if empty (some downstream passes assume its existence; HIGH for the kernel-bearing case, MED for empty-module case).
2. For every `__global__` function, emit a `("kernel", i32 1)` tuple. Without it, the function is treated as a `__device__` function and is **not** placed in PTX `.entry` blocks.
3. For `__launch_bounds__`, prefer the split form (`("maxntidx", i32 …)` + `("maxntidy", …)` + `("maxntidz", …)`) — the dispatcher accepts both, but the split form is what the EDG front-end emits, so it has the most test coverage.
4. For `__cluster_dims__`, do not mix split and fused forms in the same module. The dispatcher tolerates the mix, but downstream verifier passes (see [NVVM IR Verifier](../passes/nvvm-verify-deep.md)) check consistency per-function and may diagnose.
5. For loop metadata, attach the `llvm.loop.unroll.*` MDNodes to the *latch* branch's `!llvm.loop` chain — attaching to the header or pre-header silently disables the unroll directive.
6. Never duplicate annotations: the dispatcher deduplicates by global-value pointer in an 8-element open-addressed set, then spills to a growable list. Duplicates do not cause harm, but they bloat ThinLTO summaries.

## Cross-References

- [NVVM IR Generation](./ir-generation.md) — produces the IR these annotations attach to
- [Function, Call & Inline Asm Codegen](./irgen-functions.md) — function-level attribute emission paths
- [Type Translation, Globals & Special Vars](./irgen-types.md) — global annotation paths for `__managed__`, `__constant__`
- [LLVM Optimizer](./optimizer.md) — consumers of `nvopt` per-loop tags
- [NVVM IR Verifier](../passes/nvvm-verify-deep.md) — annotation consistency checking
- [PTX Emission](./emission.md) — final `.entry` / `.maxntid` / `.reqntid` / `.maxclusterrank` directive printing
- [NVVMPassOptions](../config/nvvm-pass-options.md) — global knobs that interact with the annotation system
- [Loop Unrolling](../llvm/loop-unroll.md) — consumer of `llvm.loop.unroll.*` MDNodes
- [CLI Flags](../config/cli-flags.md) — `-Ofast-compile=` interaction with `#pragma nvopt`
