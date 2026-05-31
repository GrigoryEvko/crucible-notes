# Sanitizer & Stack-Protector Integration

NVIDIA's Compute Sanitizer (formerly `cuda-memcheck`) needs to inject runtime
checks at every memory access in a kernel. Rather than emit a fully resolved
call into a sanitizer runtime at compile time, the toolchain generates kernels
that contain `call __cuda_sanitizer_memcheck_<class>` instructions referencing
seven well-known weak symbols. **nvlink is the component that turns those weak
calls into a linkable program**: it synthesizes the missing `.weak .func` PTX
declarations during embedded-ptxas prelude generation, propagates the
`--sanitize` / `--device-stack-protector` flags through to the embedded
assembler, reads back the resulting `EIATTR_SANITIZE` / `EIATTR_STACK_CANARY_
TRAP_OFFSETS` attributes from each cubin, and rejects mixed-toolkit sanitized
inputs with a dedicated cross-version diagnostic. This page documents the full
mechanism end-to-end, including the 1080-case PTX prelude dispatcher
(`sub_15B86A0`), the 608-entry intrinsic hash table (`sub_158A600`), the
sanitizer prefix check in section dispatch (`sub_1CAE070`), the option
forwarder (`sub_429BA0`), and the post-link `EIATTR` consumer paths.

| | |
|---|---|
| **Intrinsic registry** | `sub_158A600` at `0x158A600` (11,050 B) — registers 608 names |
| **PTX prelude emitter** | `sub_15B86A0` at `0x15B86A0` (34,362 B, 1080-case switch) |
| **Symbol-prefix gate** | `sub_1CAE070` at `0x1CAE070` (958 B) — `__cuda_sanitizer` memcmp |
| **CLI option forwarder** | `sub_429BA0` at `0x429BA0` (1,505 B) — builds ptxas flag string |
| **CLI registrar** | `sub_427AE0` at `0x427AE0` lines registering `device-stack-protector*` |
| **Sanitizer hook IDs** | 0x12 – 0x18 (7 contiguous IDs in the intrinsic table) |
| **PTX decl strings** | `0x1F8B5D0`, `0x1F8B6A8`, `0x1F8B838`, `0x1F8BA00`, `0x1F8BB88`, `0x1F8BD88`, `0x1F8BE48` |
| **Switch cases (PTX)** | 1073 — 1079 |
| **EIATTR_SANITIZE** | `0x5C` (Indexed format) |
| **EIATTR_STACK_CANARY_TRAP_OFFSETS** | `0x57` (Free format) |
| **Sanitize-mismatch error** | string `0x1D393D8` |
| **`--sanitize` arg domain** | `memcheck`, `threadsteer` (string `"memcheck,threadsteer"` at `0x1EEB8..`) |
| **Stack-protector globals** | `byte_2A5F1FF` (seen), `byte_2A5F1FE` (value), `byte_2A5F1FC` (threshold seen), `dword_2A5F1F8` (threshold value) |

## Why nvlink Cares About Sanitizers

A device kernel that has been compiled with `-Xcompiler=-fsanitize=...` or with
the modern `--sanitize=memcheck` flag contains no inline memcheck code. The
front end (`cudafe++` / `cicc`) instead emits a `call.uni` to one of seven
canonical PTX symbols:

```ptx
call.uni (retval), __cuda_sanitizer_memcheck_global,
  (addr_lo, addr_hi, size, alloc_id, access_kind, pc_lo);
```

Those seven symbols are **never defined by the cubin itself**. They are
satisfied either by an external library that the sanitizer tool injects at
launch time, or — more importantly for static analysis — by `.weak .func`
declarations that nvlink/ptxas synthesise during the ptxas prelude pass. The
weak declarations exist purely so that ptxas's PTX parser and verifier accept
the kernel without "undefined symbol" errors; the actual function body is
resolved at runtime by the sanitizer.

Stack protectors follow a different pattern: ptxas inserts `bar.cluster
trap.canary` instructions at function exit when `--device-stack-protector=true`
is in effect, records their offsets into a fresh `.nv.info.<func>` attribute
named `EIATTR_STACK_CANARY_TRAP_OFFSETS` (id `0x57`, free format), and nvlink
preserves those offsets across the link without altering them.

