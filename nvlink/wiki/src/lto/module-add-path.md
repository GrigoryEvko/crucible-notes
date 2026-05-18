# Module-Add Path (`sub_4CE070`)

`sub_4CE070` is the input-binding stage of the embedded compile driver: the point at which a payload (PTX text, NVVM bitcode, cubin, or fatbin slice) is classified, validated against the driver's context magic, and attached to the per-compile state object so that the subsequent `sub_4CE8C0` invocation has a single canonical handle for whichever input flavor came in. It sits between architecture/option configuration (`sub_4CE2F0` / `sub_4CE3B0` / `sub_4CE380` / `sub_4CE640` / `sub_4CE3E0`) and the actual compile launch (`sub_4CE8C0`), and is therefore the gate every compile request must pass before the backend will touch anything.

The function is 633 bytes across 35 basic blocks (`0x4ce070`–`0x4ce2e9`) and is invoked from exactly three sites: `sub_4BD0A0` (the split-compile worker's per-item entry, see [Split Compilation](split-compilation.md)), `sub_4BD4E0` (the whole-program ptxas wrapper, see [LTO Overview](overview.md)), and `sub_4BD760` (the relocatable ptxas wrapper). All three call it with the same shape: context pointer first, payload pointer second. This single-entry-three-callers pattern is why the LTO pipeline can keep one classifier rather than open-coding the magic checks at each compile site.

## Position in the Compile Driver

The compile driver maintains a context object (272+ bytes, allocated by `sub_4CDD60`) whose first qword is the signature `0x1464243BC`. Every API entry validates this magic before touching the object. The standard call sequence enforced by `sub_4BD0A0` is:

```
sub_4CDD60(&ctx)              create context
sub_4CE3B0(ctx, mode)         set compilation mode (0/2/4/6)
sub_4CE2F0(ctx, sm_version)   set target architecture
sub_4CE380(ctx)               enable optimizations (if requested)
sub_4CE640(ctx, 1)            set 64-bit mode (if requested)
sub_4CE3E0(ctx, opt_string)   pass option string (whole/relocatable variants)
-----------------------------------------------------------------
sub_4CE070(ctx, payload)      <-- THIS PAGE: classify + bind input
-----------------------------------------------------------------
sub_4CE8C0(ctx, payload)      execute compilation
sub_4CE670(ctx, &buf, ...)    retrieve outputs
sub_4BE400(ctx)               destroy context
```

`sub_4CE070` is the only step that inspects the payload bytes. Every earlier configuration call sets a scalar; every later call assumes the input has already been categorised. Classification cannot be deferred to `sub_4CE8C0`: the compile launcher chooses different lowering pipelines based on the tag written into the context at `ctx + 80`.

## Context Magic Validation

```c
result = 1;
if ( !a1 )                          // ctx == NULL
    return result;
result = 2;
if ( *(_QWORD *)a1 != 0x1464243BCLL )  // signature mismatch
    return result;
```

Two early returns guard the function. `1` signals a null context (caller bug); `2` signals signature mismatch (use-after-free, wrong handle, or corrupted memory). Both callers (`sub_4BD0A0` and `sub_4BD4E0`) treat any non-zero return as fatal — they jump straight to `sub_4BE400` (context destructor) and return error code 5 to their own caller. The magic word `0x1464243BC` is the same constant the destructor (`sub_4BE400`) and the compile launcher (`sub_4CE8C0`) check for, and is set exclusively by `sub_4CDD60` during context allocation.

## Thread-Local Error State Save

```c
v3 = sub_44F410(a1, a2);            // TLS error-state block
v4 = *((_QWORD *)v3 + 1);           // current jmp_buf chain head
v13 = v3;                           // save block pointer
*((_QWORD *)v3 + 1) = env;          // install our env as new head
v14 = v4;                           // save old chain link
v16 = *v3;                          // save error byte 0
LOBYTE(v4) = v3[1];                 // save error byte 1
*(_WORD *)v3 = 0;                   // clear both error bytes
v17 = v4;
```

`sub_44F410` returns the per-thread error/exception block, an allocation associated with the current pthread via `pthread_getspecific`. The block carries (a) the current `setjmp` chain head pointer at offset 8 and (b) two diagnostic bytes at offset 0 and 1 used by the binary's exception machinery to track whether an error was signalled and whether it was suppressed. `sub_4CE070` saves all three slots, installs its own `jmp_buf` as the new chain head, and zeroes the diagnostic bytes so that classifier callees can independently signal failures without contaminating outer state.

This is the only LTO-pipeline function that wraps a classifier in `setjmp`. The reason becomes clear when you look at what the classifier touches: it dereferences caller-supplied pointers (`a2->__size`), calls into the error-reporting subsystem (`sub_467460` for diagnostics, which can `longjmp` on fatal warning levels), and walks PTX text byte-by-byte. Any of those can throw via the binary's own exception protocol. The wrapper turns those throws into the dedicated return code `5` ("classification raised") so the caller can clean up the context deterministically.

## The setjmp Landing Pad

```c
if ( _setjmp(env) )
{
    *((_QWORD *)v13 + 1) = v14;     // restore chain head
    *(_WORD *)v13 = 257;            // mark error pending (0x101)
    goto LABEL_6;                   // shared exit
}
```

If any callee `longjmp`s into `env`, the function restores the saved chain link and sets the error-bytes to `0x0101` (both diagnostic bits set, signalling "error was raised and not suppressed"). Control then falls into the shared exit (`LABEL_6`) which re-reads the diagnostic byte and returns either `0` (success), `5` (error escalated to fatal during cleanup), or whichever return value the normal path produced.

## Payload Classification

After the `setjmp` returns 0 (normal-path), the function stores the raw payload pointer into the context and runs four discriminating tests on the first bytes of the buffer:

```c
*(_QWORD *)(a1 + 72) = a2;          // ctx[72] = payload pointer

if ( a2 )
{
    // Test 1: NVVM IR wrapper magic
    if ( (*(_QWORD *)a2->__size & 0xFFFFFFFFFFFFLL) == 0x1BA55ED50LL )
    {
        *(_DWORD *)(a1 + 80) = 2;   // tag: NVVM bitcode
        goto LABEL_18;
    }

    // Test 2: ELF cubin
    if ( sub_43D970(a2) )            // *(int*)a2 == 0x464C457F  (\x7fELF)
    {
        if ( *(_WORD *)(sub_448360((__int64)a2) + 18) == 190 )
                                     //   e_machine == EM_CUDA (190)
        {
            *(_DWORD *)(a1 + 80) = 3;  // tag: cubin
            goto LABEL_18;
        }
    }

    // Test 3: Fatbin/legacy container magic
    if ( LODWORD(v5->__jmpbuf[0]) == 518347265
         || (!LODWORD(v5->__jmpbuf[0])
             && HIDWORD(v5->__jmpbuf[0]) == 518347265) )
                                     // 518347265 == 0x1EE55A01
    {
        *(_DWORD *)(a1 + 80) = 1;    // tag: fatbin/legacy
        goto LABEL_18;
    }

    // Test 4: PTX text
    if ( sub_4CDF80((char *)v5) )    // scans .version after WS/comments
    {
        *(_DWORD *)(a1 + 80) = 4;    // tag: PTX
        goto LABEL_18;
    }

    // No match -> diagnostic + return 2
    sub_467460(dword_2A5BFA0, 0, 30675157);
    sub_44F410((__int64)dword_2A5BFA0, 0)[1] = 0;
    ...
    return 2;
}
else
{
    sub_44F410((__int64)env, (pthread_mutexattr_t *)env)[1] = 0;
    ...
    return 1;                       // payload == NULL
}
```

### Tag Catalog

| Tag | Class | Detection | Notes |
|---|---|---|---|
| 1 | Fatbin / legacy container | First or second `int` equals `0x1EE55A01` | Two-position match handles 4-byte alignment padding ahead of the container. Used for embedded `.nv_fatbin` slices that arrive after fatbin extraction has already peeled off the outer wrapper |
| 2 | NVVM IR (LLVM bitcode wrapper) | Low 48 bits of first qword equal `0x1BA55ED50` | The high 16 bits are masked off because the NVVM wrapper carries a flags/version field at bits 48–63 that varies between toolkit versions. The 48-bit core is the stable magic |
| 3 | Cubin (CUDA ELF) | `\x7fELF` magic AND `e_machine == 190` (`EM_CUDA`) | `sub_43D970` does the ELF check; `sub_448360` is the identity wrapper that returns the same pointer (used here as a marker for the bytes-at-offset-18 read so that IDA's type analyser keeps the cast clean) |
| 4 | PTX text | Starts with `.version` after skipping ASCII whitespace and `//` / `/*` comments | `sub_4CDF80` uses `__ctype_b_loc()` to skip space-class bytes and `sub_45CB90` to skip over block/line comments before doing the 8-byte `memcmp(".version", ...)` |

### Test Order Is Significant

The order is bitcode → ELF → fatbin → PTX text. PTX is checked last because its detection is the only one that walks an arbitrary number of bytes (whitespace and comment skipping), whereas the other three are constant-time prefix matches. ELF is checked before fatbin because a stray `0x1EE55A01` could in principle appear at offset 4 inside an ELF header (it cannot — that position is `e_machine`/`e_version` — but the ordering removes any ambiguity).

### Diagnostic on No-Match

`sub_467460(dword_2A5BFA0, 0, 30675157)` emits a "could not classify input" diagnostic. `dword_2A5BFA0` is the warning channel descriptor (it is the third argument used here, not as data — see [Error Reporting System](../infra/error-reporting.md) for the channel taxonomy). The constant `30675157 = 0x1D410D5` is a pointer into `.rodata` to the format string. After emitting the diagnostic, the function clears the second diagnostic byte (so the error is reported but does not escalate to fatal) and returns `2`.

## Tag-Indexed Downstream Dispatch

After `LABEL_18` writes the tag into `ctx + 80`, the function falls into the shared exit path (`LABEL_6`) which restores the TLS state and returns. The compile launcher (`sub_4CE8C0`) then reads `ctx + 80` to pick the lowering pipeline:

| Tag | `sub_4CE8C0` pipeline | Description |
|---|---|---|
| 1 | Fatbin slice unpacker | Decodes the embedded container, iterates members, and recurses with each member rebound via a second `sub_4CE070` call on the inner ctx. Used when input came from a `.nv_fatbin` section the outer linker did not pre-peel |
| 2 | NVVM bitcode reader | Hands the buffer to the libnvvm side via a deferred bind (the launcher itself does not link against libnvvm — for tag 2 it currently emits an internal error because `sub_4CE070` is reached only via the ptxas wrappers, which never see bitcode in production. See QUIRK 2 below) |
| 3 | Cubin pass-through | Skips compilation, just validates and forwards. The cubin already is SASS; the only work `sub_4CE8C0` does is to wrap it in the compile driver's output envelope so `sub_4CE670` can return it uniformly |
| 4 | PTX parser → ISel → emit | The normal PTX-to-SASS pipeline. This is the path every LTO output takes in practice: libnvvm produces PTX text, nvlink hands it to one of the ptxas wrappers, those wrappers call `sub_4CE070` which tags the input `4`, and `sub_4CE8C0` runs the full backend |

## Why The Wrapper Cannot Be Inlined

A natural question is why classification is not just done inline in each of the three callers. The function is 633 bytes, called from three sites, and has an obvious "inline me" silhouette. The reason is the `setjmp` wrapper.

The three callers (`sub_4BD0A0`, `sub_4BD4E0`, `sub_4BD760`) each have their own `setjmp`/`longjmp` chain frames installed earlier. They cannot install another `jmp_buf` inline because the chain head is a per-thread singleton — installing a new env replaces the outer wrapper's. By making `sub_4CE070` its own function, each caller's `setjmp` installation runs in a fresh stack frame, and the chain-head save/restore happens at exactly the right pair of points (function entry / function exit). Inlining the body would require duplicating the chain-head management three times in three different surrounding contexts, and the saved-state slots are stack-allocated, so the lifetimes would not survive across the callers' own setjmp boundaries.

## Return Code Summary

| Return | Meaning | Caller response |
|---|---|---|
| 0 | Classified and bound successfully | Continue to `sub_4CE8C0` |
| 1 | `a1 == 0` (null context) OR `a2 == 0` (null payload) | Fatal: caller bug; destroy ctx, return 5 |
| 2 | Signature mismatch on `a1` OR no classifier matched `a2` | Fatal in caller; diagnostic already emitted for the no-match case |
| 5 | Error escalated during classifier cleanup | Caller jumps to `sub_4BE400` and returns 5 itself |

## Tracking Globals

| Address | Role |
|---|---|
| `dword_2A5BFA0` | Warning channel descriptor used for the "could not classify input" diagnostic |
| `0x1464243BC` | Compile-driver context signature; written by `sub_4CDD60`, checked here and in every other compile-driver entry |
| `0x1BA55ED50` | NVVM IR wrapper magic (48-bit) |
| `0x1EE55A01` | Fatbin / legacy container magic |
| `0x464C457F` | ELF magic (checked indirectly via `sub_43D970`) |
| `190` | `EM_CUDA` for ELF `e_machine` |
| `30675157` (`0x1D410D5`) | Format string for the classification-failure diagnostic |
| Context offset 72 | Payload pointer storage |
| Context offset 80 | Tag storage (1/2/3/4) |

## QUIRKs

### QUIRK 1: NVVM Magic Is Masked To 48 Bits

The bitcode check reads a full qword and `AND`s with `0xFFFFFFFFFFFF` before comparing to `0x1BA55ED50`. The top 16 bits of the actual file header carry a version/flags word that NVIDIA increments across toolkit releases. The classifier intentionally ignores them so that an old nvlink can still tag a newer bitcode container correctly — the tag-3 versus tag-2 routing decision must not depend on version stamps. Validation of the version field happens later, inside the bitcode reader on the libnvvm side, where it can be turned into a "minimum toolkit" diagnostic rather than a "could not classify" one. See [LTO IR Format Versions](ir-format-versions.md) for the version tables.

### QUIRK 2: Tag 2 Is Reachable But Unused In Production

`sub_4CE070` happily classifies a payload as NVVM bitcode (tag 2) when invoked through `sub_4BD0A0` / `sub_4BD4E0` / `sub_4BD760`, but those three callers always feed it PTX text. In the LTO pipeline, NVVM bitcode is consumed by `sub_4BC4A0` / `sub_4BD1F0` (which call `nvvmAddModuleToProgram` directly via dlsym) — it never reaches the embedded ptxas wrappers. The tag-2 branch is therefore vestigial in the LTO path. It is exercised only when someone constructs an explicit compile-driver context outside the LTO flow and hands it a bitcode buffer, which the public driver API supports for compatibility with older toolchain configurations but `main()` never does. Removing the branch would shrink the function, but doing so would break any third-party caller that links against the embedded driver via its (undocumented) symbols.

### QUIRK 3: Two-Position Fatbin Magic Check

Tag 1's test reads `(LODWORD == 0x1EE55A01) || (LODWORD == 0 && HIDWORD == 0x1EE55A01)`. The second arm — magic at offset 4 with a zero word at offset 0 — handles the case where the fatbin slice arrives from a caller that prepended an 8-byte zero header. This happens specifically when the input comes through the archive-extraction path (`sub_42AF40` → `sub_42A680`), which copies the member into a buffer with an 8-byte alignment prefix on some toolkit versions. Rather than fix the prefix in the producer, NVIDIA accepts both positions in the consumer. The check is order-sensitive: the second arm requires the first 4 bytes to be exactly zero, not just "not the magic", so a buffer whose first 4 bytes happen to be `0x00000001` (a perfectly legal NVVM bitcode prefix in some encodings) cannot accidentally match here because tag 2 would have matched first.

## Related Pages

- [LTO Overview](overview.md) — full pipeline showing where `sub_4CE070` fits in Phase 8h
- [libnvvm Integration](libnvvm-integration.md) — the separate path bitcode actually travels through
- [Split Compilation](split-compilation.md) — per-module compile driver invocations via `sub_4BD0A0`
- [Whole vs Partial LTO](whole-vs-partial.md) — the two non-split wrappers (`sub_4BD4E0` / `sub_4BD760`)
- [Input File Loop](../pipeline/input-loop.md) — outer dispatch that decides what kind of payload to hand to the compile driver in the first place
- [File Type Detection](../input/file-type-detection.md) — the linker's own (different) input classifier, run before LTO
- [Error Reporting System](../infra/error-reporting.md) — `sub_467460` channel taxonomy and the diagnostic byte protocol
- [ELF Parsing](../input/elf-parsing.md) — what `sub_43D970` / `sub_448360` and the `e_machine == 190` check mean in the wider linker
