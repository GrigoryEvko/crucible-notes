# Encrypted Data Tables

> *Addresses apply to the CUDA 13.1 `cicc` (non-PIE, link base 0). Other versions differ.*

cicc keeps three of its large `.data` string tables obfuscated: a `strings` scan
shows none of the LLVM intrinsic names, Clang builtin names, or PTX mnemonics it
emits, yet all are present in the clear at runtime. Unlike the strong stream
cipher ptxas/nvlink/nvdisasm use on their pools, cicc uses a cheap
position-dependent XOR, decoded in place by a single shared routine.

## The cipher

One decode routine at VMA `0xE5A5B0` is tail-called from several string-access
sites. It applies a position-keyed XOR over a `[base, base+len)` region, in
place:

```c
// decoder at 0xE5A5B0
void decode(uint8_t *p, size_t len) {
    uint32_t key = 0;
    for (size_t i = 0; i < len; i++) {
        p[i] ^= (key & 0xFF);
        key = (key + 3) & 0xFF;     // key advances by 3 each byte, mod 256
    }
}
// equivalently: plain[i] = cipher[i] ^ ((3 * i) & 0xFF)
```

Recovery is trivial and exact — XOR the on-disk bytes with the `(3·i) & 0xFF`
keystream. For example the on-disk run `6c 6f 70 64 22 61 64 63` XORed with
`00 03 06 09 0c 0f 12 15` yields `llvm.nvv`. The transform is fully reversible
and re-encoding the decoded output reproduces the on-disk bytes byte-for-byte.

Decoding is **lazy**, not a static constructor: the decode routine and the table
base-load sites get zero hits on `cicc --version`; the tables are deciphered on
demand only when intrinsic names are first requested during NVVM-IR → PTX
compilation.

## The three tables

| Table | Base VMA | Size | Contents |
|---|---|---|---|
| Blob A | `0x4FA0780` | 708,608 B | LLVM `Intrinsics.inc` name table — 25,063 strings |
| Blob B | `0x506F4E0` | 344,064 B | Clang `Builtins` table — 12,506 strings |
| Blob C | `0x50D16A0` | 104,560 B | cicc's PTX-emission mnemonic dictionary |

Each base is loaded by a single `lea` immediately before the tail-call to
`0xE5A5B0` (Blob A's loader is at `0xCE63D0`, inside the intrinsic-name lookup;
the `0x60`-byte gap before the entropy-scan's `0x4FA07E0` is a header).

### Blob A / B — bundled-LLVM name tables

Blobs A and B are the static name pools of cicc's bundled LLVM/Clang. They carry
the *all-targets* intrinsic set, not just NVPTX — 18 foreign back-ends appear
(`llvm.x86.*` 1,713, `llvm.hexagon.*` 2,051, `llvm.amdgcn.*` 1,271,
`llvm.loongarch.*` 1,517, the NEC `llvm.ve.*` 1,263, and more). This is a
TableGen artifact: `Intrinsics.inc` emits one target-unconditional array, and
NVIDIA ships it verbatim, so the encryption sweeps up entirely public data
alongside the NVVM names. The recent-LLVM entries (`llvm.spv.*`, the full
`llvm.dx.*` DirectX set, complete `llvm.ptrauth.*`) plus the `llvm-mc (based on
LLVM 21.0.0)` build string pin cicc's device back-end to a downstream **LLVM 21**.

Fourteen `llvm.nvvm.internal.*` names are NVIDIA-private downstream additions
with no public NVVM documentation — among them `internal.ld.l2desc` /
`internal.st.l2desc` (L2 cache-descriptor loads/stores),
`internal.clusterlaunchcontrol.try_cancel`, and `internal.printf.cl`.

### Blob C — the PTX mnemonic dictionary

Blob C holds cicc's PTX-emission spellings as bare mnemonic stems (no printf
substitution slots, unlike the ptxas macro pool): `tcgen05.*`, `wgmma.*`, the
`cp.async.bulk.*` / TMA family, `clusterlaunchcontrol.*`, `mapa`, `getctarank`,
and integer-emulation `mad.lo.cc`/`madc.hi.cc` chains. The division of labour is
clean: cicc owns *what the legal PTX spelling of an operation is*; ptxas owns
*the multi-instruction expansion that implements it* (the encrypted macro pool).

## Toolchain context

cicc's position-XOR is the weakest of the four CUDA-toolchain obfuscation
schemes; ptxas, nvlink, and nvdisasm use a stronger LCG ⊕ S-box ⊕ feedback
stream cipher on the data that would actually enable reimplementation (the
PTX→SASS lowering recipes and the SASS binary encoding). The full cipher map is
on the ptxas side:
[ptxas: String-Pool Encryption](https://gh.evko.io/nvopen-tools/ptxas/reference/string-pool-cipher.html).

## Cross-References

- [NVVM Intrinsic Lowering](../passes/nvvm-intrinsic-lowering.md) — the consumer of the Blob A intrinsic-name table.
- [NVPTX Machine Opcodes](nvptx-opcodes.md) — the PTX opcodes cicc emits via Blob C.