The two features share infrastructure in nvlink for one reason: both are
runtime safety features whose effect on the final cubin is visible only as
small `.nv.info` attributes and a handful of weak `__cuda_*` calls — neither
generates new section data of its own. Together they form the nvlink
"instrumentation surface".

## The Seven Sanitizer Hooks

`sub_158A600` is a 608-entry intrinsic-name registry. It allocates a hash map
(call to `sub_44F410` followed by `memcpy(v5, &unk_1F8E0C0, 0x2600)`), then
registers names with monotonically increasing IDs via `sub_448E70(map, "name",
(void*)id)`. The seven sanitizer entries are contiguous at IDs `0x12` — `0x18`:

| ID | Symbol Name | PTX Return | Param Count | PTX Decl Address |
|--:|---|---|--:|---|
| 0x12 | `__cuda_sanitizer_memcheck_free` | `void` | 2 (`b64`, `b64`) | `0x1F8BE48` |
| 0x13 | `__cuda_sanitizer_memcheck_generic` | `void` | 8 (4×`b64`, 4×`b32`, …) | `0x1F8BB88` |
| 0x14 | `__cuda_sanitizer_memcheck_global` | `void` | 7 (4×`b64`, 3×`b32`) | `0x1F8B6A8` |
| 0x15 | `__cuda_sanitizer_memcheck_local` | `void` | 6 (2×`b64`, 4×`b32`) | `0x1F8BA00` |
| 0x16 | `__cuda_sanitizer_memcheck_malloc` | `b64` (return val) | 2 (`b64`, `b64`) | `0x1F8BD88` |
| 0x17 | `__cuda_sanitizer_memcheck_readmetadata` | `b64` | 2 (`b64`, `b64`) | `0x1F8B5D0` |
| 0x18 | `__cuda_sanitizer_memcheck_shared` | `void` | 8 (2×`b64`, 6×`b32`) | `0x1F8B838` |

The name pool itself lives at `0x1F57E90`—`0x1F57F70` as a contiguous run of
NUL-terminated strings 32–40 bytes each; the order in the pool matches the ID
order, which means an attacker who can replace a single name in the pool
moves its semantics one ID slot — see the QUIRK below.

> ⚡ **QUIRK — ID range overlap with reduxsync intrinsics**
>
> IDs `0x01`–`0x11` are `__cuda_reduxsync_*` warp-reduction helpers. IDs
> `0x12`–`0x18` are the sanitizer hooks. IDs `0x19`–`0x29` are scalar video
> emulation. The sanitizer hooks are not in a dedicated namespace — they share
> the same flat numeric space as every other `__cuda_*` intrinsic. If a future
> NVIDIA toolkit inserts a new reduxsync variant at ID `0x12`, every existing
> sanitizer ID shifts by one. The dispatch switch at `sub_15B86A0` keys on
> these IDs offset by `+1057` (cases 1073–1079 = IDs 16–22 = 0x12–0x18 after
> a +0x10 register-rebase the decompilation hides), so the offset is hard-coded
> at two layers and any reshuffle desynchronises them silently.

### PTX Declaration Lengths

The string-pool lengths reveal that the four most parameter-heavy hooks
(`global`, `shared`, `local`, `generic`) are stored truncated in the `.rodata`
strings extracted by the binary scanner — actual on-disk lengths are 377, 434,
370, and 500 bytes respectively. Reading the binary directly shows that each
truncated string continues with the remaining `.param` declarations and the
closing `);` token. The dispatch table allocates exactly the right buffer size
via `sub_14932E0((pthread_mutexattr_t *)<size>, …)`; the first argument to
`sub_14932E0` is the buffer length used by the subsequent `strcpy`. The
observed lengths from the switch table are:

| Hook | `sub_14932E0` size | Matches string length |
|---|---|---|
| `readmetadata` | `0xCF` (207) | `0x1F8B5D0` declaration |
| `malloc` | `0xBD` (189) | `0x1F8BD88` declaration |
| `free` | `0x9F` (159) | `0x1F8BE48` declaration |
| `global` | `0x189` (393) | `0x1F8B6A8` declaration (≥377 B) |
| `shared` | `0x1B2` (434) | `0x1F8B838` declaration |
| `local` | `0x172` (370) | `0x1F8BA00` declaration |
| `generic` | `0x1F4` (500) | `0x1F8BB88` declaration |

