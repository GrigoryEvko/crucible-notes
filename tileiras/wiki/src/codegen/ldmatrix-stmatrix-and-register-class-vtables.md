# ldmatrix/stmatrix Emission + Register Class Vtables

## Abstract

Two table-driven parts of NVPTX code generation regularly coexist in the
same matrix instruction sequence. The first is the `ldmatrix`/`stmatrix`
selector, which maps MLIR/NVVM matrix-copy properties to LLVM intrinsic
IDs and reconstructed PTX mnemonics. The second is the NVPTX
register-class model that instruction selection and the asm printer
consult when emitting register declarations such as `.reg .b32 %r<N>;`
and the per-instruction operand prefixes (`%r`, `%rd`, `%rs`, `%rq`,
`%p`, `%f`).

Both subsystems are deliberately static. Matrix-copy lowering is a small
dispatcher over shape, matrix count, layout, transpose, and packed
element-width fields, with no runtime feedback. Register-class emission
is a fixed mapping from LLVM register classes to PTX declaration width
and printer prefix, with one subtlety: the `%f` (32-bit float view) class
shares physical register IDs with `%r`, and the `%fd` (64-bit float view)
prefix layers on top of `%rd` storage at print time only.

## Matrix-Copy Templates

The warp-wide matrix-copy path has three layers:

```text
cute_nvgpu.arch.copy.{ldsm,stsm}
    -> nvgpu.ldmatrix / nvgpu.stmatrix
    -> nvvm.ldmatrix / nvvm.stmatrix / nvvm.movmatrix
    -> llvm.call_intrinsic
```

The NVVM-to-LLVM tier receives a properties blob, validates legal
combinations, then selects an intrinsic ID. `ldmatrix` properties:
`{num, shape, sz, trans, layout}`. `stmatrix` properties: `{num, shape, trans}`.

