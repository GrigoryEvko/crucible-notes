# Attribute Emission to `.int.c`

Most CUDA attributes collapse into entity-byte mutations at application time and never appear in the generated `.int.c` text. Two CUDA attributes are exceptions: `__launch_bounds__` (kind `'\\' = 0x5C`) and `__nv_pure__` (kind `'n' = 0x6E`). Both are re-emitted as an IL expression node of kind `25` and serialized into `.int.c` by `sub_540560` (`scan_subscript_operator` from `expr.c`). The re-emission path is invoked from `sub_5565E0` (`rescan_expr_with_substitution_internal`, also `expr.c`), which dispatches on the byte at offset `+40` of the underlying expression operand and routes the two attribute kinds through a shared branch that retypes the IL node and calls the subscript-operator scanner.

This page documents that branch: the conditions under which the dispatcher runs, the predicate that selects re-emission, what kind `25` means in this context, why a subscript-operator routine is reused for attribute serialization, and which attributes silently disappear because they never reach this path.

## Key Facts

| Property | Value |
|---|---|
| Re-emission dispatcher | `sub_5565E0` (`rescan_expr_with_substitution_internal`, 1,558 lines, `expr.c`) |
| Re-emission helper | `sub_540560` (`scan_subscript_operator`, 2,715 bytes, `expr.c:1989`) |
| IL node discriminator | byte at `attr_node+40` (the parsed operand kind, not the attribute kind byte `+8`) |
| Retyped IL kind written | `25` (subscript-operator / "function attribute" rescan form) |
| Two attribute kinds re-emitted | `0x5C` (`__launch_bounds__`), `0x6E` (`__nv_pure__`) |
| All others dispatched here | retyped to other IL kinds (52/53/54/56/57/67/etc.) and routed to their own scanners |
| Outer caller (typical) | template instantiation rescan / file-scope expression replay |
| `dword_106BE0C` | "in template rescan" flag — gates the assert at `expr.c:2791` |
| Token-kind global | `word_126DD58` — 25 here means `'['` (left bracket / subscript), which is why `scan_subscript_operator` is the natural shared routine |
| Re-emission output sink | `qword_106B970` ring buffer + `sub_67BEE0` (token writer used by the `.int.c` emitter family) |

## Entry Conditions

`sub_540560` is reached from `sub_5565E0` exactly when all of the following hold simultaneously:

1. The walker is rescanning an expression operand owned by an entity that survived attribute application. Attributes whose handlers freed their IL node (`__host__`, `__device__`, `__global__`, `__shared__`, `__constant__`, `__managed__`, `__maxnreg__`, `__local_maxnreg__`, `__cluster_dims__`, `__block_size__`, `__nv_register_params__`, `__forceinline__`, the `__noinline__` variants, `__inline_hint__`) cannot reach this path.
2. The operand's expression-kind byte (read at `*(_BYTE *)(v12 + 40)` in the dispatch frame, where `v12 = *a1` is the operand pointer) matches one of the two attribute kinds: `0x5C` or `0x6E`.
3. The walker has reached `case 1` of the outer switch on `*(v12 + 24)`, which selects "operator-form" operands. Identifier-form (`case 0`), literal-form, and dependent-name forms route elsewhere.
4. The outer caller has not pre-suppressed the operand via `qword_126ED90 && sub_760FA0(...)`. That gate (lines 180--186 of `sub_5565E0`) drops the operand entirely when the substitution context indicates the surrounding template parameter was substituted away.

The dispatcher does not check the `+8` kind byte of the original attribute node directly. It reads the operand-kind byte at `+40`, which `apply_one_attribute` (`sub_413240`) seeds with the original CUDA kind value for the two attributes that need re-emission. For every other CUDA attribute, the apply handler either does not allocate an operand wrapper at all (entity-bit-only attributes) or seeds `+40` with a different kind that routes to a different `case` in the dispatch.

## The Shared `case 0x5C / case 0x6E` Branch

From `sub_5565E0`, decompiled lines 566--572 (verbatim, modulo variable renaming):

```c
case 0x5C:   // '\\' = __launch_bounds__ original attribute kind
case 0x6E:   // 'n'  = __nv_pure__       original attribute kind
    a2->m128i_i16[4] = 25;     // operand IL kind := 25 (subscript form)
    v29 = 0;
    v30 = 0;
    sub_540560(0, 0, a2, a4);  // scan_subscript_operator(NULL, NULL, operand, sink)
    goto LABEL_52;             // common post-emission cleanup
```

