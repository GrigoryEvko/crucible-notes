# String-Pool Encryption

> *Addresses apply to the CUDA 13.1 `nvlink` (PIE). The 13.0 build differs.*

Because nvlink embeds ~95% of ptxas (see [Embedded ptxas](../ptxas/overview.md)),
it also embeds ptxas's encrypted **PTX-macro expansion pool** — the table of
printf-style templates that lower compiler-internal pseudo-PTX into legal PTX.
The decoded pool is content-identical to the ptxas one: the same
pseudo-instruction lowering recipes, vote/ballot synthesis, WMMA choreography,
tensormap patching, and `__cuda_*` reserved-identifier namespace. The
implication is that nvlink carries a **full pseudo-PTX → PTX lowering engine**,
not merely a cubin stitcher — it can re-expand PTX macros at link time.

## The blob and its decoder

| Item | Location (13.1) |
|---|---|
| Encrypted pool | `.rodata` blob base VMA `0x1F5B580`, size 2,969,600 B |
| Only code reference | `lea` to *base + 0x100* inside the loader at `0x13F04C0`–`0x13F0603` |
| Runtime pool size | `.data` global `0x29D5A34` (observed `0x1C2648` = 1,844,808 B decoded) |
| Cipher-context init | `0x225DA0` (seed `0x5389A4F8`) |
| Decryptor | `0x225F20` |
| S-box (256 B) | `.rodata` `0x1CB4A20` |

The loader at `0x13F05BF` allocates the runtime buffer, `0x13F05DD` `memcpy`s the
encrypted blob from *base + 0x100*, `0x13F05E5` installs the buffer pointer at
the object field `this+0x248`, and `0x13F05F6` calls the decryptor over the whole
buffer. The `.rodata` copy stays encrypted; only the heap buffer is plaintext —
which is why the macro strings never appear in a `strings` dump.

## The cipher

Identical to the ptxas scheme — an LCG keystream XORed with a 256-byte S-box
substitution, chained on the ciphertext byte:

```c
uint32_t state = 0x5389A4F8;      // key / seed
uint32_t ks    = 0;
int      cnt   = 1;
uint8_t  prev  = (~0x5389A4F8) & 0xFF;
for (each cipher byte c) {
    if (--cnt == 0) { state = state*0x41C64E6D + 0x3039; ks = state; cnt = 4; }
    else            { ks >>= 8; }
    uint8_t plain = SBOX[(c ^ prev) & 0xFF] ^ (ks & 0xFF);
    prev = c;
    emit(plain);
}
```

The multiplier `0x41C64E6D` / increment `0x3039` are the glibc `rand()` LCG
constants; four keystream bytes are consumed per 32-bit word. Reversing the
cipher reproduces clean PTX plaintext (`{\n  …%s \membar%s; …`) directly from the
on-disk blob — no live snapshot required.

The full mechanics, pool contents, and the toolchain-wide cipher map are
documented on the ptxas side:
[ptxas: String-Pool Encryption](https://gh.evko.io/crucible-notes/ptxas/reference/string-pool-cipher.html).

## Cross-References

- [Embedded ptxas — Architecture Overview](../ptxas/overview.md) — why nvlink carries the ptxas back-end.
- [ROT13-Encoded Pass Names](rot13-passes.md) — the lighter obfuscation nvlink applies to its pass-name table.