| Family       | Shape (enum) | Num    | Layout / trans                       | sz-code (width)            | Properties `{num,shape,sz,trans,layout}` | Intrinsic id | Reconstructed PTX mnemonic                                       |
| ------------ | ------------ | :----: | :----------------------------------- | :------------------------- | :--------------------------------------- | :----------: | :--------------------------------------------------------------- |
| ldmatrix     | `m8n8` (0)   |   1    | no-trans                             | `b16`                      | `{1, 0, 0, 0, 0}`                        |    9165      | `ldmatrix.sync.aligned.m8n8.x1.b16`                              |
| ldmatrix     | `m8n8` (0)   |   1    | `.trans`                             | `b16`                      | `{1, 0, 0, 1, 0}`                        |    9166      | `ldmatrix.sync.aligned.m8n8.x1.trans.b16`                        |
| ldmatrix     | `m8n8` (0)   |   2    | no-trans                             | `b16`                      | `{2, 0, 0, 0, 0}`                        |    9167      | `ldmatrix.sync.aligned.m8n8.x2.b16`                              |
| ldmatrix     | `m8n8` (0)   |   2    | `.trans`                             | `b16`                      | `{2, 0, 0, 1, 0}`                        |    9168      | `ldmatrix.sync.aligned.m8n8.x2.trans.b16`                        |
| ldmatrix     | `m8n8` (0)   |   4    | no-trans                             | `b16`                      | `{4, 0, 0, 0, 0}`                        |    9169      | `ldmatrix.sync.aligned.m8n8.x4.b16`                              |
| ldmatrix     | `m8n8` (0)   |   4    | `.trans`                             | `b16`                      | `{4, 0, 0, 1, 0}`                        |    9170      | `ldmatrix.sync.aligned.m8n8.x4.trans.b16`                        |
| ldmatrix     | `m8n16` (1)  |   1    | row (mandatory; trans illegal)        | `b8`                       | `{1, 1, 0, 0, 0}`                        |    9160      | `ldmatrix.sync.aligned.m8n16.x1.b8`                              |
| ldmatrix     | `m8n16` (1)  |   1    | row                                   | `b8x16.b6x16_p32`          | `{1, 1, 1, 0, 0}`                        |    9159      | `ldmatrix.sync.aligned.m8n16.x1.b8x16.b6x16_p32`                 |
| ldmatrix     | `m8n16` (1)  |   2    | row                                   | `b8`                       | `{2, 1, 0, 0, 0}`                        |    9162      | `ldmatrix.sync.aligned.m8n16.x2.b8`                              |
| ldmatrix     | `m8n16` (1)  |   2    | row                                   | `b8x16.b6x16_p32`          | `{2, 1, 1, 0, 0}`                        |    9161      | `ldmatrix.sync.aligned.m8n16.x2.b8x16.b6x16_p32`                 |
| ldmatrix     | `m8n16` (1)  |   4    | row                                   | `b8`                       | `{4, 1, 0, 0, 0}`                        |    9164      | `ldmatrix.sync.aligned.m8n16.x4.b8`                              |
| ldmatrix     | `m8n16` (1)  |   4    | row                                   | `b8x16.b6x16_p32`          | `{4, 1, 1, 0, 0}`                        |    9163      | `ldmatrix.sync.aligned.m8n16.x4.b8x16.b6x16_p32`                 |
| ldmatrix     | `m16n16` (3) |   2    | col (mandatory)                       | `b8`                       | `{2, 3, 0, 0, 1}`                        |    9155      | `ldmatrix.sync.aligned.m16n16.x2.b8`                             |
| ldmatrix     | `m16n16` (3) |   2    | col                                   | `b8x16.b6x16_p32`          | `{2, 3, 1, 0, 1}`                        |    9154      | `ldmatrix.sync.aligned.m16n16.x2.b8x16.b6x16_p32`                |
| ldmatrix     | `m16n16` (3) |   2    | col                                   | `b8x16.b6x16_p64`          | `{2, 3, 2, 0, 1}`                        |    9153      | `ldmatrix.sync.aligned.m16n16.x2.b8x16.b6x16_p64`                |
| ldmatrix     | `m16n16` (3) |   4    | col                                   | `b8`                       | `{4, 3, 0, 0, 1}`                        |    9158      | `ldmatrix.sync.aligned.m16n16.x4.b8`                             |
| ldmatrix     | `m16n16` (3) |   4    | col                                   | `b8x16.b6x16_p32`          | `{4, 3, 1, 0, 1}`                        |    9157      | `ldmatrix.sync.aligned.m16n16.x4.b8x16.b6x16_p32`                |
| ldmatrix     | `m16n16` (3) |   4    | col                                   | `b8x16.b6x16_p64`          | `{4, 3, 2, 0, 1}`                        |    9156      | `ldmatrix.sync.aligned.m16n16.x4.b8x16.b6x16_p64`                |
| stmatrix     | `m8n8` (0)   |   1    | no-trans                              | `b16`                      | `{1, 0, 0, –, –}`                        |    9862      | `stmatrix.sync.aligned.m8n8.x1.b16`                              |
| stmatrix     | `m8n8` (0)   |   1    | `.trans`                              | `b16`                      | `{1, 0, 1, –, –}`                        |    9861      | `stmatrix.sync.aligned.m8n8.x1.trans.b16`                        |
| stmatrix     | `m8n8` (0)   |   2    | no-trans                              | `b16`                      | `{2, 0, 0, –, –}`                        |    9864      | `stmatrix.sync.aligned.m8n8.x2.b16`                              |
| stmatrix     | `m8n8` (0)   |   2    | `.trans`                              | `b16`                      | `{2, 0, 1, –, –}`                        |    9863      | `stmatrix.sync.aligned.m8n8.x2.trans.b16`                        |
| stmatrix     | `m8n8` (0)   |   4    | no-trans                              | `b16`                      | `{4, 0, 0, –, –}`                        |    9866      | `stmatrix.sync.aligned.m8n8.x4.b16`                              |
| stmatrix     | `m8n8` (0)   |   4    | `.trans`                              | `b16`                      | `{4, 0, 1, –, –}`                        |    9865      | `stmatrix.sync.aligned.m8n8.x4.trans.b16`                        |
| stmatrix     | `m8n16` (2)  |   1    | `.trans` (mandatory in observed arm)  | `b8`                       | `{1, 2, 1, –, –}`                        |    9858      | `stmatrix.sync.aligned.m8n16.x1.trans.b8`                        |
| stmatrix     | `m8n16` (2)  |   2    | `.trans`                              | `b8`                       | `{2, 2, 1, –, –}`                        |    9859      | `stmatrix.sync.aligned.m8n16.x2.trans.b8`                        |
| stmatrix     | `m8n16` (2)  |   4    | `.trans`                              | `b8`                       | `{4, 2, 1, –, –}`                        |    9860      | `stmatrix.sync.aligned.m8n16.x4.trans.b8`                        |
| stmatrix alt import path | `m8n8` (0) | – | attr 0 / attr 1 | – | – | 10379 / 10380 | `stmatrix.sync.aligned.m8n8.{x?}.{trans?}.b16` sibling |
| stmatrix alt import path | `m16n16` (3) | – | attr 0 / attr 1 | – | – | 10381 / 10382 | `stmatrix.sync.aligned.m16n16.{...}` sibling |
| ldmatrix sibling | – | – | – | – | – | 8366 | WGMMA / m8n16.x1 single-id sibling |
| movmatrix     | `m8n8`       |   1    | `.trans` (mandatory)                  | `b16`                      | (no arm; folded)                         |   (none)     | `movmatrix.sync.aligned.m8n8.trans.b16`                          |

Shape enum value `2` is reserved for `ldmatrix`. `m16n16` rejects `num=1`;
`m8n16` rejects `.trans` and reports
`Transposed layout is not supported for m8n16 shape for nvvm.ldmatrix`.
The `m8n8` arm is b16-only. `movmatrix` carries no separate selected
intrinsic in this path because its layout swap folds into
`shufflevector` and `bitcast` operations before instruction selection.

The selector is a thin validator over the properties blob, followed by an ID-table lookup:

```c
unsigned select_matrix_copy(MatrixCopyNode *node, Subtarget *st) {
    MatrixCopyProps p = decode_properties(node->properties);

    /* Family-specific legality checks happen before any ID lookup so the
       caller never has to recover from a bogus intrinsic id. */
    if (node->family == LDMATRIX) {
        if (p.shape == LDSM_M8N16 && p.trans) {
            fatal("Transposed layout is not supported for m8n16 shape for nvvm.ldmatrix");
        }
        if (p.shape == LDSM_M16N16 && p.num == 1) {
            fatal("m16n16 ldmatrix requires num=2 or num=4");
        }
        return select_ldmatrix_id(p);
    }

    require(node->family == STMATRIX, "unknown matrix-copy family");
    return select_stmatrix_id(p);
}
```

The ID-selection bodies are compact enough to reimplement directly:

```c
int select_ldmatrix_id(LdMatrixProps p) {
    if (p.shape == LDSM_M16N16) {
        return (p.num == 2 ? 9153 : 9156) + (2 - p.sz);
    }

    if (p.shape == LDSM_M8N16) {
        static const int ids[3][2] = {
            {9160, 9159},
            {9162, 9161},
            {9164, 9163},
        };
        return ids[num_to_index(p.num)][p.sz];
    }

    return 9165 + 2 * (p.num / 2) + (p.trans ? 1 : 0);
}

int select_stmatrix_id(StMatrixProps p) {
    if (p.shape == STSM_M8N16) {
        return 9857 + p.num;
    }

    return 9862 - (p.trans ? 0 : 1) + 2 * (p.num / 2);
}
```

## NVPTX RegisterClass vtables

The NVPTX register classes used by the selector and asm printer are:

| Class | ClassID | Width | Declaration type | Printer prefix | Notes |
| --- | ---:| ---:| --- | --- | --- |
| `%p` | 0 | 1 bit | `.pred` | `%p` | predicate registers |
| `%rs` | 1 | 16 bit | `.b16` | `%rs` | 16-bit integer registers |
| `Special` | 2 | 32 bit | internal | none | PTX special registers such as `%tid` and `%laneid` |
| `%r` | 3 | 32 bit | `.b32` | `%r` | ordinary 32-bit integer registers |
| `%f` | 4 | 32 bit | printer-only | `%f` | float view over selected 32-bit register IDs |
| `%rd` | 5 | 64 bit | `.b64` | `%rd` | 64-bit integer and f64 physical storage |
| `%rq` | 6 | 128 bit | `.b128` | `%rq` | 128-bit registers |

The `%f` class is the easiest one to miss. The asm printer never declares
`.reg .b32 %f<N>;` because float registers print as a view of the same
underlying 32-bit register IDs `%r` uses. The class still exists so that
`TargetRegisterInfo::getRegClass(MVT::f32)` succeeds during DAG
legalization and copy lowering. No separate `%fd` class exists; f64
values physically live in `%rd` and print with the float-double prefix
only at instruction-print time.

Subclassing closes through `%f`: both `%r` and `Special` include `%f` in
their subclass masks, and `%f` lists `%r` and `Special` as superclasses.
Preserve that relationship in a reimplementation — it affects COPY
lowering and register-class queries even though `%f` is mostly invisible
in declarations.

The declaration printer is a pair of maps:

```c
StringRef reg_class_type(const RegisterClass *rc) {
    switch (rc->id) {
    case RC_RQ:
        return ".b128";
    case RC_RD:
        return ".b64";
    case RC_R:
        return ".b32";
    case RC_RS:
        return ".b16";
    case RC_P:
        return ".pred";
    default:
        return "INTERNAL";
    }
}

StringRef reg_class_prefix(const RegisterClass *rc) {
    switch (rc->id) {
    case RC_RQ:
        return "%rq";
    case RC_RD:
        return "%rd";
    case RC_R:
        return "%r";
    case RC_RS:
        return "%rs";
    case RC_P:
        return "%p";
    default:
        return "INTERNAL";
    }
}
```

The declaration printer emits one `.reg` directive per non-empty class.
`%f` is skipped because its registers share IDs with `%r` and have
already been declared under that prefix:

```c
void print_reg_decls(Printer *p, const RegisterAllocation *ra) {
    for (RegisterClass *rc : ra->classes) {
        if (rc->id == RC_F || rc->id == RC_SPECIAL) {
            continue;                /* %f shares storage with %r; Special is intrinsic */
        }
        unsigned count = ra->count_for(rc);
        if (count == 0) {
            continue;
        }
        fprintf(p, "\t.reg %s %s<%u>;\n",
                reg_class_type(rc), reg_class_prefix(rc), count);
    }
}
```

Inside an instruction operand, the printer chooses `%f` over `%r` for
32-bit float MVTs and `%fd` over `%rd` for 64-bit float MVTs, printing the
same numeric register ID either way.

## Reimplementation Notes

- Validate matrix-copy shape, count, layout, transpose, and element-width fields before selecting
  an intrinsic ID.
- Preserve the `m8n16` transpose rejection and the `m16n16` `num=1` rejection.
- Treat `movmatrix` as foldable unless reproducing a backend that still consumes a dedicated
  intrinsic.
- Keep `%f` as a real register class for legalization even though declarations are printed through
  `%r` storage.
- Store f64 values in `%rd` and print `%fd` only at the instruction-printer layer.