The size argument to `sub_14932E0` is the exact byte count copied by the
subsequent `strcpy`, not the buffer capacity — there is no NUL slack and the
buffer is exactly one byte larger than the visible string length to accommodate
the terminating NUL that `strcpy` writes. The PTX parser in the embedded ptxas
treats these as `.weak .func` external declarations and does not generate code
for them.

## The Prelude Emission Pipeline

When ptxas (embedded inside nvlink) compiles a kernel module that references a
sanitizer hook, the front-end walks every call instruction and asks the
intrinsic registry "do you have a definition for this name?". The registry
returns the integer ID via `sub_448E70`'s reverse lookup. The PTX prelude
builder then emits the `.weak .func` declaration that satisfies the call.

```c
// Reconstructed flow (sub_15B86A0 is the 1080-case dispatcher).
void emit_prelude_decl(PTX_Emitter *e, uint32_t intrinsic_id) {
    char *buf;
    switch (intrinsic_id) {
    /* ... 1072 cases for non-sanitizer intrinsics ... */
    case 1073:  /* __cuda_sanitizer_memcheck_readmetadata, ID 0x17 */
        buf = sub_14932E0(0xCF, e);
        strcpy(buf,
            ".weak .func (.param .b64 func_retval0) "
            "__cuda_sanitizer_memcheck_readmetadata ("
            "    .param .b64 __cuda_sanitizer_memcheck_readmetadata_param_0,"
            "    .param .b64 __cuda_sanitizer_memcheck_readmetadata_param_1);");
        break;
    case 1074: /* global */     buf = sub_14932E0(0x189, e); strcpy(buf, ".weak .func () __cuda_sanitizer_memcheck_global (…);"); break;
    case 1075: /* shared */     buf = sub_14932E0(0x1B2, e); strcpy(buf, ".weak .func () __cuda_sanitizer_memcheck_shared (…);"); break;
    case 1076: /* local */      buf = sub_14932E0(0x172, e); strcpy(buf, ".weak .func () __cuda_sanitizer_memcheck_local (…);"); break;
    case 1077: /* generic */    buf = sub_14932E0(0x1F4, e); strcpy(buf, ".weak .func () __cuda_sanitizer_memcheck_generic (…);"); break;
    case 1078: /* malloc */     buf = sub_14932E0(0xBD,  e); strcpy(buf, ".weak .func (.param .b64 func_retval0) __cuda_sanitizer_memcheck_malloc (…);"); break;
    case 1079: /* free */       buf = sub_14932E0(0x9F,  e); strcpy(buf, ".weak .func () __cuda_sanitizer_memcheck_free (…);"); break;
    default:
        return /* empty string at "\n\t" + 2 */;
    }
}
```

### Case-to-ID Map

The decompilation shows the switch cases use literal integers 1073–1079. The
registry IDs are 0x12–0x18 (= 18–24). The offset is exactly 1055 between the
two:

```text
case_number = intrinsic_id + 1055
```

This constant (`1055 = 0x41F`) does not appear as a clean shift or mask in the
decompiled code — it is the running counter at the point where the sanitizer
block begins, set by 1072 prior cases for unrelated `__cuda_*` intrinsics.
Reimplementations must preserve the relative ordering of all 1080 cases or
substitute their own ID/case mapping.

### Memory Layout of an Emitted Declaration

```c
struct ptx_decl_record {
    /*  0 */ char *text;          // pointer returned by sub_14932E0
    /*  8 */ uint32_t length;     // strcpy'd byte count, excluding NUL
    /* 16 */ struct ptx_decl_record *next;  // forward-only linked list
    /* 24 */ uint16_t intrinsic_id;
    /* 26 */ uint8_t  is_weak;    // always 1 for sanitizer hooks
    /* 27 */ uint8_t  flags;
};
```

The PTX emitter writes records in source order; on dump it concatenates them
into the kernel's `.weak` declarations block immediately after the `.version`
and `.target` directives.

## Symbol-Prefix Gate (sub_1CAE070)

After ELF merging, when nvlink iterates linked symbols to apply DCE
(`dead-code-elimination.md`), it must distinguish "real" weak functions
(template instantiations, inline `__device__` functions) from sanitizer hooks
(which are weak placeholders that should never be eliminated). `sub_1CAE070`
performs a 16-byte memcmp against the literal `"__cuda_sanitizer"`:

