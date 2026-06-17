# Methodology

> **Sources pinned for this book.** Five shipped binaries plus one GPL-2.0 kernel source tree, and nothing else:
> · `libnrt.so` → `libnrt.so.2.31.24.0` (`aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`, BuildID[sha1] `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, ELF64 DYN x86-64, **not stripped**, full **DWARF v4** — 331 compile units)
> · `libnccom.so.2.31.24` (`aws-neuronx-collectives 2.31.24.0-1a31ba186`, BuildID `9c00176c081788c9435d27d11bb40e92495463f0`, not stripped, `.debug_info` present)
> · `neuron.ko` (`aws-neuronx-dkms 2.27.4.0` — **GPL-2.0 C source ships in the DKMS package and is authoritative**; the built object is stripped)
> · `libncfw.so` (SONAME `libncfw.so.2.31.1.0.cf13a49f`, BuildID `a98f8e1ca2294582835310c3a1092e0a5e500db5`, **symtab-only, no DWARF**)
> · `libnrtucode_extisa.so` (BuildID `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`, **stripped — no DWARF and no symbol table**)
> · plus the static archive `libnds.a` (6 TUs, 810,674 B), the same code linked into `libnrt.so`.
>
> **Part 0 — Reference Apparatus** / NARRATIVE · **Evidence grade:** this page documents the method; its own claims are facts about the named artifacts (CU counts, section names, tool output) · [back to index](index.md)

## Abstract

This book is built from two evidence sources and no others: **static analysis of the shipped binaries** listed above, and **direct reading of the GPL-2.0 kernel C source** that ships inside the `aws-neuronx-dkms` package. There is no privileged source tree behind any claim — the userspace and firmware components are closed binaries reverse-engineered from their bytes, and the only first-party source ever read is the kernel module, which is published under GPL-2.0 with the driver. Every other page in the book either anchors a claim to an address, symbol, offset, section, or string inside one of those binaries, or anchors it to a `file:line` in the GPL kernel C, or marks the claim as inferred and tags its confidence.

The decisive fact that makes the binary side tractable is that `libnrt.so` ships **not stripped, with full DWARF v4**. Its `.debug_info` exposes 331 `DW_TAG_compile_unit` records, each carrying a verbatim source path in `DW_AT_name` and a common build root in `DW_AT_comp_dir`; reading that table back reconstructs the first-party source tree — 203 `KaenaRuntime` translation units defining 4,407 functions — without ever holding the source. `libnccom.so` carries `.debug_info` likewise. The two firmware/microcode carriers degrade from there: `libncfw.so` has a symbol table but no DWARF, and `libnrtucode_extisa.so` is fully stripped and reached only as a `dlopen` target. The method scales its claims to that evidence floor — full source attribution where DWARF exists, symbol-and-string attribution where only a symtab exists, and blob-carving plus raw disassembly where neither does.

This page documents the method itself: the **evidence hierarchy** ([§1](#1-the-evidence-hierarchy)) that ranks each source by what it yields and how far to trust it; the **toolchain** ([§2](#2-the-toolchain)) of `nm`/`objdump`/`readelf`/`addr2line`/`c++filt` plus the IDA sidecar JSON; the **source-TU attribution** method ([§3](#3-source-tu-attribution)) — DWARF `comp_dir`/`DW_AT_name` where present, and the `__assert_fail` / log-string path-string method for the no-DWARF binaries; the **confidence convention** ([§4](#4-the-confidence-convention)) of CERTAIN/HIGH/MEDIUM/LOW and the CORRECTION/QUIRK/GOTCHA/NOTE callout taxonomy; and a plain statement of **limits** ([§5](#5-limits--what-is-certain-vs-inferred)) — what is certain, what is inferred, where the no-DWARF ceiling sits, and how gaps feed the deep-dive backlog.

---

## 1. The evidence hierarchy

Not all evidence is equal, and the book grades a claim by the strongest source that supports it. The hierarchy below ranks the six evidence types from "source read directly" down to "stripped blob carved by hand". Every page's header blockquote names which tier its claims sit on, so a reader can calibrate trust before reading a single sentence.

| Source | Evidence type | What it yields | Reliability |
|---|---|---|---|
| `neuron.ko` GPL-2.0 C | **Source read directly** | The kernel driver verbatim: IOCTL dispatch, DMA-op layer, DHAL v2/v3/v4 vtable bodies, NQ/reset/CRWL logic — cited `file:line`, not reverse-engineered | CERTAIN — it *is* the source |
| `libnrt.so` DWARF v4 + `.dynsym` | **Full debug info** | 331 CUs, 203 first-party TUs / 4,407 fns with `DW_AT_name` source paths + `DW_AT_low_pc` bodies; 145 `NRT_*` versioned exports; demangled symbols | HIGH — verbatim DWARF facts |
| `libnccom.so` `.debug_info` + `.dynsym` | **Debug info (NCCL fork)** | Comm-init / topology / ring-build / proxy-engine TUs; the libnrt↔libnccom ABI; inherited NCCL bodies mapped by *delta* from upstream `2.31.24+nrt2.0` | HIGH on first-party deltas, MED on inherited bodies |
| `libnrt.so` `.symtab` (24,500 defined) | **Local-symbol table** | Function names with no line table (e.g. the `-g`-less vendored protobuf runtime — 3,003 `google::protobuf` text symbols `nm` sees but DWARF does not) | HIGH on names, no `file:line` |
| `libncfw.so` symtab-only | **Symtab, no DWARF** | Firmware carrier surface: `libncfw_get_image_func` get-image entry, embedded Xtensa blob set, v2 reset prologue — boundary byte-anchored, internal serializer *names* not recoverable | MED — boundary HIGH, interiors capped |
| `libnrtucode_extisa.so` stripped | **Blob-carvable, no symbols** | 30-entry `dlsym` binding table names the API; everything below decoded by raw disassembly against carved Q7 blobs; the embedded `.tie` config makes the GPSIMD **Vision-Q7 ISA fully decode** | MED — surface from the dlsym table; interior from disassembly |

> **NOTE —** the kernel source sits at the *top* of the hierarchy precisely because it is not reverse-engineering: a DHAL vtable body recovered from `neuron.ko`'s GPL C is `file:line`-cited and CERTAIN, where the same logic recovered from a stripped binary would be HIGH at best. The book keeps the two provenances visibly distinct — kernel pages say "GPL C source read directly", binary pages say "byte-anchored" — so a reimplementer knows which claims were *read* and which were *reconstructed*.

> **QUIRK —** DWARF presence and `.symtab` presence are independent axes. The protobuf runtime statically linked into `libnrt.so` is a `-g`-less build: `nm` lists 3,003 `google::protobuf` text symbols (`SerialArena`, `ThreadSafeArena`, `VerifyVersion` @`0x6fb690`), but those TUs emit **no** DWARF compile unit. Reading "absent DWARF CU" as "absent code" is the trap; the symbol table is the corrective second view, and the book cross-checks the two against each other rather than trusting either alone.

---

## 2. The toolchain

The binary side is read with the standard binutils set plus one pre-built IDA sidecar. Nothing exotic: the leverage is in cross-checking independent tools against the same address, not in any single tool.

| Tool | What it recovers |
|---|---|
| `readelf -h/-SW/-lW/-d/-n/-V` | ELF header, section table, segment map, dynamic table, build-id note, symbol-version graph. Establishes the identity-map invariant (every `PT_LOAD` has `p_offset == p_vaddr`, so VMA == file offset) that makes every address on every page usable as a file offset. |
| `readelf --debug-dump=info` | The DWARF CU table: 331 `DW_TAG_compile_unit` records, each `DW_AT_name` (source path), `DW_AT_comp_dir` (build root), `DW_AT_producer` (`GNU C++17 14.2.1 … -O2 -fPIC -ggdb3`), and `DW_TAG_subprogram` bodies with `DW_AT_low_pc`. This is the source-tree reconstruction's primary evidence. |
| `nm -C` / `readelf -sW --dyn-syms` | The defined-symbol table (24,500 defined in `libnrt.so`) and the 145 `NRT_*` versioned exports. The independent name view that catches `-g`-less TUs DWARF misses. |
| `objdump -d -j .text` | Full `.text` disassembly (1,888,996 lines for `libnrt.so`). The instruction stream from which assert triples and dispatch-table indirections are read. |
| `strings -t x -n 4` | The literal pool with file offsets (644,069 lines). Source `__FILE__` manifests, env-var names, version banners, format strings, Rust panic strings. |
| `addr2line` | DWARF round-trip: maps a `low_pc` back to `file:line`, used to *validate* the CU partition (e.g. `nrt_tensor` → `nrt_tensor.cpp:295`) and to fold inlined bodies to their defining TU. |
| `c++filt` | Itanium demangling of the C++ and RTTI symbols, so the class hierarchy is read by name. |
| **IDA sidecar JSON** | Pre-extracted `*_functions.json` (17,372 functions for `libnrt.so` — including PLT thunks, `.cold` fragments, outlined clones that carry no DWARF subprogram), plus `*_frames.json` (stack-frame `vars[]`), `*_callgraph.json` (in-degree / fan-out), structures, enums, strings, xrefs. The call-graph and heavy-frame census draw from these. |

> **NOTE —** the IDA function count (17,372) and the DWARF subprogram count (8,791) differ by design, not error: the delta is PLT stubs, `.cold` split fragments, and `.constprop`/`.isra`/`.part` clones that an optimizing compiler emits with no DWARF subprogram of their own. The book treats the DWARF set as the *logical* function inventory (what to reimplement) and the IDA delta as *toolchain artifacts* (regenerated automatically). See [Forensics Overview §1.3](forensics/overview.md).

---

## 3. Source-TU attribution

Attributing a function to the source translation unit it came from is what turns an address dump into a reimplementation map. The method has two paths, selected by whether the binary carries DWARF.

### 3.1 The DWARF path (libnrt.so, libnccom.so)

Where DWARF exists, attribution is direct and HIGH. Every first-party CU carries the same `DW_AT_comp_dir` —

```text
/opt/workspace/KaenaRuntime/build/private/develop/almalinux/RelWithDebInfo
```

— and a `DW_AT_name` giving the verbatim relative source path (`tdrv/encd/archs/cayman.c`, `enc/enc.cc`, `nrt/nrt_config.cpp`). Counting `DW_TAG_subprogram` records that carry a `DW_AT_low_pc` inside a CU (deduped by `low_pc`) gives the per-TU function count. This partitions the 331 CUs into **203 first-party `KaenaRuntime` TUs (4,407 fns)** and **122 vendored TUs (4,384 fns)** — Abseil, the Rust `std`/`core`/`alloc` crates, the `KaenaProfilerFormat` protobuf, Libarchive, simdjson, `KaenaDriverLib`, zlib. The partition is cross-checked by `addr2line` round-trips on the public exports and against the 17,372-function IDA inventory. This is the spine of [Source-Tree Reconstruction](front/source-tree.md).

### 3.2 The assert-string path (no-DWARF binaries)

Where DWARF is absent, source attribution falls back to the **path strings the compiler embedded into assert and log call sites**, recovered from the instruction stream. `KaenaRuntime`/`KaenaHal` asserts expand the standard glibc `assert()` macro, which passes `__FILE__`, `__LINE__`, and the stringized condition as **separate operands** at the call site: `RDI` = condition, `RSI` = file, `EDX` = line, `RCX` = `__PRETTY_FUNCTION__`, feeding `__assert_fail`. The file and condition are `lea`-rip pointers into `.rodata`; the line number is a `mov $0xNNN,%edx` immediate. Reading the NUL-terminated string at each `lea` target — sound because the image is identity-mapped, so VMA == file offset — recovers the `(file, line, condition)` triple verbatim.

```c
// the assert call-site shape that yields a (file:line:condition) triple
//   lea    rsi, [rip + FILE_off]      // .rodata "tdrv/exec.c"   ← RSI = __FILE__
//   mov    edx, 0x2FA                 //                          ← EDX = __LINE__ (770)
//   lea    rdi, [rip + COND_off]      // .rodata "state->state == EXEC_STATE_WAIT_CORE"
//   lea    rcx, [rip + FUNC_off]      // .rodata __PRETTY_FUNCTION__
//   call   __assert_fail
```

Run over `libnrt.so` this resolves **5,928 / 5,928** glibc and **283 / 283** AnnapurnaLabs-HAL (`al_assert` → `al_abort_program`) call sites with zero unresolved operands — 3,582 unique `(file,line)` sites that seed the source map directly, and a condition corpus that doubles as an invariant catalogue (enum tag-types, state machines, sizing constants recovered from the `== ENUM` / `< MAX_` shapes). The same path-string principle attributes code in `libncfw.so`, whose embedded format and log strings carry the carrier's internal file names even though it has no DWARF. For the fully-stripped `libnrtucode_extisa.so` the floor is its 30-entry `dlsym` binding table for the API surface and raw disassembly against the carved blobs below it.

> **GOTCHA —** the assert path attributes a `file:line`, not a *function body*. At `-O2` an inlined helper's assert resolves to the *header* it was defined in (`enc/rdh.h`, `tdrv/assert_noexpr.h`), not the `.cc` that inlined it. The book treats a header-attributed assert as evidence the header's logic is present *somewhere* in the citing TU's `.text`, and uses `addr2line` on the enclosing `low_pc` to fold it to the defining TU — never claiming a header is its own translation unit.

---

## 4. The confidence convention

Reverse-engineered claims are only as useful as their honesty about certainty. Every page carries a confidence signal, and the convention is uniform across the book so a reimplementer can read it once.

### 4.1 The four levels

| Level | Meaning | Typical evidence |
|---|---|---|
| **CERTAIN** | The claim is read directly, not inferred. A wire-field decoded byte-by-byte, a constant read from a `mov` immediate, a line of GPL kernel C, an export in `.dynsym`. | GPL `file:line`; verbatim DWARF; a literal read from `.rodata`; a register-loaded immediate. |
| **HIGH** | Recovered from solid binary evidence with a sound but non-trivial inference step. A CU partition, a per-TU fn-count, a struct layout confirmed against multiple sources. | DWARF `DW_AT_name`; `nm` symbol; assert triple cross-checked against the call site. |
| **MEDIUM** | A reasonable inference where one link is judgement, not observation — a regex-classified bucket, a basename-only attribution that could collide, an inherited library body mapped by delta. | basename-only TU cite; KIND classifier bucket; negative-evidence claim. |
| **LOW** | A plausible reading offered explicitly as tentative, flagged inline so it is never mistaken for fact. | a sized-but-not-walked interior; a guessed field role marked `(LOW confidence)`. |

The Confidence column appears in every function map and every table of reverse-engineered claims; in prose, an inferred statement carries an inline `(LOW confidence)` / `(MED)` tag. A page with no confidence signal would read as uniformly authoritative, which is dishonest for reverse-engineered material — so the absence of a tag means CERTAIN-or-HIGH by construction, and anything weaker is marked.

### 4.2 The callout taxonomy

Gaps, traps, and overturned claims are surfaced as blockquote callouts with a bold text marker — never an emoji — so they pull a reimplementer's eye to what the surrounding prose would bury.

| Marker | Use for |
|---|---|
| `> **CORRECTION (tag) —**` | An earlier claim overturned by later analysis, stated **in place** with its provenance tag rather than silently edited (e.g. the `.data` `0x400000`-delta correction in [Forensics Overview](forensics/overview.md), which holds for a different image, not `libnrt.so`). |
| `> **QUIRK —**` | A counter-intuitive fact that bites a reimplementer who assumes the obvious (per-arch code is triplicated, not parameterized; absent DWARF ≠ absent code). |
| `> **GOTCHA —**` | A trap where the naive implementation is silently wrong (the 6.75 MB on-stack `enc_ctx` frame; a header-attributed assert mistaken for a TU). |
| `> **NOTE —**` | An important clarification that is not itself a trap. |

> **CORRECTION (METHOD) —** an early reading of the source-map evidence treated the absence of a protobuf-runtime DWARF CU as absence of the protobuf runtime, and described Abseil as the C++ support runtime "instead". `nm` overturns it: the protobuf runtime **is** statically linked (3,003 `google::protobuf` text symbols), only built `-g`-less. Both runtimes are present; they are not alternatives. This is exactly the [§1](#1-the-evidence-hierarchy) rule in action — the symbol table corrects the DWARF gap, and the correction is recorded in place, not edited away.

---

## 5. Limits — what is certain vs inferred

The book is explicit about its own ceiling. Stated plainly:

- **Certain (read, not reconstructed).** The kernel driver — `neuron.ko` ships as GPL-2.0 C in the DKMS package, so its IOCTL dispatch, DMA-op layer, DHAL v2/v3/v4 vtable bodies, NQ/reset/CRWL logic are *read* at `file:line`, not reverse-engineered. The ELF geometry of all six artifacts (`readelf` columns), the `libnrt.so` DWARF CU partition (203 first-party / 122 vendored), the 145 `NRT_*` exports, the 6,211 assert triples, and every wire format / ISA catalogue / struct layout the book grades **byte-level** in [Extraction Status](reference/extraction-status.md).

- **Inferred (reconstructed, confidence-tagged).** Everything recovered from closed binary bytes carries a confidence tag, never a bare assertion: byte-budget provenance buckets (name-prefix classified, MED on the edges), the libnccom inherited-NCCL bodies (mapped by delta from upstream, HIGH on first-party deltas only), the KIND-bucketed assert classification (MED on bucketing, HIGH on the condition text itself), and any per-arch leaf body sized but not line-walked (marked LOW/MED in place).

- **The no-DWARF ceiling.** Two of the five binaries cap what attribution can recover. `libncfw.so` carries no DWARF — its boundary (get-image entry, embedded blob set, reset prologue) is byte-anchored, but its internal serializer and handler *names* are not recoverable, so its pages are graded surface-to-MED **by construction, not by neglect**. `libnrtucode_extisa.so` is fully stripped — no DWARF and no symtab — and is decoded only from its 30-entry `dlsym` surface and raw disassembly against carved blobs. The ceiling caps internal *naming*; it does not cap byte-level *structural* claims (a blob carved and fingerprinted is byte-level even without a single symbol). The grade floor of a no-DWARF page is therefore "surface on names, byte-level on bytes", and [Extraction Status §5](reference/extraction-status.md#5-the-no-dwarf-ceiling) names that ceiling per page.

- **How gaps feed forward.** A gap is never silently absorbed. Each opaque region is named at its boundary, its inputs and callers pinned, and the un-walked interior routed to the [Phase-3 Deep-Dive Backlog](appendix/deep-dive-backlog.md) — the sequencer-internal Q7 TIE ops, the NCFW Xtensa handler internals, the per-arch SBUF leaf derivations, the `encd` interior math, the 13 template-inlined switch-platform per-event composer TUs that fold into `enc.cc` by address but were never name-cited. "Not-traced" means the boundary is a contract and the interior is a build-against-the-binary task — not a hole the book pretends is filled.

> **NOTE —** the honest gap is the decisive distinction between this book and a decompiler transcript. A marked unknown is correct; a plausible fiction is a landmine for the reimplementer. Where a function was not individually traced, the page says what was done (the range swept, the bodies sized) and what remains — and routes the remainder to the backlog rather than guessing.

## Cross-References

- [Forensics Overview and Heavy-Frame Census](forensics/overview.md) — the DWARF-CU partition, the IDA-vs-DWARF reconciliation, and the byte-budget provenance split this method produces
- [Source-Tree Reconstruction (KaenaRuntime)](front/source-tree.md) — the 331-CU source tree the DWARF attribution path ([§3.1](#31-the-dwarf-path-libnrtso-libnccomso)) reconstructs
- [Extraction Status](reference/extraction-status.md) — the per-page coverage map the confidence convention produces, and the no-DWARF ceiling ([§5](#5-limits--what-is-certain-vs-inferred)) named per binary
- [Phase-3 Deep-Dive Backlog](appendix/deep-dive-backlog.md) — the work queue every named gap routes to
