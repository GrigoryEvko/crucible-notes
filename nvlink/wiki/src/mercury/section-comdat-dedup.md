# Mercury Section Content-Equality Dedup (`sub_4748F0`)

The closest analog in nvlink to a traditional ELF COMDAT group is the **mercury** section family (`.nv.merc.*`). The linker performs content-equality elimination on these sections inside the orchestrator at `0x4748F0` -- an 8,950-byte function (325 basic blocks, 73 callees) that also drives debug-info merging. The check happens late in the merge phase, after symbol resolution but before the final section table is materialised, and operates on the parallel vector layout that the input loop builds for every cubin contributing to the link.

This page documents the algorithm at decompiled lines 1564-1670 in detail: how it walks the parallel vectors, what fields gate acceptance, why the prefix-strip arithmetic uses `+8` instead of the textually obvious `9`, and which `.nv.merc.*` section families participate. The corresponding orientation summary lives in [Section Merging](../linker/section-merging.md#mercury-sections-content-equality-dedup-sub_4748f0); this page is the reimplementation-grade reference.

## Why Mercury Sections Need COMDAT-Like Semantics

Conventional ELF linkers solve duplicate-debug-info using SHF_GROUP and COMDAT signatures: every `.debug_info` slice from a translation unit is wrapped in a group whose key is the linkonce name; the linker keeps the first key it sees and discards every later group with the same key by signature alone, without comparing payload bytes. Mercury cubins do not emit GROUP sections -- the front-end tooling instead tags duplicate copies with a literal `.nv.merc.` prefix and relies on the linker to perform the equivalent elimination at merge time.

The reason is that Mercury debug data is closely coupled to FNLZR's later code rewrite: a stale `.nv.merc.debug_line` slice that survived a name-based dedup but whose bytes drifted from the kept slice would silently mismap source lines after the finalizer relabels Mercury PCs to SASS PCs. By comparing content rather than names, the dedup engine refuses to fold two slices whose backing bytes diverge even by one relocation entry, sacrificing some output size for an unconditional safety guarantee.

The dedup is therefore conservative on purpose: it is a **content-equality** check, not a name-equality check.

## Entry Conditions

The dedup block at lines 1564-1670 of `sub_4748F0` runs once per `(reference, candidate)` pairing. The caller -- `sub_4748F0` itself, recursively, at `0x4768FA` -- walks every pair of cubins that contributed mercury sections and invokes the dedup arm with:

| Vector role | Source | Meaning |
|---|---|---|
| `v402[60]` | Per-pair scratch array, slot 60 | Candidate section header vector (mercury debug + container) |
| `v402[61]` | Per-pair scratch array, slot 61 | Candidate relocation-target descriptor vector |
| `*(QWORD*)(v331 + 24)` | Reference object, offset +24 | Reference section header vector |
| `*(QWORD*)(v331 + 16)` | Reference object, offset +16 | Reference relocation-target descriptor vector |
| `v330` / `v329` | Output pointers | Receive merged-blob pointer and length on accept |

`v331` is the per-cubin descriptor for the reference object that the linker has already chosen to keep. `v402` is the 536-byte staging frame (`memset` at `0x474AE1`, allocation `memset(v402, 0, 536)`) holding the candidate state. The dedup is gated on `BYTE5(a20)`, the bit that the caller sets when the candidate's section table actually carries mercury entries; if the bit is clear the function jumps over the dedup block and behaves as a plain debug-info merge.

The control path enters the dedup at line 1571 after the candidate's primary data vector (`elfw+16`-equivalent slot at `*(QWORD*)(v259 + 8)` with cardinality `*(int*)(v259 + 16)`) has been walked once and compared byte-for-byte to the corresponding reference vector. That first walk is the unconditional gate: if any element fails `memcmp`, the dedup aborts with `v30 = 17` and the candidate is kept.

## The Three-Stage Walk

The dedup advances through three nested walks, each of which can reject independently. All three must succeed before LABEL_229 (the merged-success cleanup) is reached.

### Stage 1: Primary Data Vector

```c
v265 = v263 < 0;
v266 = v35;
if ( v265 )
    v262 = v264;
v267 = v252;
v268 = 0;
v269 = v266;
while ( v262 != v264 )
{
    v270 = *(_QWORD *)(v262 + 8);   // size
    v271 = *(const void **)v262;    // data pointer
    v262 += 16;                     // advance candidate iterator (16-byte entries)
    v272 = *(_QWORD **)v331;
    v360 = v270;
    v273 = (const void *)sub_464DB0(v272, v268);  // reference[v268]
    if ( memcmp(v273, v271, v360) )
    {
        v252 = v267;
        v30 = 17;
        goto LABEL_292;             // REJECT: data bytes differ
    }
    ++v268;
}
```

Each candidate entry is a 16-byte `(data_ptr, size)` pair. The reference is indexed positionally through `sub_464DB0(v272, v268)`, which is a `std::vector::at`-equivalent (returns `*(QWORD*)(*v272 + 8*index)` with a bounds check). Rejection here yields error code `17` -- the byte-level mismatch path.

Notice that the loop only checks data identity in one direction: the reference is read at the same index `v268` as the candidate is walked. There is no cross-check that the reference vector has the same cardinality as the candidate. That requirement is enforced by Stage 2.

### Stage 2: Section Header Vector (`v402[60]` vs `v331+24`)

```c
v277 = sub_464BB0((__int64)v402[60]);
if ( v277 == sub_464BB0(*(_QWORD *)(v331 + 24)) )
{
    v346 = 0;
    while ( v346 < (unsigned __int64)sub_464BB0((__int64)v402[60]) )
    {
        v278 = sub_464DB0(v402[60], v346);
        v279 = *(char **)(v278 + 48);             // candidate section name
        v280 = v278;
        if ( sub_44E3A0(".nv.merc.", v279) )
            v279 += 8;                            // strip ".nv.merc" (8 bytes)
        v281 = 0;
        v282 = v280;
        while ( 1 )
        {
            if ( v281 >= (unsigned __int64)sub_464BB0(*(_QWORD *)(v331 + 24)) )
                goto LABEL_347;                   // REJECT: candidate not found in reference
            v283 = sub_464DB0(*(_QWORD **)(v331 + 24), v281);
            v284 = *(char **)(v283 + 48);
            v285 = v283;
            if ( sub_44E3A0(".nv.merc.", v284) )
                v284 += 8;
            if ( !strcmp(v279, v284)
              && *(_QWORD *)v285 == *(_QWORD *)v282     // offset (+0)
              && *(_QWORD *)(v285 + 8) == *(_QWORD *)(v282 + 8)  // link/info pair (+8)
              && *(_DWORD *)(v285 + 24) == *(_DWORD *)(v282 + 24) )  // addralign (+24)
                break;                            // match found
            ++v281;
        }
        ++v346;
    }
}
else
{
LABEL_347:
    v30 = 19;                                     // REJECT: cardinality or lookup miss
}
```

`sub_464BB0` returns `*(QWORD*)(a1 + 8)`, the vector's `size()` member. Cardinality mismatch immediately falls through to LABEL_347 with error code `19`. When cardinalities agree, every candidate section must find a name-and-metadata twin in the reference. The twin check is an O(N^2) linear search -- acceptable because the section count per cubin is small (the 19 named `.nv.merc.*` slots plus the `.nv.merc.text.*` container) and the cost is bounded.

The metadata gate is the **quad-tuple `(name, offset_or_size_qword, link_info_qword, addralign_dword)`**. The QWORD at offset +0 of the entry holds a packed offset/size field, the QWORD at +8 holds the packed `sh_link`/`sh_info` pair, and the DWORD at +24 holds `sh_addralign`. Two slices with identical bytes but different alignment requirements are not considered duplicates.

### Stage 3: Relocation-Target Vector (`v402[61]` vs `v331+16`)

```c
v302 = sub_464BB0((__int64)v402[61]);
if ( v302 == sub_464BB0(*(_QWORD *)(v331 + 16)) )
{
    v303 = 0;
    while ( sub_464BB0((__int64)v402[61]) > v303 )
    {
        v304 = (char **)sub_464DB0(v402[61], v303);
        v305 = *v304;                          // candidate target name
        if ( sub_44E3A0(".nv.merc.", *v304) )
            v305 += 8;
        v307 = 0;
        while ( 1 )
        {
            if ( sub_464BB0(*(_QWORD *)(v331 + 16)) <= v307 )
            {
                v30 = 18;
                goto LABEL_292;                // REJECT: relocation target not in reference
            }
            v308 = sub_464DB0(*(_QWORD **)(v331 + 16), v307);
            s2 = *(char **)v308;
            v310 = s2;
            if ( sub_44E3A0(".nv.merc.", s2) )
                v310 = s2 + 8;
            if ( !strcmp(v305, v310) )
            {
                v311 = *(_DWORD *)(v308 + 16);
                if ( v311 == *((_DWORD *)v306 + 4) )      // same payload length
                {
                    if ( !memcmp(*(const void **)(v308 + 8), v306[1], v311) )
                        break;                            // bytes match
                }
            }
            ++v307;
        }
        v303 = v351 + 1;
    }
    goto LABEL_229;                            // ACCEPT: all three walks passed
}
v30 = 18;
```

Stage 3 walks the relocation-target descriptors. Each descriptor entry is `(name_ptr, data_ptr, length_dword)` (length at offset +16 in the reference layout, at offset +4 of the candidate's 16-byte pair-of-pointers entry). The walk demands that for every candidate target, the reference holds a target with the same prefix-stripped name, the same payload length, and byte-identical payload. Error code `18` reports a relocation mismatch.

This third walk is the conservative half of the COMDAT analogue: it ensures that the relocation table for the candidate would have rewritten the same target bytes in the same way as the reference's table. A single differing `Elf64_Rela` entry in `.nv.merc.rela.debug_info` falsifies the `memcmp` and forces both copies to survive. Without this stage, two cubins could share identical `.nv.merc.debug_info` payloads but disagree on which symbols those payloads refer to, producing a silently corrupt merged debug section after relocation application.

### Accept Path: `LABEL_229`

LABEL_229 (line 1694) is the cleanup arm. It releases the candidate's scratch vectors via `sub_45B680` on `v402[6]` and `v402[7]`, frees temporary storage via `sub_4746C0(v383)` and `sub_4746C0(v382)`, and returns `v30 = 0` to the caller. The candidate's data blob is *not* copied into the output -- the caller writes `*v330 = (size_t)v402[2]` and `*v329 = (void*)v402[5]` (lines 1444-1445) before invoking the recursive dedup arm, so on a successful match the output already references the reference's blob.

## Reject Paths and Error Codes

The three rejection codes propagate to the caller and decide whether retry or fallback is appropriate:

| `v30` | Source | Condition | Caller response |
|---|---|---|---|
| `17` | Stage 1 (`LABEL_292`) | `memcmp` on a primary-data entry returned non-zero | Keep both copies; emit duplicate section |
| `18` | Stage 3 | Relocation target list mismatch (count, name, length, or bytes) | Keep both copies |
| `19` | Stage 2 (`LABEL_347`) | Section header vector cardinality or quad-tuple mismatch | Keep both copies |
| `0` | LABEL_229 | All three stages accepted | Drop candidate, reuse reference |

Every reject path runs through LABEL_292, which does identical cleanup to LABEL_229 but skips the blob-merge bookkeeping and returns the non-zero code unchanged. The caller (the recursive site at `0x4768FA`) treats any non-zero return as "did not dedup" and proceeds to emit the candidate as an independent section.

## Section Families Participating in the Dedup

Every `.nv.merc.*` literal in the binary is consumed by `sub_4748F0` and its prefix-matching helpers (`sub_1CED0E0`, `sub_1CF1690`, `sub_1CEF5B0`, `sub_1CF3720`, `sub_1CF72E0`). The full table:

| Section name | Vector slot | Role in dedup |
|---|---|---|
| `.nv.merc.debug_abbrev` | `v402[60]` | DWARF abbreviation tables. Per-CU; deduped when two CUs share an identical abbrev set. |
| `.nv.merc.debug_aranges` | `v402[60]` | Address-range lookup table. Identical only when two CUs cover the same PC ranges, rare. |
| `.nv.merc.debug_frame` | `v402[60]` | Call frame information. Frequently identical across template instantiations. |
| `.nv.merc.debug_info` | `v402[60]` | DIE tree. The most valuable dedup target -- whole-CU duplicates here save the most space. |
| `.nv.merc.debug_line` | `v402[60]` | Line number program. Coupled to `.nv.merc.rela` for source-path entries. |
| `.nv.merc.debug_loc` | `v402[60]` | Location lists. Often differs per instantiation; rarely deduped. |
| `.nv.merc.debug_macinfo` | `v402[60]` | Preprocessor macros. Deduped when two TUs share an identical macro set. |
| `.nv.merc.debug_pubnames` | `v402[60]` | Public-name accelerator. Deduped when symbol exports match. |
| `.nv.merc.debug_pubtypes` | `v402[60]` | Public-type accelerator. Deduped under the same conditions as `debug_pubnames`. |
| `.nv.merc.debug_ranges` | `v402[60]` | Disjoint-range tables for split functions. |
| `.nv.merc.debug_str` | `v402[60]` | DWARF string pool. Strict byte-equality required; the string-table dedup (`sub_442400`) operates separately on individual entries. |
| `.nv.merc.nv_debug_line_sass` | `v402[60]` | NVIDIA SASS-level line table -- parallel data structure to `.nv.merc.debug_line` but indexed by SASS PC. |
| `.nv.merc.nv_debug_info_reg_sass` | `v402[60]` | SASS register liveness. |
| `.nv.merc.nv_debug_info_reg_type` | `v402[60]` | Register type annotations. |
| `.nv.merc.nv_debug_ptx_txt` | `v402[60]` | Embedded PTX source. |
| `.nv.merc.symtab_shndx` | `v402[60]` | Extended section-index sidecar for the Mercury symbol table. |
| `.nv.merc.rela` | `v402[61]` | Mercury relocation table. Stage 3 walks this list. |
| `.nv.merc.nv.shared.reserved.*` | `v402[60]` | sm_100+ only. Reserved shared-memory placeholders. The suffix carries the reservation identifier and participates in the `strcmp` after prefix stripping. |
| `.nv.merc` (container) | `v402[60]` | The top-level Mercury instruction container (`.nv.merc.text.<kernel>`). Almost never deduped because two kernels with identical Mercury bytes are unusual, but the path exists. |

The 19 distinct `.nv.merc.*` literals in the binary's `.rodata` (addresses `0x24582E8`-`0x2458D00`) are exactly the set that this dedup recognises. Adding a new mercury section name without updating the producer helpers would not break the dedup -- the algorithm is name-agnostic, gated only on the `.nv.merc.` prefix -- but the new section would not get a dedicated emitter and would therefore never enter `v402[60]`.

## Verbose Tracing

The dedup is silent on the accept path. On reject, verbose mode (`elfw+64 & 1`) emits no per-section diagnostic from `sub_4748F0` itself; the closest verbose string is `"skip mercury section %i"` emitted by `sub_45E7D0` during the earlier merge-phase skip pass. Diagnostic absence is deliberate: a duplicate-debug-info rejection is not actionable from the user's side, since the producer (cicc / ptxas) controls the byte-level layout.

The fastpath optimization trace `"[Finalizer] fastpath optimization applied for off-target %u -> %u finalization\n"` (string `0x1D40610`, line 1844 of the strings table) is the only verbose emission from this function and is unrelated to dedup -- it fires from a separate FNLZR path inside the same orchestrator.

## QUIRKs

### QUIRK Q-MERC-DEDUP-1: Prefix-Strip Uses `+8` Not `+9`

The literal at `0x1D40605` is `.nv.merc.` -- 9 characters including the trailing dot. After `sub_44E3A0(".nv.merc.", name)` returns non-zero (confirming the prefix matches), the code advances the pointer by **8**, not 9:

```c
if ( sub_44E3A0(".nv.merc.", v279) )
    v279 += 8;
```

This is not a bug. `sub_44E3A0` is a starts-with helper that walks character-by-character until either operand hits a null terminator; it returns the second-operand pointer when the first runs out. Stripping 8 bytes leaves the leading dot of the suffix intact: `.nv.merc.debug_info` -> `.debug_info`, which is exactly the canonical name of the corresponding standard ELF section. The dedup then `strcmp`s these canonicalised names against each other. The arithmetic is intentional and matches the canonical/tagged duality: a mercury section is recognised as the same logical entity as its non-mercury sibling sharing the prefix-stripped name. Auditing tools that pattern-match on `+9` will see this as suspicious; it is correct as written.

### QUIRK Q-MERC-DEDUP-2: Cardinality Check Implicit on Stage 1, Explicit on Stages 2 and 3

Stage 1 walks the candidate vector to exhaustion and reads the reference vector positionally with `sub_464DB0(v272, v268)`. If the candidate has more entries than the reference, `sub_464DB0` returns `0` (its bounds-check arm at offset +1 in its decompilation) and `memcmp` is invoked with a null pointer -- which on glibc-Linux dereferences `NULL` for the first byte and segfaults. The function does not survive this case in practice because Stage 2 and Stage 3 explicitly require `sub_464BB0` cardinality equality *before* indexing. Stage 1 therefore relies on an invariant maintained by the caller: the two primary data vectors are always built with the same cardinality from the same input layout. If front-end tooling ever broke that invariant, Stage 1 would crash before reaching the explicit cardinality gate. This is brittle but not exploitable -- the input is always cubin bytes that have passed earlier validation in `sub_1CF07A0` and `sub_1CEF5B0`.

### QUIRK Q-MERC-DEDUP-3: Three Separate `memcmp` Stages, No Hash Shortcut

A natural optimisation would be to hash each candidate section once at emit time and compare 64-bit hashes instead of bytes. nvlink does not do this. Every dedup attempt walks every byte of every candidate section through `memcmp`. For a link with N translation units each contributing M mercury sections of average size S, the cost is O(N^2 * M * S) bytes compared in the worst case. The cost is tolerated because (a) mercury sections are small relative to the SASS payload they describe, (b) most production links have N <= 16, and (c) a hash-collision false-positive would silently corrupt debug data, while byte-equality cannot. The string-table dedup at `sub_442400` does use a hash table (`elfw+296`), but only because string-table entries are short and the hash is a key into a structural map, not a content-equality proof. This asymmetry between the two dedup engines is structural: string dedup is correctness-irrelevant (collision merely wastes space), section dedup is correctness-critical (collision corrupts debug output).

## Cross-References

- [Mercury ELF Sections](elf-sections.md) -- complete catalog of `.nv.merc.*` section types and `sh_flags` encoding.
- [Mercury Compiler Passes](compiler-passes.md) -- producer side; what fills `v402[60]` and `v402[61]` upstream.
- [R_MERCURY Relocations](r-mercury-relocations.md) -- relocation entries that Stage 3 cross-checks.
- [Section Merging](../linker/section-merging.md) -- broader merge phase context, including the constant-dedup and string-table dedup engines.
- [Mercury Debug Sections](../debug/mercury-debug.md) -- downstream consumers of the deduped output.