`m128i_i16[4]` is offset `+8` of the IL node interpreted as a 16-bit integer (the operand's kind field, not the attribute node's `+8` byte from the original attribute IL node). The write sets that field to `25` — the same value that the subscript-operator parser uses for `a[i]` expressions. After the write, control transfers to `sub_540560`, which now sees a properly-shaped subscript node and serializes it using its existing emission machinery.

`a2` is the operand IL node, `a4` is the output sink (typically the expression-list head being built for re-emission into the `.int.c` writer's token stream).

## Why Kind 25, Why `scan_subscript_operator`

Kind 25 in `word_126DD58` is `'['` — the left-bracket token. The IL representation for an array subscript `a[i]` allocates an operand with kind 25 and two children: the array expression and the index expression. `scan_subscript_operator` walks that pair, validates them, and emits the subscript form into the writer.

NVIDIA repurposes that exact node shape for re-emitting `__launch_bounds__` and `__nv_pure__` into the `.int.c` output because the attribute's argument list happens to be expressible as a "function name + bracketed argument list" pair under EDG's existing IL grammar. The arguments to `__launch_bounds__(N, M, K)` are integer constants; `scan_subscript_operator` already knows how to walk a node with a "head" expression and an "argument" expression. Reusing it avoids a dedicated `case` in `scan_expr_full` for two attributes that are otherwise interchangeable from the writer's perspective.

The cost is one assert in `sub_540560`:

```c
// sub_540560, line 92 of decompilation
if ( a3->m128i_i16[4] != 25 )
    sub_4F2930(".../expr.c", 1986, "scan_subscript_operator", 0, 0);
```

When the dispatcher writes `25` to the operand kind before calling, this assert is satisfied; any other caller into `sub_540560` from a non-attribute path also passes a real subscript node (kind 25 by construction), so the assert never fires in correct compilations.

## The `kind == 25` Re-Emission Path Inside `sub_540560`

Once entered with a kind-25 operand, `sub_540560` follows a fixed sequence:

1. Debug-trace entry under `dword_126EFC8` (debug level 4 emits `scan_subscript_operator` to the trace stream via `sub_48AE00`).
2. Four asserts at `expr.c:1986`, `:1987`, `:1989`, `:1997` validate the node shape: kind must be 25, `a1` (the result accumulator) must be NULL on entry, the head expression byte at `+24` must be `1` (operator-form), and the operator-kind byte at `+40` must be `92` (which is `0x5C` — `__launch_bounds__`). The fourth assert is the one that proves `__nv_pure__` reaches this branch only after the dispatcher has re-tagged the operand kind: in the `0x6E` path, the dispatcher does not change `+40`, so the assert fires unless the original kind byte was `0x5C` *or* unless the assert is suppressed by `dword_106BE0C` being zero.
3. `sub_573B70` extracts the underlying expression list into local scratch buffers (`v69[40]` and `v70[23]` in the IDA decompilation).
4. Context flags from `qword_106B970+16` and `+19` choose one of four token codes for the emission (58, 60, 529, or fall-through to the default 2,184). These are token IDs in `word_126DD58` — they select the spelling that the writer emits for the attribute marker before the argument list.
5. `sub_511D40` (the 15.5 KB IL token writer) consumes the prepared operand and serializes it to the active output stream. The flag word `0x4000` passed as the fourth argument tells the writer to render the operand as a function-attribute clause rather than as a postfix subscript — the same node shape, different surface syntax.
6. After the writer returns, a check at lines 180--185 of the decompilation:

   ```c
   v33 = word_126DD58;
   if ( dword_106BE0C && word_126DD58 == 25 )
   {
       sub_4F8200(7, 2791, &qword_126DD38);  // emit error 2791
       v33 = word_126DD58;
   }
   ```

   This is the post-write recheck: if the writer left the token stream parked on a stray `'['` (token 25) while a template rescan is active, error 2791 is emitted. In practice this fires only on malformed re-entries, not in normal CUDA compilation.
7. Cleanup via `sub_55C830(0)` releases the temporary initializer component, and the function returns.

## C Pseudocode (Top-Level + Helpers)

### Outer dispatch (subset of `sub_5565E0`)

```c
// sub_5565E0 -- rescan_expr_with_substitution_internal (expr.c, ~1558 lines)
// Walks an expression tree during template / file-scope re-evaluation.
// a2 = IL operand wrapper, a4 = output sink (token list head for .int.c writer)
void rescan_expr_with_substitution_internal(__int64 *a1,
                                            __m128i *a2,
                                            __int64  a3,
                                            _QWORD  *a4,
                                            __m128i *a5,
                                            int      a6)
{
    operand_t *op = (operand_t *)a2->m128i_i64[0];   // = *a1
    uint8_t    head_kind = op->byte_24;              // operator-form selector

    switch (head_kind) {
    case 1:   // operator-form operand
        switch (op->byte_40) {                       // operator kind
        /* ... many cases for arithmetic / relational / comparison ops ... */

        case 0x5C:   // __launch_bounds__   (original CUDA attribute kind)
        case 0x6E:   // __nv_pure__         (original CUDA attribute kind)
            a2->il_kind = 25;                        // retype operand to subscript form
            scan_subscript_operator(/*result*/ 0,
                                    /*ctx*/    0,
                                    /*node*/   a2,
                                    /*sink*/   a4);
            goto common_post_emission_cleanup;

        /* ... other operator kinds ... */
        }
        break;

    case 0: /* identifier-form */    /* ... */ break;
    case 2: /* literal-form    */    /* ... */ break;
    /* ... */
    }

common_post_emission_cleanup:
    /* shared exit: pop context, restore caller's writer state */
}
```

### Subscript scanner with the kind-25 re-emission path (`sub_540560`)

```c
// sub_540560 -- scan_subscript_operator (expr.c:1989)
// Originally serializes a[i]; reused to serialize __launch_bounds__ / __nv_pure__
// when the dispatcher above retypes their operand to kind 25.
void scan_subscript_operator(operand_t *result_accum,
                             il_node_t *index_expr,
                             il_node_t *node,
                             il_node_t *sink)
{
    if (debug_level == 4)
        trace_enter("scan_subscript_operator");

    if (node) {
        assert(node->il_kind     == 25);             // expr.c:1986
        assert(result_accum      == NULL);           // expr.c:1987
        assert(node->head->byte_24 == 1);            // expr.c:1989
        assert(node->head->byte_40 == 92);           // expr.c:1997 (0x5C)

        prepare_operand_lists(node, scratch_lhs, scratch_rhs,
                              0, &src_pos, &line_no, &col_no);

        uint8_t ctx_byte = expr_ctx->byte_16;        // qword_106B970+16
        if ((expr_ctx->byte_19 & 0x40) != 0 && ctx_byte == 1) {
            emit_token(58);                          // token "@", function-attribute prefix
        } else if (ctx_byte == 2) {
            emit_token(529);                         // token __attribute__((...)) prefix
        } else {
            emit_token(60);                          // token __nv_pure / __launch_bounds spelling
        }

        write_attribute_clause(scratch_lhs,          // sub_511D40 with flag 0x4000
                               /*sink*/    sink,
                               /*flags*/   0x4000);
    }

    if (in_template_rescan && current_token == 25)   // dword_106BE0C && word_126DD58 == 25
        emit_error(7, 2791, &current_token);         // unexpected '[' during rescan

    free_init_component(NULL);                       // sub_55C830(0)

    if (debug_level == 4)
        trace_exit();
}
```

The pseudocode collapses many short-lived locals from the IDA decompilation. The token IDs (58, 60, 529, 2184) are real values that `sub_540560` chooses from at runtime; the spelling that ends up in `.int.c` is selected by the EDG token-name table at `off_E6D240`, indexed by the token code passed to `sub_4F81B0`.

## Dropped-Attribute Semantics

Every CUDA attribute that the application phase consumes through entity-bit mutation has *no operand left to rescan*. The application handler (e.g., `sub_4108E0` for `__host__`, `sub_40EB80` for `__device__`) consumes the parsed attribute node, writes the bit pattern into the entity, and returns. The 72-byte IL attribute node (kind `0x48`) is unlinked from the attribute chain and returned to the arena. By the time `sub_5565E0` runs over the entity's expression operands, there is no remaining operand whose `+40` byte still equals `'V'`, `'W'`, `'X'`, `'Z'`, `'['`, `']'`, `'^'`, `'f'`, `'k'`, or `'l'`. Those switch cases are not present in `sub_5565E0` at all, so even a malformed surviving node would fall through to the `default: LABEL_43` arm and be ignored.

The visible consequence: the `.int.c` output does not contain `__host__`, `__device__`, `__global__`, `__shared__`, `__constant__`, `__managed__`, `__maxnreg__`, `__local_maxnreg__`, `__cluster_dims__`, or `__block_size__` tokens anywhere. cicc and the downstream PTX generator reconstruct the necessary information from the entity bytes (for execution / memory space) or from the side-band `launch_config_t` struct (for the launch-config family). Only `__launch_bounds__` and `__nv_pure__` survive into the textual host stream.

The third pseudo-pass-through category from the attribute matrix — `__tile_global__`, `__tile_builtin__`, `__grid_constant__` — is handled by an unrelated emission path. `__grid_constant__` is emitted inline with the kernel-parameter declaration by the routine declarator (`sub_47BFD0`), not through the operand rescan branch. The two `__tile_*` attributes have no handler and no emission path; they exist in the attribute kind table but no cudafe++ consumer reads them.

## QUIRK

> **QUIRK — scan_subscript_operator is reused for two unrelated attributes.**
> The same routine that serializes `a[i]` also serializes `__launch_bounds__(...)` and `__nv_pure__`. The trick is to retype the attribute operand to IL kind 25 (subscript) before dispatching. The four asserts in `sub_540560` (lines 1986, 1987, 1989, 1997 of `expr.c`) catch any caller that fails to retype correctly. Anyone reading the IDA decompilation of `sub_540560` who only sees the asserts firing for `byte_40 == 92` will wrongly conclude the function is `__launch_bounds__`-specific; it is also `__nv_pure__`-specific via the dispatcher's two-case branch.

> **QUIRK — kind 25 is overloaded across three layers.**
> `25` is the token code for `'['` in the lexer (`word_126DD58 == 25` after a `[`), the IL operand kind for subscript expressions (`node->il_kind == 25`), and the IL string-walking kind for `string_text` (per [IL Tree Walking](../il/walking.md)). All three are the same numeric value, none of them have name collisions in their respective contexts, and EDG documentation predating CUDA appears to have chosen the value once for the token table and propagated it. The CUDA attribute re-emission path piggybacks on the IL-operand-kind sense, not the token sense.

> **QUIRK — `__nv_pure__` looks chainless but reaches the writer.**
> The apply handler for `__nv_pure__` (case `'n'` in `sub_413240`) sets no entity bits and frees no IL node, leaving the attribute on the chain. Casual inspection suggests `__nv_pure__` is a no-op for cudafe++, but in fact the chain entry is exactly what enables the kind-25 re-emission: when the expression rescanner walks the entity's operands, the surviving operand with `+40 == 0x6E` hits the shared `0x5C/0x6E` branch and gets serialized. Removing the chain entry would silently drop the attribute from `.int.c` and cicc would never see it. The chain entry is the carrier; the apply handler's apparent inertia is by design.

## Cross-References

- [Attribute System Overview](../attributes/overview.md) — per-attribute IL emission matrix and the three emission categories (collapse, side-band struct, preserved chain)
- [Launch Configuration Attributes](../attributes/launch-config.md) — struct + re-emit detail for `__launch_bounds__`, including the `node->kind_field = 25` write
- [Minor CUDA Attributes](../attributes/minor-attributes.md) — `__nv_pure__` chain-preservation rationale and the LLVM attributes cicc derives from it
- [.int.c File Format](./int-c-format.md) — containing file structure that receives the re-emitted tokens
- [Expression Parser](../edg/expression-parser.md) — `rescan_expr_with_substitution_internal` and `scan_subscript_operator` in their original (non-attribute) roles
- [Token Kinds](../reference/token-kinds.md) — token code 25 (`'['`) and the table at `off_E6D240`
- [IL Tree Walking](../il/walking.md) — string kind 25 (`string_text`) in the walker, unrelated to the operand-kind 25 used here

## Function Map

| Address | Identity | Source | Confidence |
|---|---|---|---|
| `sub_5565E0` | `rescan_expr_with_substitution_internal` | `expr.c` | HIGH |
| `sub_540560` | `scan_subscript_operator` | `expr.c:1989` | HIGH |
| `sub_573B70` | `prepare_operand_lists` (rescan-default helper) | `expr.c` | HIGH |
| `sub_511D40` | IL token writer (15.5 KB) | `expr.c` | MEDIUM |
| `sub_4F8200` | `emit_diag_basic` (2-arg diagnostic) | `error.c` | HIGH |
| `sub_4F2930` | `assert_fail` (file/line/function reporter) | `error.c` | VERY HIGH |
| `sub_4F81B0` | `set_current_token_code` | `lexer.c` | MEDIUM |
| `sub_55C830` | `free_init_component` | `exprutil.c` | HIGH |
| `sub_67BEE0` | `required_token` (writer-side token enforcement) | `tokutil.c` | HIGH |
| `qword_106B970` | expression context stack pointer | `exprutil.c` | HIGH |
| `word_126DD58` | current token code (16-bit) | `lexer.c` | HIGH |
| `dword_106BE0C` | template-rescan-active flag | `template.c` | MEDIUM |
| `qword_126DD38` | source-position for diagnostics | `lexer.c` | HIGH |