```c
// Reconstructed: in the symbol-iteration loop at sub_1CAE070.
do {
    sym_record = *(_QWORD *)(sym_table[46] + offset);
    name_ptr   = strtab_get(strtab, sym_record->name_idx);
    if (!memcmp(name_ptr, "__cuda_sanitizer", 0x10u)) {
        // Sanitizer hook — keep regardless of DCE liveness.
        mark_live(sym_record);
        continue;
    }
    /* normal weak-symbol handling */
    offset += 8;
} while (offset < end);
```

The check is on the **prefix only** (16 bytes = length of `"__cuda_sanitizer"`
exactly, no NUL). Any symbol whose first 16 bytes are `__cuda_sanitizer` is
preserved. The seven canonical names all match this prefix, but so would any
hypothetical future name like `__cuda_sanitizer_racecheck_*`.

> ⚡ **QUIRK — 16-byte prefix preserves more than the seven hooks**
>
> The DCE gate trusts the prefix, not an enumerated list. A user-defined
> device function named `__cuda_sanitizer_my_custom_check` would also be
> preserved through DCE even though nvlink will never synthesise its
> declaration. The intrinsic registry covers exactly seven names; the DCE
> preservation set is unbounded. This was almost certainly intentional — it
> lets the sanitizer runtime define new hooks without nvlink updates — but it
> also means a maliciously named device function escapes dead-code stripping.

## CLI Flag Surface

nvlink exposes three sanitizer-adjacent CLI flags through the standard option
registrar (`sub_427AE0`). The registration calls live in the same prologue as
the rest of the CLI surface:

```c
// sub_427AE0 — registration order
sub_42F130(ctx, "device-stack-protector", "device-stack-protector",
           /* type=bool */ 1, /* multiplicity=1 */ 1, /* flags=hidden */ 0,
           a3, 0, 0, 0, 0, 0, "Enable stack protectors");

sub_42F130(ctx, "device-stack-protector-frame-size-threshold",
           "device-stack-protector-frame-size-threshold",
           /* type=int */ 4, /* multiplicity=1 */ 1, /* flags */ 4,
           a3, 0, 0, 0, 0, "<threshold>",
           "Set stack protector frame size threshold");

// `sanitize` is registered inside the embedded ptxas option block,
// reachable from nvlink only via -Xptxas forwarding.
sub_42F130(ctx, "sanitize", "sanitize",
           /* type=string */ 2, /* multiplicity=1 */ 1, /* flags */ 0,
           a3, "memcheck,threadsteer", 0, 0, 0, "<string>",
           "Generate instrumented code with specified sanitizer tool");
```

### Flag-to-Global Wiring

After registration, `sub_42E390` binds each name to a global storage slot, and
`sub_42E580` exposes a "was this flag seen?" predicate stored in a separate
byte:

| CLI flag | Value global | Seen predicate | Type | Default |
|---|---|---|---|---|
| `--device-stack-protector` | `byte_2A5F1FE` | `byte_2A5F1FF` | `uint8_t` | 0 |
| `--device-stack-protector-frame-size-threshold` | `dword_2A5F1F8` | `byte_2A5F1FC` | `int32_t` | 0 |
| `--sanitize=<tool>` | (ptxas-internal) | (ptxas-internal) | string | unset |

The split between "seen" and "value" matters: `byte_2A5F1FF == 0` means the
flag was absent from the command line (so nvlink should not forward a
`--device-stack-protector=…` argument to embedded ptxas at all). `byte_2A5F1FE`
is only meaningful when the seen byte is non-zero, and its value (`0` or `1`)
selects between the literal flag strings `--device-stack-protector=false` and
`--device-stack-protector=true`.

### Forwarding into Embedded ptxas (sub_429BA0)

`sub_429BA0` is the LTO ptxas flag-string assembler. When LTO finalisation
calls embedded ptxas, this function walks the global flag bytes and
concatenates the corresponding argv pieces:

```c
// Reconstructed from sub_429BA0
char *ptxas_argv_buf = arena_alloc(arena, total_len);
char *cursor        = ptxas_argv_buf;

if (byte_2A5F1FF || byte_2A5F1FC || dword_2A5B518 != 1) {
    // At least one stack-protector flag is in play -- this branch builds the
    // forwarded argv.  dword_2A5B518 is the global "default behaviour"
    // bypass; when != 1, forwarding happens even if no flag was passed.

    if (byte_2A5F1FC) {                                 // threshold seen
        char threshold_buf[50];
        size_t n = snprintf(threshold_buf, 50,
                            "--device-stack-protector-frame-size-threshold=%d",
                            dword_2A5F1F8);
        if (n > 49) sub_467460(err_ctx,
                               "--device-stack-protector-frame-size-threshold");
        // n > 49 means the integer rendering overflowed 50 bytes -- only
        // possible if dword_2A5F1F8 has > 30 digits, i.e. negative
        // INT_MIN-like values.  The 50-byte buffer is fixed-size.
    }

    if (byte_2A5F1FF) {                                 // protector seen
        const char *piece = byte_2A5F1FE
            ? "--device-stack-protector=true"
            : "--device-stack-protector=false";
        // ... append to cursor
    }
}
```

The 50-byte buffer for the threshold flag is the tightest size that fits
`--device-stack-protector-frame-size-threshold=` (45 bytes) plus the
`%d`-formatted integer (up to 11 bytes for INT_MIN) plus the NUL. If the
integer prints to more than 4 bytes the snprintf return value crosses the 49
threshold and a diagnostic is raised through `sub_467460`. In practice this is
unreachable because the option parser already validates the integer is
non-negative.

> ⚡ **QUIRK — flag forwarding bypasses the standard -Xptxas path**
>
> The CLI flags `--device-stack-protector*` are nvlink flags, but their effect
> is entirely deferred to embedded ptxas. `sub_429BA0` reconstructs the
> equivalent ptxas command line as if the user had typed
> `-Xptxas=--device-stack-protector=true`. This duplication exists because
> `--device-stack-protector` is also accepted directly by `ptxas` as a
> top-level flag, so nvlink users expect the short form, but the embedded
> ptxas invocation is internal and needs the explicit flag string. There is no
> shared global between the two — nvlink's option storage and embedded
> ptxas's option storage are independent variables, and `sub_429BA0` is the
> bridge.

## The `--sanitize` Argument

`--sanitize` is a ptxas-resident option with a two-element domain. The
registration string at `0x1EEB9B0` is `"memcheck,threadsteer"` — these are the
two accepted values:

- **`memcheck`**: instruments every load/store with a call to one of the seven
  `__cuda_sanitizer_memcheck_*` hooks. Produces an `EIATTR_SANITIZE` attribute
  on each instrumented function.
- **`threadsteer`**: instruments synchronisation primitives for race detection.
  Shares the same EIATTR but uses a different hook namespace.

The "validator" string at `0x1EEC336` (`"--sanitize"`) is what `sub_1104950`
(an embedded-ptxas option-validation function) compares argv against when
validating `-Xptxas`-forwarded sanitize flags. The error path on an unknown
sanitizer name reads `'--sanitize'` from this address and feeds it into a
generic "unrecognised option value" diagnostic.

## Cross-Version Sanitizer Compatibility

CUDA 13.0 introduced a hard check that prevents linking sanitized objects
across toolkit major versions. The error message lives at `0x1D393D8`:

```text
Cannot link sanitized object '%s' from version %d with sanitized object from
a different toolkit version (%d)
```

The check fires inside the input-loop validator (`sub_426570`, see
[Compatibility Checking](../targets/compatibility.md)) after the
`EIATTR_SANITIZE` attribute has been read from the cubin's `.nv.info` section.
Two cubins both carrying `EIATTR_SANITIZE` must have identical
`EIATTR_CUDA_API_VERSION` major numbers, or this diagnostic fires before any
merge work begins. Non-sanitized cubins can link freely against sanitized ones
of any version — the constraint is only sanitized-vs-sanitized.

