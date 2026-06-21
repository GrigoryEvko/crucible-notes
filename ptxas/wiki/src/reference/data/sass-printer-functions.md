# SASS Printer & `-v` Report Function Map

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

The functions and tables that turn finalized SASS into human-readable text, and the `-v` resource-usage report. Two distinct paths exist: a `-v` report formatter that summarizes resource usage, and a table+vtable-driven disassembly renderer reached through the self-check / out-sass path.

## `-v` resource report

| Address | Role | Evidence |
|---|---|---|
| `0x463710` | Unified resource-report formatter | `"Used %d registers"`, `", used %d barriers"`, `", %lld bytes smem\|lmem\|gmem"`, `", %lld bytes cmem[%d]"`, `"Compile time = %.3f ms"`, `"Compiling entry function"`, `"Function properties for"` |
| `0x446240` | SASS-emit driver | calls `0x463710` at `~0x447b8a`, gated by `!verbose(+402) && !forceText(+613)` |
| `0x42F590` | Message-emit core | writes `info/warning/error/fatal` word + `@I@/@O@/@W@/@E@` channel tags; consumes the `0x29FC000` descriptor array |

The report is built into a stringstream (`0x4287D0` create, `0x428F30` sprintf-append, `0x4289F0` finalize) and flushed through `0x42F590`, which prepends the `ptxas info :` prefix. The descriptor array at `.rodata 0x29FC000` is a 16-byte stride of `[severity dword][pad][format-ptr]`; severities `1=plain, 2=info, 3=warning, 5=error, 6=fatal`.

## `-v` report field order

| Field | Format string |
|---|---|
| gmem | `%lld bytes gmem` |
| cmem (global) | `, %lld bytes cmem[%d]` |
| compiling entry | `Compiling entry function '%s' for '%s'` |
| function properties | `Function properties for %s\n    %d bytes stack frame, %d bytes spill stores, %d bytes spill loads` |
| registers | `Used %d registers` |
| barriers | `, used %d barriers` |
| stack | `, %d bytes cumulative stack size` |
| smem | `, %lld bytes smem` |
| cmem | `, %lld bytes cmem[%d]` |
| lmem | `, %lld bytes lmem` |
| textures | `, %d textures` |
| surfaces | `, %d surfaces` |
| samplers | `, %d samplers` |
| compile time | `Compile time = %.3f ms` |

`, used %d barriers` and `Compile time = %.3f ms` are newer than the field set older ptxas builds emitted; treat both as standard fields in this build. Spill / lmem / stack-limit messages are separate **warning** descriptors (`dword_29FD320/29FD330`, severity 3), gated by the `warn-on-spills` knob, not part of the info report.

## SASS disassembly renderer

| Address | Role | Table consumed |
|---|---|---|
| `0x719D00` | self-check SASS text builder (50 KB, table+vtable driven) | — |
| `0x720F00` | Flex lexer re-parsing SASS text for `--self-check` | — |
| `0x7CB560`, `0x896D50` | mnemonic + modifier name resolve (1026 refs each) | ROT13 opcode-name region `0x21C1336` + modifier-format region `0x21C5E00` |
| `0x7A5D10`, `0x7C5410`, `0xBE7390` | modifier formatting (412 refs each) | modifier-format region `0x21C5E00`–`0x21CEE00` |
| `0x9D12F0` | operand encode/print (64-byte operand struct, 289 callers) | `format_descriptors` (38 entries) |
| `0x91C840` | register-class discriminator (232 callers) | — |
| `0x9CEB50` | address-space qualifier resolver (57 callers) | — |
| `0x9DB7E0` | predicate-guard `@Px` printer (19 callers) | begin/end-predicate-guard vtable slots |
| `0x18189C0` | texture/surface SASS printer (vtable) | — |
| `0x181E1D0` | ISA-override fixup dispatcher (45 opcodes) | — |

The opcode names are ROT13-obfuscated in the table (e.g. ROT13 `VZNQ` → `IMAD`, `SSZN` → `FFMA`); the resolver decodes them at print time. The late Mercury phase-label table at `0x22bd400` orders the relevant phases `MercGenerateSassUCode → ReportFinalMemoryUsage → FormatCodeList → DumpNVuCodeText → DumpNVuCodeHex`. `MercGenerateSassUCode` is the SASS encoding-generation phase, not the text printer; `DumpNVuCodeText` (phase 129) is the text phase and `DumpNVuCodeHex` (130) the hex phase.

## DWARF emitters

| Address | Role |
|---|---|
| `0x679DC0` | debug-emission orchestrator (builds all cubin debug sections) |
| `0x867880` | top-level line-table entry (calls `0x866BB0` twice: `.debug_line`, then `.nv_debug_line_sass`) |
| `0x866BB0` | line-program writer (pass 0 → `.debug_line`, pass 1 → `.nv_debug_line_sass`) |
| `0x8679F0` | `.nv_debug_info_reg_sass` emitter (register-location sidecar) |
| `0x867B00` | `.nv_debug_info_reg_type` emitter (register-type sidecar) |
| `0x88D870` | post-RA liveness annotator producing the reg-location data |
| `0x463C40` | unsigned LEB128 encoder |
| `0x463CA0` | signed LEB128 encoder |
| `0x66F4E0` | main `.debug_info` DIE builder (cicc PTX-DWARF → SASS-addr rewrite + relocations) |
| `0x66A0B0` | DWARF attribute / misc-section emitter |
| `0x1C98C60` | Mercury `.nv.merc.debug_*` classifier (15 names) |

The DWARF-2 line-program prologue constants are `min_inst_length=1, default_is_stmt=1, line_base=-5, line_range=14, opcode_base=10`. Special-opcode encoding: `op = (advLine − line_base) + (advAddr / min_inst) × line_range + opcode_base`, with `advance_line`/`advance_pc`/`copy` fallback when out of range.
