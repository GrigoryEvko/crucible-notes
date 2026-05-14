# .data XOR-3 Obfuscation

## Abstract

The `tileiras` AsmWriter stores its two largest plaintext-string assets — the physical-register-name pool and the PTX opcode-mnemonic pool — as XOR-encoded byte arrays in a writable load segment. The encoding is a simple walking XOR stream (`0, 3, 6, 9, ...`) applied after linking and undone in place at runtime by `pthread_once`-guarded initializers. Once decoded, both pools feed the normal LLVM `AsmWriterEmitter` lookup paths.

The cipher has no cryptographic value. Its only purpose is to keep the strings out of trivial `strings` output. For reimplementation, the clean design is to store the pools as ordinary read-only string tables and delete the runtime decoder.

## XOR-3 obfuscation scheme

Only two pools are encoded: the opcode-mnemonic string pool and the physical-register-name pool. They live in writable memory, so the decoder mutates the bytes directly without needing `mprotect`. No third encoded pool is referenced by the AsmWriter path.

## Mnemonic and register-name pools

The opcode-mnemonic pool decodes to a packed NUL-delimited C-string table with roughly three thousand chunks. The first chunks are AsmWriter separators such as `"},\n\t\t"`, `"},\n\t"`, and `";\n\t"`; later chunks carry PTX mnemonic fragments such as `"match.all.sync.b32 \t"` and `"suld.b.1d_buffer.v2.b8"`.

The shorter register-name pool carries physical-register names. Decoded prefixes include `%Depot`, `%SP`, `%SPL`, `%envreg0..31`, and the PTX register families `%p`, `%rs`, `%r`, `%rd`, `%f`, `%fd`, and `%rq`. Only physical-register names use the pool; virtual PTX register classes are formatted directly from prefix plus register number.

## Decoded once at initialization

Both pools are decoded exactly once per process by `pthread_once`. After the mnemonic pool is decoded, `getMnemonic` performs a second one-shot to cache the pool base pointer behind the Itanium ABI static-local guard protocol.

```c
static pthread_once_t once_reg_name = PTHREAD_ONCE_INIT;
static uint8_t guard_once = 0;
static const char *base_ptr_cache = NULL;
static pthread_once_t once_mnemonic = PTHREAD_ONCE_INIT;
```

No teardown registers. Once decoded, the pools live in writable memory for the lifetime of the process.

## Byte-level transform decoding

Both init helpers implement the same byte-granular walking-XOR cipher:

```c
void xor3_decode(uint8_t *begin, uint8_t *end) {
    uint8_t k = 0;
    while (begin != end) {
        *begin++ ^= k;
        k += 3;                           // wraps mod 256
    }
}
```

The key schedule is `k[i] = (3 * i) mod 256`. Because `gcd(3, 256) = 1`, the schedule visits every byte value once per 256-byte window before repeating. XOR is self-inverse, so running the same pass twice re-encodes the pool.

The transform is in-place, byte-granular, single-pass — no block chaining, no IV, no key derivation, no integrity tag. The encoder is the same function as the decoder.

## AsmWriter consumer

`NVPTXInstPrinter::getMnemonic(const MCInst*)` is the canonical LLVM `AsmWriterEmitter` lookup with NVIDIA's decode/cache steps welded onto the prologue:

```c
const char *get_mnemonic(const MCInst *mi) {
    pthread_once(&once_mnemonic, decode_mnemonic_pool);

    if (!guard_once && __cxa_guard_acquire(&guard_once)) {
        base_ptr_cache = mnemonic_pool;
        __cxa_guard_release(&guard_once);
    }

    uint32_t op = mi->opcode;
    uint32_t lo = mnemonic_offsets[op];
    uint32_t hi = mnemonic_flags[op];
    if (lo | ((uint64_t)hi << 32))
        return base_ptr_cache + (lo & MNEMONIC_OFFSET_MASK) - 1;
    return NULL;
}
```

The per-opcode offset table contains one packed `uint32_t` per MC opcode. Bits 0..16 are the byte offset into the mnemonic pool; bits 17..31 carry AsmWriter tail state. A parallel companion table carries operand flags and modifier-class words. The `(lo | hi << 32) == 0` test distinguishes a real mnemonic from an LLVM generic pseudo without a mnemonic. The `-1` bias is upstream AsmWriterEmitter convention: offset 0 is the no-mnemonic sentinel.

The parallel `printRegName` path decodes the register-name pool once and uses a 16-bit offset table for physical registers. Other register classes format directly from prefix plus register number without consulting the pool.

## Reimplementation Notes

A faithful but cleaner implementation can make both string pools read-only:

```c
const char *get_mnemonic_clean(const MCInst *mi) {
    uint32_t op = mi->opcode;
    uint32_t lo = mnemonic_offsets[op];
    uint32_t hi = mnemonic_flags[op];

    if ((lo | ((uint64_t)hi << 32)) == 0)
        return NULL;

    return mnemonic_pool + (lo & MNEMONIC_OFFSET_MASK) - 1;
}
```

That removes the writable string tables, the two `pthread_once` decoders, and the base-pointer guard while preserving the AsmWriterEmitter lookup contract.
