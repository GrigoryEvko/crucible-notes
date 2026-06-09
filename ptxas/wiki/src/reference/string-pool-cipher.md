# String-Pool Encryption

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0) unless a v13.1 build is named explicitly. Other versions differ.*

ptxas keeps several of its string tables out of plain sight. A `strings` scan of
the binary never shows the PTX-macro expansion templates, the opcode mnemonics,
or the tuning-knob names — yet all of them are present, in the clear, in process
memory at runtime. Three independent obfuscation schemes are responsible, and
the same schemes recur across the sibling tools (`nvlink`, `cicc`, `nvdisasm`).
This page documents the ptxas mechanisms and maps the toolchain-wide picture.

## At a glance

| Concealed data | Where (13.1) | Scheme | Decoder |
|---|---|---|---|
| PTX-macro expansion pool (~1.85 MB) | `.rodata` blob `0x1E5EFC0` | LCG keystream ⊕ S-box ⊕ ciphertext feedback | `sub_430710` |
| Opcode mnemonic table | ctor at startup | ROT13 | `ctor_003` |
| Tuning-knob name table (2,000+) | ctor at startup | ROT13 | `ctor_005` |

The opcode/knob ROT13 tables are covered in [PTX Parser](../pipeline/ptx-parser.md#rot13-opcode-name-obfuscation)
and [Knobs System](../config/knobs.md); this page focuses on the stronger
stream cipher that protects the macro pool.

## The PTX-macro pool stream cipher

The macro pool is the table of printf-style templates ptxas uses to expand
"pseudo-PTX" (compiler-internal opcodes that have no direct hardware
instruction) into legal PTX before SASS codegen — integer division software
sequences, vote/ballot/match synthesis, WMMA fragment choreography, tensormap
field patching, and so on. On disk it is a single high-entropy blob with **zero
direct code references**; the only `lea` that reaches it lands at *blob + 0x100*
(the payload start past a small header), so a naive xref scan finds nothing.

The decryptor is a byte-stream cipher combining three primitives: a Linear
Congruential Generator keystream, a 256-byte substitution box, and
ciphertext-feedback chaining. Init function `sub_4305D0` builds a 16-byte state
object from the 32-bit key `0x5389A4F8`; decryptor `sub_430710` runs the loop:

```c
// state init (sub_4305D0), key = 0x5389A4F8
uint32_t state = key;            // LCG state
uint32_t ks    = 0;              // current keystream word
int      cnt   = 1;              // bytes remaining in ks
uint8_t  prev  = (~key) & 0xFF;  // ciphertext-feedback register (= 0x07)

// decode loop (sub_430710), per ciphertext byte c:
for (each cipher byte c) {
    if (--cnt == 0) {                                 // refill keystream
        state = state * 1103515245u + 12345u;         // glibc rand() LCG
        ks    = state;
        cnt   = 4;                                     // 4 keystream bytes / word
    } else {
        ks >>= 8;                                      // next byte, LSB-first
    }
    uint8_t plain = SBOX[(c ^ prev) & 0xFF] ^ (ks & 0xFF);
    prev = c;                                          // chain on the CIPHER byte
    emit(plain);
}
```

- **Key** `0x5389A4F8`; LCG multiplier `1103515245` (`0x41C64E6D`) and increment
  `12345` (`0x3039`) are the textbook glibc `rand()` constants, four keystream
  bytes consumed per 32-bit word.
- **S-box** is a fixed 256-byte permutation in `.rodata` (13.1 VMA `0x1C5A780`;
  13.0 file offset `0x18E3340`).
- **Feedback** chains on the *ciphertext* byte (CBC-style on input), which is
  why a simple constant-XOR or repeating-key analysis against known plaintext
  fails — and why the on-disk entropy (~7.99 bits/byte) is near-maximal while
  the decoded text is ordinary ASCII.

### When it runs

The pool is decoded **once, whole**, during front-end/macro initialization — not
lazily per template. The loader (inside `sub_451730`, around `0x454daf`)
allocates a heap buffer, `memcpy`s the full encrypted blob into it, stores the
buffer pointer at lexer-context `+0x320`, then makes a single `sub_430710` pass
over the whole buffer. The ~588 pseudo-instruction emitter functions later read
templates straight out of that buffer with `sprintf(out, "%s", &pool[offset])`,
offsets spanning the full pool size. The encrypted `.rodata` copy is left
untouched; only the heap buffer holds plaintext.

### Pool contents

Decoded, the pool is ~1.85 MB of NUL-delimited templates beginning:

```text
{
            %s \membar%s;
            %s cctl.global.invall;
```

Notable families: the IEEE-754 `__cuda_sm20_div_*` integer/float division
recipes (with denormal slow-paths and per-rounding-mode variants), `__cuda_sm70_*`
Volta independent-thread-scheduling vote/ballot/match synthesis, WMMA fragment
load/store choreography, `tensormap.replace` field-patch sequences,
`_tcgen05.guardrails.*` Blackwell tensor-memory bounds checks, and the full
`__cuda_*` reserved-identifier namespace the expander uses for temporaries. The
namespace prefix is still `__ptxMacroFuncsFermi___` — a fossil from the Fermi
era, retained verbatim through Blackwell.

## Toolchain-wide picture

The same concealment family appears across the CUDA compiler tools. Three of the
four use the *identical* LCG ⊕ S-box ⊕ feedback stream cipher; `cicc` uses a
cheaper position-dependent XOR.

| Binary | Concealed data | Scheme |
|---|---|---|
| **ptxas** | PTX-macro pool | LCG ⊕ S-box ⊕ feedback, key `0x5389A4F8` |
| **nvlink** | same PTX-macro pool (content-identical) | same cipher/key — see [nvlink: String-Pool Encryption](https://gh.evko.io/nvopen-tools/nvlink/reference/string-pool-cipher.html) |
| **nvdisasm** | per-arch SASS ISA description tables (~68 MB plaintext) | same stream cipher (per-arch key) wrapping an LZ4 block |
| **cicc** | LLVM/Clang name tables + PTX mnemonics | position-XOR `plain[i] = cipher[i] ^ ((3·i) & 0xFF)` — see [cicc: Encrypted Data Tables](https://gh.evko.io/nvopen-tools/cicc/reference/encrypted-data-tables.html) |

The protection effort tracks reimplementation value: the strong stream cipher
guards exactly the two assets NVIDIA has never published — the PTX→SASS lowering
recipes and the SASS binary encoding — while mere names get ROT13 or a 3·i XOR.

**nvdisasm** is the standout: its `.data` blob is a pack of per-architecture SASS
ISA description tables (one per SM, `sm_75` through `sm_121`). Each table is the
same stream cipher wrapping an LZ4 block; decoded, every instruction is a
`CLASS "<mnemonic>"` record carrying its operand `FORMAT`, the `OPCODES` bit
pattern, an `ENCODING` block placing each field into exact instruction-word bits
(`BITS_<width>_<hi>_<lo>_<name> = …`, some via `TABLES_*` enum→bits maps),
`CONDITIONS` validity rules, and scheduling `PROPERTIES` — in effect a complete
SASS assembler/disassembler specification.

## Provenance

The macro pool was independently located by redplait (CUDA 13.1) while
reverse-engineering the PTX grammar; his [denvdis](https://github.com/redplait/denvdis)
project snapshots the decoded pool from a live process under a debugger. The
cipher itself (key, S-box, LCG schedule, whole-blob decode) was recovered here
from static analysis of the decryptor `sub_430710`, and reproduces the runtime
pool byte-for-byte directly from the on-disk blob.

## Cross-References

- [PTX Parser (Flex + Bison)](../pipeline/ptx-parser.md) — the macro preprocessor and the ROT13 opcode table that consume this pool.
- [Knobs System](../config/knobs.md) — the ROT13 tuning-knob name table (`ctor_005`).
- [Binary Layout](../binary-layout.md) — `.rodata` blob and S-box placement.