The rationale: the sanitizer runtime ABI for the seven memcheck hooks changed
between CUDA 12.x and 13.0. Specifically, the parameter list of
`__cuda_sanitizer_memcheck_global` gained the `pc_lo`/`pc_hi` arguments used to
report the source-level program counter where the violation occurred. A cubin
compiled against the CUDA 12 ABI calls the hook with 5 parameters; a CUDA 13
cubin calls it with 7. Both cubins use the same weak declaration synthesised
by nvlink at link time — so if both ABIs end up in the same final binary,
exactly one of them is calling the runtime with the wrong argument count, and
the violation reports become unreliable. The cross-version check is the only
sound way to catch this without changing the weak declarations themselves.

> ⚡ **QUIRK — sanitizer compat is asymmetric**
>
> The compat rule is not "all cubins from the same toolkit version" — it is
> "all sanitized cubins from the same toolkit version". A binary may freely
> mix CUDA 12 and CUDA 13 non-sanitized cubins (subject only to the SM arch
> rules) but the moment two of them are sanitized they must agree on toolkit
> version. This is the only cross-version check in nvlink that depends on a
> `.nv.info` attribute rather than on the ELF header's toolkit version field.

## EIATTR Attribute Layout

### EIATTR_SANITIZE (id 0x5C, "Indexed" format)

Indexed format means the attribute records a function index rather than a
range of offsets. The encoding in the `.nv.info` section is:

```text
+0:  uint8_t  format     = 0x04  (EIFMT_HVAL_INDEXED)
+1:  uint8_t  attribute  = 0x5C  (EIATTR_SANITIZE)
+2:  uint16_t value      = func_index   // 1-based into .symtab
```

Total record size: 4 bytes. A function carries this attribute if and only if
it was compiled with `--sanitize=<tool>` and at least one memory access in its
body was instrumented. Trivial functions (e.g., constexpr-only bodies) may
omit the attribute even when the compilation unit was sanitized.

### EIATTR_STACK_CANARY_TRAP_OFFSETS (id 0x57, "Free" format)

Free format means a variable-length list of 32-bit offsets:

```text
+0:  uint8_t  format     = 0x03  (EIFMT_HVAL_FREE)
+1:  uint8_t  attribute  = 0x57  (EIATTR_STACK_CANARY_TRAP_OFFSETS)
+2:  uint16_t length     = sizeof(uint32_t) * N
+4:  uint32_t offsets[N]                 // bytes from function entry
```

Each `offsets[i]` points to a `BAR.CLUSTER` or equivalent trap instruction
that aborts the kernel when the canary value has been overwritten. nvlink does
not validate that the targets actually contain a trap instruction — it
preserves the attribute verbatim across the merge.

### Layout in `.nv.info.<funcname>`

The two attributes typically appear together in a single `.nv.info.<funcname>`
section emitted per-instrumented function:

```text
Offset  Size  Contents
------  ----  ----------------------------------------
+0      4     EIATTR_REGCOUNT (id 0x12, sized)
+4      4     EIATTR_MIN_STACK_SIZE (id 0x12)
+8      4     EIATTR_FRAME_SIZE (id 0x11)
+0C     4     EIATTR_SANITIZE (id 0x5C, indexed)            <-- 4 bytes
+10    var    EIATTR_STACK_CANARY_TRAP_OFFSETS (id 0x57)    <-- 4 + 4N bytes
+10+v   ...   EIATTR_OTHER_*
```

Order within the per-function `.nv.info` section is not strictly fixed — the
embedded ptxas emits in a stable order and nvlink preserves it, but the
attribute consumers in the CUDA runtime use a linear scan, not a positional
lookup. See [`.nv.info` Metadata](../elf/nv-info.md) for the full attribute
catalogue.

## Linker Behaviour Summary

| Stage | Action |
|---|---|
| Option parse (`sub_427AE0`) | Register `device-stack-protector*` flags into globals |
| Input scan (`sub_426570`) | Read `EIATTR_SANITIZE`, enforce cross-version rule |
| LTO ptxas dispatch (`sub_429BA0`) | Forward `--device-stack-protector*` to embedded ptxas |
| PTX prelude (`sub_15B86A0`) | Emit `.weak .func` decls for 7 sanitizer hooks |
| Section merge | Preserve `.nv.info.*` attributes verbatim |
| DCE (`sub_1CAE070`) | Skip stripping for any symbol with `__cuda_sanitizer` prefix |
| Finalisation | Emit `EIATTR_SANITIZE` / `EIATTR_STACK_CANARY_TRAP_OFFSETS` in output `.nv.info` |
| Mercury output (sm ≥ 100) | Same EIATTRs flow through the FNLZR pipeline unchanged |

