# ptxas FP-emulation, DWARF debug-info, and SASS text-printing

Reverse-engineering notes for NVIDIA `ptxas` (CUDA 13.0.88, ELF
`/home/grigory/iprit/nvopen-tools/ptxas/ptxas`, 37.74 MB, x86-64, stripped).
All findings are derived from static analysis of the stripped binary
(decompiled C, string/xref tables, raw ELF). Addresses are virtual addresses
in the `ptxas` image unless noted.

This directory documents three under-covered ptxas areas:

1. **FP constant-fold engine** (the "softfloat" question) — `softfloat_*.tsv`
2. **DWARF / debug-info emission** into the cubin — `dwarf_*.tsv`
3. **SASS text printing** (`-v` resource report + self-check SASS text) —
   `sass_*.tsv`

---

## 1. FP constant-fold engine (softfloat) — KEY NEGATIVE FINDING

**ptxas does NOT statically link the Berkeley SoftFloat-3e library, and contains
no FP128 / extF80 / 128-bit-integer software-float routines.**

Evidence (binary, first-hand):

* The canonical SoftFloat-3e reciprocal lookup tables
  `approxRecip_1k0s[]` and `approxRecipSqrt_1k0s[]` (distinctive uint16
  sequences) are **absent** from the entire 37.74 MB ELF (byte-scan miss).
* No `countLeadingZeros8` LUT, no `roundPackToF64/F32` leaf shape, no softfloat
  rounding-mode / exception-flag global, no `__multf3/__addtf3/__divtf3/__fixtfsi`
  compiler-rt soft-float helpers in `.dynsym`.
* The dynamic math imports are exactly the **glibc libm** set:
  `sqrt, pow, floor, ceil, cos, sin, log` (`@GLIBC_2.2.5`). There is **no
  `fma`, no `fesetround`/`fegetround`** import.
* The lone `__float128` string (`0x240bc2a`) has **no code xref** — it lives in
  the C++ Itanium demangler type table (neighbours: `long double`,
  `decimal128`, `unsigned __int128`), not in a softfloat path.

What ptxas actually does for compile-time folding of `.f16/.bf16/.tf32/.f32/.f64`
immediates (add/sub/mul/div/min/max/sqrt/cvt/transcendental approximations):
it uses the **host x86 SSE2 FPU** (`_mm_*_pd/_ss`) plus **glibc libm** kernels,
with manual round-bit twiddling for directed-rounding and narrowing cases.

Native fold-engine inventory (the structural replacement for softfloat):

| addr | role |
|------|------|
| `0x926a30` | constant-fold master dispatch (multi-thousand-line opcode switch) |
| `0x9203a0` | binary-op folder (add/sub/mul/div/min/max .f32/.f64) — native `__m128d` |
| `0x921820` | unary-op folder (sqrt/cos/lg2/ex2/rcp/round/cvt) — libm kernels |
| `0x91b730` | typed-immediate decoder → host `double` (f16 via `pow(2,e-15)`, bf16 `<<16`) |
| `0x91a0f0` | operand-descriptor fetch |
| `0x91ba60` / `0x91cdd0` | folded-result writer / const-pool interner |
| `0x2a12cb0` / `0x2a12d50` | `sqrt@GLIBC` / `pow@GLIBC` PLT thunks (fold math kernels) |

See `softfloat_routines.tsv` and `softfloat_callers.tsv`. **DRIFT**: any wiki
note that assumes a Berkeley-SoftFloat layer in ptxas would be wrong for this
build; the FP-emulation page should describe the native fold engine above.

---

## 2. DWARF / debug-info emission

ptxas consumes the PTX-level DWARF that cicc emits (as `@@DWARF` directives and
PTX `.debug_*` sections), **rewrites every symbolic label reference to a final
SASS address** (emitting relocations where the address is not yet known), and
**re-emits** the bytes into cubin `.debug_*` (and `.nv_debug_*`) sections.

### Orchestrator and flag gating

`0x679DC0` is the master debug-emission entry (the function that builds all
cubin debug sections). Its behaviour is gated by two flags — `deviceDebug` (`-g`
/ `--device-debug`, arg slot `a7`) and `lineInfo` (`-lineinfo` /
`--generate-line-info`, arg slot `a8`):