> ⚡ **QUIRK — sanitized objects on Mercury targets retain CUDA 12 attributes**
>
> The Mercury post-link finaliser (FNLZR, see [mercury/fnlzr.md](../mercury/fnlzr.md))
> recodes most `.nv.info` attributes into capsule form. `EIATTR_SANITIZE` and
> `EIATTR_STACK_CANARY_TRAP_OFFSETS` pass through Mercury **without**
> re-encoding. This is because the sanitizer runtime is part of the user-space
> CUDA driver and does not run inside the Mercury capsule's protected
> execution domain — the attributes are read by the host-side sanitizer tool,
> which sees the raw `.nv.info` block regardless of whether the kernel was
> finalised through FNLZR.

## Reimplementation Notes

To reproduce the sanitizer/stack-protector integration in a clean-room
re-implementation of nvlink:

1. **Build the intrinsic registry** as a fixed table of 608 names with
   monotonic IDs. The seven sanitizer hooks must occupy IDs `0x12`–`0x18` in
   the order `free, generic, global, local, malloc, readmetadata, shared`.

2. **Emit weak declarations** in PTX prelude pass. The exact byte sequences
   are reproduced verbatim above; do not paraphrase them — the PTX parser
   tokenises whitespace-sensitively.

3. **Register CLI flags** as documented. The "seen byte" / "value byte" split
   is essential: the embedded ptxas forwarder must distinguish "flag absent"
   from "flag set to false".

4. **Forward flags** through a string-assembly path that produces the
   canonical `--device-stack-protector=true|false` and
   `--device-stack-protector-frame-size-threshold=N` strings. Do not attempt
   to share global state with the embedded ptxas — use the same flag-string
   interface that a user would type.

5. **Implement the cross-version check** as a strict equality between the
   `EIATTR_CUDA_API_VERSION` major numbers of any two cubins both bearing
   `EIATTR_SANITIZE`. Non-sanitized cubins are exempt.

6. **Preserve `__cuda_sanitizer` prefix** through DCE. The 16-byte prefix is
   the contract; do not narrow it to the seven canonical names.