| Flag | Sections emitted |
|------|------------------|
| `-lineinfo` | `.debug_line`, `.nv_debug_line_sass`, `.nv_debug_ptx_txt`, `.debug_str` |
| `-g` | the above **plus** `.nv_debug_info_reg_sass`, `.nv_debug_info_reg_type`, `.debug_info`, `.debug_abbrev`, `.debug_loc`, `.debug_frame`, `.debug_ranges/pubnames/pubtypes/aranges` |
| `forceDebugFrame \|\| -g` | `.debug_frame` (emitted before the suppress check) |
| `--suppress-debug-info` | early-returns after frame; strips everything else |

### Line-table model (PTX line → SASS pc)

Two line generators feed `0x866BB0` (the line-program writer), called twice by
top-level `0x867880`: pass 0 → `.debug_line`, pass 1 → `.nv_debug_line_sass`
(only when a SASS-line-info pointer is present).

The line-program prologue constants are **DWARF version 2**:
`min_inst_length=1, default_is_stmt=1, line_base=-5, line_range=14,
opcode_base=10`, with the DWARF-2 `standard_opcode_lengths`. Special-opcode
encoding: `op = (advLine - line_base) + (advAddr/min_inst)*line_range +
opcode_base`, with the `advance_line`/`advance_pc`/`copy` fallback when out of
range. For `.nv_debug_line_sass`, each row's PTX address is re-keyed to the
final SASS pc (sass↔merc remap), collapsing duplicate rows except where the
prologue `preserve` flag is set.

Two LEB128 encoders: `0x463C40` (unsigned) and `0x463CA0` (signed).

### Relocation / SASS-address rewrite

Every label reference in the parsed DWARF is resolved by class:
function/label symbols → `R_MERCURY_ABS_PROG_REL32/64` (addend adjusted to SASS
offset) or legacy `R_CUDA_32/64`; CBANK params → direct patch; stack variables
→ deferred `DW_OP_fbreg` SLEB128 frame-offset patches resolved by a lightweight
DWARF decoder; constant-space symbols → `R_MERCURY_G64` / `R_CUDA_G32/G64`
generic-address relocs.

### NV sidecar sections

* `.nv_debug_line_sass` — SASS pc → PTX/source line; its file-table entry
  references `.nv_debug_ptx_txt[.<adler32>]` (adler32 of the PTX input, for
  separate compilation).
* `.nv_debug_info_reg_sass` (`0x8679F0`) — register-location sidecar; which
  physical register holds each variable at each SASS pc. Format: per `.debug_loc`
  entry, `DW_OP_regx` = ULEB128 register-name; `DW_OP_bregx` = ULEB128
  register-name + SLEB128 offset. Data produced by post-RA pass `0x88D870`.
* `.nv_debug_info_reg_type` (`0x867B00`) — register-type sidecar.
* `.nv_debug_ptx_txt` — verbatim PTX source minus DWARF-machinery lines.

### Mercury namespace (SM100+) — NEW vs older model

A full `.nv.merc.debug_*` namespace (15 names) classified by `0x1C98C60`
(switch on ELF `SHT_LOPROC`-range section tags, e.g. `0x70000006`) and writers
`0x1C98C60`/`0x1C9D1F0`. This is the deferred-finalization (capsule/Mercury)
path for SM100+, with `R_MERCURY_*` relocations. The older CUDA-side model has
no equivalent.

### Version gate

The emitter writes DWARF v2. The "Dwarf version %d is not supported"
(`0x22b14e0`) string is a **reader-side** guard in the DWARF-line reader, which
rejects an input DWARF version it cannot parse.

See `dwarf_sections.tsv` (17 sections) and `dwarf_emitters.tsv` (37 functions).

---

## 3. SASS text printing

### `-v` resource-usage report

`0x463710` is the single unified resource-report formatter. It builds each line
into a stringstream buffer (`0x4287D0` create, `0x428F30` sprintf-append,
`0x4289F0` finalize) and flushes through the message-emit core `0x42F590`, which
prepends the `ptxas info :` prefix. Report strings live in a `.rodata`
descriptor array at `0x29FC000` (16-byte stride: `[severity dword][pad][format-ptr]`;
severities 1=plain, 2=info, 3=warning, 5=error, 6=fatal); `0x42F590` writes the
`info/warning/error/fatal` word plus `@I@/@O@/@W@/@E@` channel tags. The info
carrier is `dword_29FC150` (`"%s"`).

Two-block structure matching the canonical output: a **global** line (`gmem`,
then `cmem[N]` over const-bank tags `0x70000004..`, ~18 banks) then a
**per-entry** line (`Compiling entry function '%s' for '%s'`, `Function
properties` with stack frame / spill stores / spill loads [verbose-gated],
`Used %d registers`, `used %d barriers`, `cumulative stack size` [verbose-gated],
`smem`, `cmem[N]`, `lmem`, `textures/surfaces/samplers`), plus a separate
`Compile time = %.3f ms` line. Field order and format strings are in
`sass_listing_fields.tsv`.

Verbosity gate at the call site (`0x446240`, ~`0x447b8a`): `!verbose(+402) is
false && !forceText(+613)` — i.e. **`verboseMode && !forceText`** (the `-v` /
`--verbose` flag). Spill / lmem / stack-limit diagnostics are separate **warning**
descriptors (`dword_29FD320/29FD330`, type 3) gated by the `warn-on-spills`
knob, not part of the info report.

**DRIFT (CUDA-13 additions):** `, used %d barriers` and `Compile time = %.3f ms`
are present in this build's report; both are newer than the field set emitted by
older ptxas builds. Treat barrier count and compile time as standard
modern-ptxas fields.

### SASS disassembly text printer

ptxas can emit human-readable SASS text via the **self-check / out-sass** path,
gated by the `--binary-kind {mercury,capmerc,sass}` option (`0x703AB0`;
`out-sass` → context `+84`, self-check → `+83`, `-forcetext` forces text mode
and suppresses the verbose report). The text builder `0x719D00` is table+vtable
driven (50 KB, zero inline strings); a Flex lexer `0x720F00` re-parses
SASS text for `--self-check`.

The late Mercury pipeline phase-label table at `0x22bd400` orders the relevant
phases: `MercGenerateSassUCode → ReportFinalMemoryUsage → FormatCodeList →
DumpNVuCodeText → DumpNVuCodeHex`. Note `MercGenerateSassUCode` is the SASS
**encoding-generation** phase, **not** the text printer; `DumpNVuCodeText`
(phase 129) is the SASS-text phase and `DumpNVuCodeHex` (130) the hex phase.
The renderer is a **builder/visitor** vtable (~520 slots, region
`0x17F8000–0x181FFFF`), not the older md-driven print path — opcode at
`instr+72` → ROT13 mnemonic table → modifier templates → operand encoder →
predicate/control flags via vtable methods.

Printer data model (consumes the extracted format tables):

| component | function(s) | table consumed |
|-----------|-------------|----------------|
| mnemonic + modifier name resolve | `0x7CB560`, `0x896D50` (1026 refs each) | ROT13 opcode-name region `0x21C1336` (322 primary + 385 Mercury-extended) + modifier-format-string region `0x21C5E00` |
| modifier formatting | `0x7A5D10`, `0x7C5410`, `0xBE7390` (412 refs) | modifier-format-string region `0x21C5E00`–`0x21CEE00` |
| operand encode/print | `0x9D12F0` (64-byte operand struct, 289 callers), `0x91C840` (reg-class), `0x9CEB50` (address-space) | `format_descriptors` (38 entries, slot sizes/types/flags) |
| predicate-guard `@Px` | `0x9DB7E0` | begin/end-predicate-guard vtable slots |
| texture/surface print | `0x18189C0` (vtable) | — |
| ISA-override fixup dispatch | `0x181E1D0` (45 opcodes) | — |

The opcode names are ROT13-obfuscated in the table (e.g. ROT13 `VZNQ` → `IMAD`,
`SSZN` → `FFMA`); the resolver decodes them at print time.

See `sass_printer_fns.tsv` and `sass_listing_fields.tsv`.

---

## Confidence

* Softfloat negative finding: **high** (LUT absence + import set both confirmed
  first-hand on the raw ELF).
* DWARF line-table / LEB / reg-sidecar / gating: **high** (cross-validated
  binary ↔ strings ↔ structure). Misc-section routing (`aranges/macinfo/
  pubtypes`): **medium**.
* SASS `-v` report (`0x463710`): **high**. Disasm-text printer roles: **medium-high**
  (table-reference counts and vtable shapes; some vtable-only functions have no
  static callers).

## Files

* `softfloat_routines.tsv` — fold-engine routine inventory (op → fn)
* `softfloat_callers.tsv` — callers of the fold engine
* `dwarf_sections.tsv` — debug section → emitter / gating flag
* `dwarf_emitters.tsv` — emitter functions (leb128 / line / reg sidecars / reloc)
* `sass_printer_fns.tsv` — SASS printer / report functions by role
* `sass_listing_fields.tsv` — `-v` report fields, format strings, order