7. **Round-trip `EIATTR_STACK_CANARY_TRAP_OFFSETS`** verbatim through merge
   and Mercury finalisation. Do not attempt to compress or coalesce the
   offsets — the runtime expects them in source order.

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|--:|---|
| `sub_158A600` registers 608 intrinsics including 7 sanitizer hooks | HIGH | Context file `sub_158A600_0x158a600.md` shows `sub_448E70(v3, "__cuda_sanitizer_memcheck_*", id)` calls at lines 46–52 |
| Sanitizer hook IDs are 0x12–0x18 contiguous | HIGH | Decompiled IDs in `sub_158A600` are literal `(pthread_mutexattr_t *)0x12` … `0x18` |
| `sub_15B86A0` is a 1080-case switch | HIGH | Function size 34,362 B, 1084 basic blocks, dispatch table `0x15B86B1` documented in context file |
| Switch cases 1073–1079 emit the 7 sanitizer declarations | HIGH | `strcpy` calls in `sub_15B86A0` at addresses `0x15B86CF`, `0x15B880E`, `0x15C0CCD` etc. confirm the case-to-string mapping |
| `sub_14932E0` size constants `0xCF`/`0xBD`/`0x9F`/`0x189`/`0x1B2`/`0x172`/`0x1F4` | HIGH | Decompiled context for `sub_15B86A0` shows these literals immediately before each `strcpy` |
| PTX declaration string addresses `0x1F8B5D0`+ | HIGH | All 7 addresses verified by `jq` against `nvlink_strings.json` |
| `sub_1CAE070` 16-byte memcmp `"__cuda_sanitizer"` | HIGH | Context file shows `memcmp(name, "__cuda_sanitizer", 0x10u)` twice in symbol-iteration loop |
| `--device-stack-protector` flag globals `byte_2A5F1FE/FF` | HIGH | `sub_427AE0` registration calls `sub_42E390(ctx, "device-stack-protector", &byte_2A5F1FE, 1)` and `sub_42E580(...)` returns are stored in `byte_2A5F1FF` |
| `--device-stack-protector-frame-size-threshold` globals `byte_2A5F1FC` / `dword_2A5F1F8` | HIGH | `sub_42E390(ctx, "device-stack-protector-frame-size-threshold", &dword_2A5F1F8, 4)` |
| `sub_429BA0` forwards flags via `snprintf("--device-stack-protector-frame-size-threshold=%d", …)` | HIGH | Context file `sub_429BA0_0x429ba0.md` shows the `snprintf` with literal format string and 50-byte buffer |
| 50-byte threshold buffer and overflow path | HIGH | Decompiled `snprintf(v14, 0x32u, …) > 49` check with error path to `sub_467460` |
| `--sanitize=memcheck,threadsteer` accepted domain | HIGH | String `"memcheck,threadsteer"` at `0x1EEB9B0` and registration call in `sub_1103030` context file |
| Cross-version error message at `0x1D393D8` | HIGH | String verified in `nvlink_strings.json`; cross-referenced from `versions.md` and `reference/elflink-errors.md` |
| EIATTR_SANITIZE id `0x5C`, indexed format | HIGH | `elf/nv-info.md` catalogue line 92 |
| EIATTR_STACK_CANARY_TRAP_OFFSETS id `0x57`, free format | HIGH | `elf/nv-info.md` catalogue line 87 |
| ID-to-case offset `case = id + 1055` | MEDIUM | Derived from `0x12 + 1055 = 1073` and `0x18 + 1055 = 1079`; consistent across all 7 cases but no explicit constant in the binary |
| Param counts and types for the 7 hooks | HIGH | Reproduced verbatim from the truncated declarations in the strings file, then verified parameter counts by counting `param_N` tokens |
| Cubin ABI differences between CUDA 12 and 13 for `memcheck_global` | LOW | Inferred from the existence of the cross-version error and the 7-vs-5 parameter difference visible in the declaration; the precise ABI change has not been confirmed against a paired CUDA 12 / CUDA 13 cubin |
| Mercury FNLZR pass-through of sanitizer attributes | MEDIUM | Inferred from the absence of a Mercury-specific recoding entry in the FNLZR transform table; not directly observed from a sanitized-Mercury output |
| `dword_2A5B518 != 1` bypass in `sub_429BA0` | MEDIUM | Decompiled condition present; meaning of the global as a "default behaviour bypass" inferred from the surrounding code, not from a label |

## Cross-References

### nvlink Internal

- [.nv.info Metadata](../elf/nv-info.md) — full attribute catalogue including
  `EIATTR_SANITIZE` (line 92) and `EIATTR_STACK_CANARY_TRAP_OFFSETS` (line 87)
- [Versions](../versions.md) — toolkit-version numbering and the sanitized
  cross-version constraint
- [Compatibility Checking](../targets/compatibility.md) — input validation in
  `sub_426570` where the cross-version sanitizer rule fires
- [CLI Flags Reference](../config/cli-flags.md) — flag-row entries 6, 7 for
  `--device-stack-protector*`
- [Embedded ptxas Options](../config/ptxas-options.md) — `--sanitize` argument
  semantics inside embedded ptxas
- [LTO Option Forwarding](../lto/option-forwarding.md) — `sub_429BA0` and the
  general flag-forwarding pipeline
- [Dead Code Elimination](dead-code-elimination.md) — DCE path that consults
  `sub_1CAE070` for the `__cuda_sanitizer` prefix preservation
- [Weak Symbol Handling](weak-symbols.md) — the standard weak-symbol resolver
  that runs alongside the sanitizer-preservation pass
- [Mercury FNLZR](../mercury/fnlzr.md) — post-link finaliser that passes
  sanitizer EIATTRs through unchanged
- [Error Reporting](../infra/error-reporting.md) — diagnostic infrastructure
  used for the sanitize-mismatch error
- [elfLink Errors](../reference/elflink-errors.md) — full catalogue of error
  strings including `0x1D393D8`

### Sibling Wikis

- [ptxas: ptx-parsing](../../ptxas/ptx-parsing.html) — standalone ptxas
  documentation of the sanitizer intrinsic family (`__cuda_sanitizer_memcheck_*`)
- [cicc: targets](../../cicc/targets/index.html) — front-end emission of the
  sanitizer call sites that nvlink ultimately satisfies
