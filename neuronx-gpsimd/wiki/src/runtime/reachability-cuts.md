# Crypto / SQLite / Codec Reachability Cuts

> **What this page is.** Three **negative reachability results**, each one a clean
> cut a reimplementer can act on. The host Neuron runtime ships (or sits beside) a
> pile of general-purpose libraries — OpenSSL, SQLite, liblzma, libbz2, zlib — and
> the natural assumption is that a NEFF *load* exercises them: signature checks,
> attestation, a cache DB, multi-codec decompression. **It does not.** The GPSIMD
> load path touches **exactly one** of those libraries (zlib's `inflate`), and even
> that only on one packager version. The other four are *linked-but-unreached* or
> *not even linked*.
>
> Every verdict below is a **claim + the exact piped command + its count**, re-run
> against the binary while authoring this page. The payoff for a reimplementer is
> spelled out once, up front: **a GPSIMD host can SAFELY OMIT libcrypto, libssl,
> libsqlite3, liblzma and libbz2 and keep only a decompress-only zlib.** Nothing on
> the `nrt_load` path will miss them.

Primary subject — the single 122 MB host runtime ELF that every Part-8 page
dissects:

```
opt/aws/neuron/lib/libnrt.so.2.31.24.0   (SONAME libnrt.so.1)
122,956,336 B ; ELF64 x86-64 ; aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce
BuildID[sha1] 8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e
sha256 956382de73f4cced5d9a0dc040ca82843fb37aef00d5bf2241f343ff02cd59a6
carries .symtab + .debug_info — symbol names below are REAL, not inferred.
```

`BuildID` and byte size were re-read while writing this page (`readelf -n` /
`ls -l`); both match. `[HIGH × OBSERVED]`

See [The libnrt Surface Map (GPSIMD lens)](libnrt-surface.md) for the host link
graph this page cuts down, and the package bibliography page (*Bibliography of
Source Binaries*, `appendix/bibliography-source-binaries.md`) for provenance of
the two packages that supply the binaries here.

---

## 0. The three cuts on one screen `[HIGH × OBSERVED]`

| # | Library family | Verdict (GPSIMD load path) | Mechanism of the negative |
|---|---|---|---|
| 1 | **libcrypto / libssl** (OpenSSL) | **NO cryptography runs.** No NEEDED edge, no crypto dynsym import, zero EVP/SSL/RSA/ECDSA/X509 PLT caller. The only hashing is a *private, unkeyed* MD5/SHA-256 integrity compare. | Not even linked on the host; **self-referential** OpenSSL in the customop SDK. |
| 2 | **liblzma / libz / libbz2** (codecs) | **zlib-only inflate.** A *decompress-only* zlib 1.3.1 is **statically vendored** into `libnrt.so`; lzma and bz2 are absent (zero symbols/strings/edges). One inflate consumer, reached on `pkg_version==2` NEFFs only. | zlib compiled-in; lzma/bz2 never linked or imported. |
| 3 | **libsqlite3** | **ZERO `sqlite3_*` importers.** No cache DB, no manifest DB, no query — on host or device. | **linked-but-unreached**: a vanilla SQLite 3.44.0 SDK bundled in the PyTorch C10 link tree with **no consumer**. |

Two physically distinct packages are in play; keep them apart:

- **SURFACE A — host runtime.** `libnrt.so.2.31.24.0` from
  `aws-neuronx-runtime-lib` (`opt/aws/neuron/lib/`). This is the *actual* `nrt_load`
  path. The "does crypto/codec/sqlite run at NEFF load?" question is answered here.
- **SURFACE B — customop SDK.** `c10/lib/{libcrypto,libssl,libsqlite3,liblzma,libz,
  libbz2}.so` from `aws-neuronx-gpsimd-customop-lib_0.21.2.0` — a PyTorch CustomOp
  build/link staging tree. The "linked-but-unreached" audit is answered here.

Both surfaces come out negative for live crypto and live sqlite; only zlib's
inflate is live, and only on Surface A.

> **NOTE — what "GPSIMD load path" means for these cuts.** The relevant entry is
> the public `nrt_load@@NRT_2.0.0` (`0xa9fe0`) and everything it transitively
> reaches to stage a NEFF onto the device. The reachability claims are *call-graph*
> claims rooted at that entry, not whole-binary "is the symbol anywhere" claims —
> three of these libraries leave *defined* symbols in the ELF that are simply never
> called from `nrt_load`.

---

## 1. Cut #1 — libcrypto / libssl: the load path exercises NO cryptography `[HIGH × OBSERVED]`

**Claim.** No attestation, no signature verification, no certificate check runs at
NEFF load. `libnrt.so` is not linked against OpenSSL at all; the customop SDK's
OpenSSL is consumed only by OpenSSL itself.

### 1.1 The DT_NEEDED set has no crypto edge

The complete dynamic-dependency set of the host runtime — re-run for this page:

```text
$ readelf -d libnrt.so.2.31.24.0 | rg 'NEEDED|SONAME|RUNPATH'
 (NEEDED)   libgcc_s.so.1
 (NEEDED)   libutil.so.1
 (NEEDED)   librt.so.1
 (NEEDED)   libpthread.so.0
 (NEEDED)   libdl.so.2
 (NEEDED)   libstdc++.so.6
 (NEEDED)   libm.so.6
 (NEEDED)   libc.so.6
 (NEEDED)   ld-linux-x86-64.so.2
 (SONAME)   libnrt.so.1
 (RUNPATH)  /opt/aws/neuron/lib
```

**Nine NEEDED entries, all of them the C/C++ runtime.** No `libcrypto`, no
`libssl`. This nine-entry set is the canonical "TIER-1" host dependency list for
`libnrt.so` and is the same set the codec cut (§2) and sqlite cut (§3) start from.

### 1.2 No crypto symbol is imported, statically or dynamically

```text
$ nm -D --undefined-only libnrt.so.2.31.24.0 | rg -c '.'            -> 613   (total UND)
$ nm -D --undefined-only libnrt.so.2.31.24.0 | rg -c 'EVP_|SSL_|SHA|RSA_'  -> 0
```

613 undefined (imported) dynamic symbols; **zero** are crypto. The import-graph
artifact agrees independently — parsing the 613-entry `native_imports.json` list
for any `EVP_/SSL_/RSA_/SHA/sqlite/inflate/...` name yields **0 hits** (see §4).

A whole-symtab scan for *defined* OpenSSL primitives is also empty once
false-positives are removed:

```text
$ nm libnrt.so.2.31.24.0 | rg -c '\bEVP_[A-Za-z]|\bSSL_[A-Za-z]|\bX509_|\bECDSA_|\bRSA_sign|\bRSA_verify|\bHMAC_[A-Za-z]|\bd2i_|\bPEM_'  -> 0
```

> **CORRECTION (grep precision, against DX-RT-16 §4).** A *naïve* scan
> `nm … | rg -c 'EVP_|RSA_|ECDSA|X509|HMAC_'` returns **19** matches on this binary,
> not 0 — but every one of the 19 is a **C++ mangled-name false positive**: the
> Itanium substitution token `…RSA_PSA_E…` inside `absl::btree_iterator<…
> basic_string…>` and `protobuf` `ExtensionSet` symbols, plus `nrt_config_parse`.
> There is **no** real OpenSSL routine. The word-boundary scan above
> (`\bEVP_[A-Za-z]` etc.) is the correct query and returns 0. DX-RT-16's "symbol
> search empty" verdict is right; this records the exact grep that proves it
> without the substring noise. `[HIGH × OBSERVED]`

### 1.3 No dlopen escape hatch into crypto

A library can reach crypto without a NEEDED edge via `dlopen()`/`dlsym()`. Ruled
out — every `.so` name the runtime can `dlopen` is EFA networking, collectives,
firmware, or device-code glue:

```text
$ strings libnrt.so.2.31.24.0 | rg '\.so$' | rg 'lib(fabric|nccom|ncfw|nrtucode|crypto|ssl|sqlite)'
 libfabric.so
 libnccom.so
 libnccom-net.so
 libncfw.so
 libnrtucode_extisa.so
```

`libfabric` (EFA), `libnccom`/`libnccom-net` (collectives), `libncfw` (device
firmware), `libnrtucode_extisa` (the device-code host glue). **No crypto, no
sqlite, no codec** target. The dlopen surface is mechanism-complete and negative.

### 1.4 The only hashing is a private, unkeyed MD5/SHA-256 — integrity, not authenticity

`libnrt` carries a hand-rolled (not OpenSSL EVP) MD5 + SHA-256, defined as local
`t` symbols at the exact addresses below (re-read for this page):

```text
$ nm libnrt.so.2.31.24.0 | rg -w 'sha256_transform|sha256_init|sha256_update|sha256_final|MD5_Init|MD5_Update|MD5_Final|MD5_hash_str'
 5ca680 t sha256_transform   5caa40 t sha256_init    5caa70 t sha256_update   5cab20 t sha256_final
 5ca0e0 t MD5_Init           5ca100 t MD5_Update     5ca300 t MD5_Final       5ca5c0 t MD5_hash_str
```

These are **unkeyed** hashes — anyone can recompute them; they authenticate
nothing. There is no AES/ChaCha/Poly1305/Curve25519/Ed25519/RSA/ECDSA/X509/HMAC
code anywhere in the binary. They run from `nrt_load → … → neff_parse` (§2.4 has
the exact chain) as a **corruption/integrity** compare — digest vs. a value stored
in the NEFF header — and are **gated on a per-load verify flag**: when the flag is
clear, the NEFF is extracted with *no* digest comparison.

> **GOTCHA — "signature" in libnrt is never cryptographic.** The runtime is full
> of `*_signature` symbols (`validate_rg_signature`, `calculate_hostcc_signature`,
> `enc_calculate_signature`, `calculate_src_target_pairs_signature`, …). Every one
> is a **collectives op-config fingerprint** — an MD5 of the replica-group / op
> configuration, compared *between peers* to catch mismatched collectives
> ("failed signature check from peer … mismatched collectives between the peers").
> No key, no asymmetric primitive. A reimplementer should not mistake any of these
> for an authentication routine.

There are no attestation/secure-boot/certificate strings either:

```text
$ strings libnrt.so.2.31.24.0 | rg -ci 'RSA_verify|ECDSA_verify|EVP_DigestVerify|attest|secure[_ ]boot|measured[_ ]boot|sev-snp|nitro'  -> 0
```

> **CORRECTION (grep precision).** A loose scan
> `rg -ci 'x509|certificate|public key|attestation'` returns **5** hits, all of
> which are mangled-name false positives (`…_S_copy_chars…S7_`, protobuf
> `ExtensionSet…S7_`). The narrow token scan above (real verify/attest/boot
> primitives) returns 0. No measured/secure boot, no TPM/Nitro/SEV-SNP attestation
> string exists. `[HIGH × OBSERVED]`

### 1.5 Surface B — the customop SDK OpenSSL is self-referential

The SDK physically ships OpenSSL `1.1.1zb (11 Feb 2025)`:
`c10/lib/{libcrypto.so.1.1, libssl.so.1.1, engines-1.1/{afalg,padlock}.so}`. Who
NEEDs `libcrypto`? — only other parts of OpenSSL:

```text
$ for so in <every .so in the customop tree>; do readelf -d "$so" | rg 'NEEDED.*libcrypto'; done
 afalg.so       NEEDS libcrypto.so.1.1     (OpenSSL engine plug-in)
 padlock.so     NEEDS libcrypto.so.1.1     (OpenSSL engine plug-in)
 libssl.so      NEEDS libcrypto.so.1.1     (OpenSSL itself)
 libssl.so.1.1  NEEDS libcrypto.so.1.1     (OpenSSL itself)
```

**No GPSIMD customop library, ucode loader, driver or executable declares a NEEDED
edge to libssl or libcrypto.** Cross-checking the device-image loaders the customop
runtime actually uses: `libnrtucode.so` DT_NEEDED is `libc.so.6` **only**, and its
UND set has zero crypto symbols (§3.2 re-uses this same query). OpenSSL here is a
transitive PyTorch-bundle packaging artifact — present on disk, dead-code from the
GPSIMD standpoint. `[HIGH × OBSERVED; "why bundled" MED × INFERRED]`

### 1.6 Verdict — Cut #1

**The GPSIMD load-time trust chain does not exercise cryptography.** On the host
runtime, crypto is *not even linked*; on the customop SDK it is *linked-but-
unreached* (self-referential OpenSSL). The only load-time hashing is an unkeyed,
optional MD5/SHA-256 integrity compare — corruption detection, not a cryptographic
trust operation. This is the import-edge + call-graph corroboration of the
"no cryptographic root of trust" finding tracked by the *Firmware Trust Chain +
Threat Model* page (`control/security/trust-chain-threat-model.md`). A reimplementer
can **omit libcrypto and libssl entirely** and reimplement only the private
MD5/SHA-256 (or skip even that, behind the same verify flag). `[HIGH × OBSERVED]`

---

## 2. Cut #2 — codecs: zlib-only inflate, statically vendored `[HIGH × OBSERVED]`

**Claim.** The runtime inflates NEFF payloads with **zlib only**. liblzma and
libbz2 are absent in every sense. A *decompress-only* zlib 1.3.1 is statically
compiled into `libnrt.so`; the single inflate consumer is libarchive's gzip filter,
reached from `nrt_load` only on `pkg_version==2` NEFFs.

### 2.1 No codec import edge — three independent negatives

```text
$ readelf -d libnrt.so.2.31.24.0 | rg 'NEEDED'
   -> 9 NEEDED, NONE of libz/liblzma/libbz2/libarchive   (the same TIER-1 set, §1.1)

$ nm -D --undefined-only libnrt.so.2.31.24.0 | rg -c 'inflate|uncompress|BZ2_|lzma_'   -> 0
$ nm -D --undefined-only libnrt.so.2.31.24.0 | rg -c 'inflate|deflate|lzma_|BZ2_|archive_|uncompress|crc32|adler32|zlib|gz'  -> 0
```

No dynamic codec dependency, no codec UND symbol. The codec is **not reached across
a library boundary** — it is compiled *into* `libnrt.so`.

### 2.2 Static codec inventory — zlib present (inflate-side), lzma/bz2 absent

```text
$ nm libnrt.so.2.31.24.0 | rg -iw 'inflate|inflateInit2_|inflateInit_|inflateEnd|inflateReset|inflate_table|inflate_fast|crc32|adler32|zlibVersion'
 501060 t adler32        501d10 t crc32          5059e0 t zlibVersion
 502260 t inflate        5020d0 t inflateInit2_  5021c0 t inflateInit_
 501f90 t inflateReset   504490 t inflateEnd     504e60 t inflate_table   505a50 t inflate_fast

$ nm libnrt.so.2.31.24.0 | rg -c 'deflate|compress2|gzopen|gzread'   -> 0
$ nm libnrt.so.2.31.24.0 | rg -ci 'lzma_'   -> 0
$ nm libnrt.so.2.31.24.0 | rg -ci 'BZ2_'    -> 0
$ strings libnrt.so.2.31.24.0 | rg 'inflate 1.3.1'
 " inflate 1.3.1 Copyright 1995-2024 Mark Adler "       (NO deflate banner)
```

So the build is **zlib 1.3.1, inflate-only** — no `deflate`/`compress2`/`gz*`
present. lzma and bz2 leave **zero** symbols. The only `lzma`/`bz2`/`xz`-shaped
strings in the binary are the static libarchive **filter-NAME enum labels**
(`ARCHIVE_FILTER_BZIP2`, etc.) baked into every libarchive build — name table, not
code.

> **QUIRK — only the gzip filter is compiled into libarchive.** `libnrt` vendors a
> *minimal* libarchive (329 `archive_*` symbols) with exactly one decompression
> filter registered:
> ```text
> $ nm libnrt.so.2.31.24.0 | rg 'archive_read_support_filter_'   -> only archive_read_support_filter_gzip (0x4d1f40)
> ```
> No `_bzip2`/`_xz`/`_lzma`/`_zstd`/`_lz4`/`_all` filter backend is linked. *This is
> the structural reason* lzma/bz2 are absent: libarchive here can dispatch to
> nothing but the gzip filter, which calls zlib's `inflate`.

### 2.3 The inflate call site — `gzip_filter_read` @ `0x4d1c04`

The codec edge proper lives in libarchive's gzip filter `gzip_filter_read`
(`0x4d1970`). Ranged disassembly (`objdump -d --start-address=0x4d1970
--stop-address=0x4d1e10`), zlib calls in order:

```text
 4d1b5f   call 0x501d10 <crc32>            ; gzip member CRC (trailer integrity)
 4d1b9c   call 0x5020d0 <inflateInit2_>    ; window-bits init for the gzip stream
 4d1c04   call 0x502260 <inflate>          ; <-- THE decompression call site
 4d1c33   call 0x504490 <inflateEnd>       ; teardown
```

Annotated reconstruction of the load-time inflate path (the codec consumer end of
`nrt_load`):

```c
/* gzip_filter_read @ 0x4d1970 — libarchive's gzip read-filter, the SOLE caller of
 * zlib's inflate in libnrt.so. Invoked lazily by the libarchive filter pipeline
 * each time a consumer pulls bytes (archive_read_data / archive_read_next_header)
 * from a stream whose bidder sniffed the 0x1f 0x8b gzip magic. */
static ssize_t gzip_filter_read(struct archive_read_filter *self,
                                const void **buff)
{
    struct private_data *state = self->data;
    /* ... pull compressed input window through libarchive's own buffering ... */
    avail_in = __archive_read_filter_ahead(self->upstream, 1, &read_avail); /* 0x4d1b3a */

    if (state->in_stream == 0) {              /* first call: parse gzip header */
        /* CRC over the just-consumed gzip member header bytes (trailer check) */
        state->crc = crc32(state->crc, p, header_len);              /* 0x4d1b5f */
        inflateInit2_(&state->stream, -15 /*raw,15-bit window*/,    /* 0x4d1b9c */
                      ZLIB_VERSION, sizeof(z_stream));
        state->in_stream = 1;
    }

    state->stream.next_in   = (Bytef *)compressed_window;
    state->stream.avail_in  = read_avail;
    state->stream.next_out  = state->out_block;
    state->stream.avail_out = state->out_block_size;

    ret = inflate(&state->stream, 0 /*Z_NO_FLUSH*/);               /* 0x4d1c04 */
    /* ... map Z_STREAM_END / Z_OK / error, advance CRC over the decompressed bytes,
     *     hand *buff back to the tar walker ... */
    if (ret == Z_STREAM_END /*member done*/)
        inflateEnd(&state->stream);                                /* 0x4d1c33 */
    __archive_read_filter_consume(self->upstream, consumed);       /* 0x4d1c26 */
    return decompressed_len;
}
```

**Uniqueness of the codec edge** (verified by ranged disassembly): `inflate`
(`0x502260`) and `inflateInit2_` (`0x5020d0`) each have **exactly one** caller —
`gzip_filter_read`. There is no `uncompress`/`gz*` high-level wrapper; libarchive
reaches the raw `inflate` API directly. So zlib has a **single consumer**
(`gzip_filter_read`), and that filter has a **single registrant** (`neff_parse`,
§2.4). `[HIGH × OBSERVED]`

### 2.4 The inflate path is reached from `nrt_load` — only on `pkg_version==2`

Every edge below is a concrete `call <target>` re-read from the binary while
authoring this page:

```text
 nrt_load @0xa9fe0  (T, public NRT_2.0.0 API)
   0xaa0a2  call 0xa9920  nrt_load_util       (_Z13nrt_load_utilPKvmiPP9nrt_modelii)
     0xa9b62  call 0xde280  kmgr_load_nn_nc
       0xde4c4  call 0x4ca3f0  neff_parse      (call site #1)
       0xde6ca  call 0x4ca3f0  neff_parse      (call site #2)
```

`neff_parse` has **exactly these two callers** in the whole binary, both inside
`kmgr_load_nn_nc`. Inside `neff_parse` (`0x4ca3f0`), the libarchive driver sequence
(re-read for this page):

```text
 0x4ca58b  call archive_read_new
 0x4ca59d  call archive_read_support_format_tar
 0x4ca5a5  call archive_read_support_filter_gzip   <-- the ONLY caller of this symbol
 0x4ca5b3  call archive_read_open_memory(a, data, data_size)
 0x4ca61c  call archive_read_next_header           ; per-member loop head
 0x4ca756  call archive_read_data_skip             ; "...checksum" side-files
 0x4ca95a  call archive_read_data                  ; <-- triggers gzip_filter_read -> inflate
 0x4ca995  call archive_read_close   /  0x4ca99d  call archive_read_free
```

The gzip filter is **registered up front** (`0x4ca5a5`) but stays **inert** until a
stream actually presents a gzip member: `archive_read_open_memory` installs the
filter chain, `gzip_bidder_bid` sniffs the `0x1f 0x8b` magic, and only then does
`gzip_filter_read` decompress on each `archive_read_data` (`0x4ca95a`) pull. So the
precise inflate site reached from `nrt_load` is:

> `neff_parse:0x4ca95a archive_read_data` → `gzip_filter_read:0x4d1c04 inflate`.

The static edges are OBSERVED; the lazy-pull ordering (registered-but-bids-0 vs.
bids-and-inflates) is INFERRED from the libarchive filter model. `[edges HIGH ×
OBSERVED; pull ordering MED × INFERRED]`

**The `pkg_version` gate** (from the decompiled `neff_parse` body, cross-checked):
- `pkg_version==1` (raw-tar NEFF): the gzip filter is registered but **bids 0**, so
  inflate is **never reached**. Integrity, if the verify flag is set, is SHA-256.
- `pkg_version==2` (gzip-tar NEFF): the inner archive is a gzip stream;
  `gzip_filter_read → inflate` runs per member. Integrity, if verified, is MD5.

So `pkg_version` is the runtime switch that arms or disarms the inflate path. See
the *NEFF Version / Compatibility Model* page (`neff/version-compat.md`) for the
pkg1/pkg2 container model this gates on.

### 2.5 Which NEFF section types are compressed — whole-archive, not per-section

The compression granularity is the **whole inner archive**, not individual
sections:

- The NEFF container is `[1024-B header][data_size bytes of inner archive]`.
- For `pkg_version==2` the **entire** inner POSIX-pax tar (holding `kelf-a.json`,
  `neff.json`, `sgNN/def.json`, `sgNN/{pe,act,dve,sp,pool}.{json,bin,asm}`,
  `ucode_lib.json`, the `*.npy` weight/constant members, …) is a **single gzip
  stream**. `neff_parse` gunzips that one stream and walks the decompressed tar;
  every member is produced by the same `gzip_filter_read → inflate` pipeline.
- There is therefore **no** "section X is compressed, section Y is not": in pkg2
  *all* members ride inside the one gzip stream; in pkg1 *none* do. The large
  constant tensors (`*.npy` weight members) are inside that gzip stream and inflated
  by the same single consumer; the runtime applies **no** second-stage codec to
  weights (no `uncompress`/lzma/bz2 anywhere). The `...checksum` producer side-files
  are `archive_read_data_skip`'d — decompressed then discarded, not a separate
  codec. `[single-gzip-stream facts HIGH × OBSERVED; weight-bin corollary MED × INFERRED]`

### 2.6 Surface B + verdict — Cut #2

The customop SDK separately ships the **full** `libz.so.1.3.1` (deflate+inflate
banners both present — distinct from the inflate-only static copy inside `libnrt`),
plus `liblzma.so.5.4.1` and `libbz2.so.1.0.8`. Their consumer graph is empty:
**nothing** in the customop tree DT_NEEDEDs liblzma, libbz2 or `libz.so`; the
device-runtime wrappers `libnrtucode{,_internal}.so` have no codec NEEDED entry.
They are transitive host-toolchain build deps (their codec internals are DX-IDA-14's
remit).

**Verdict.** The runtime NEFF codec set is exactly `{ zlib-inflate 1.3.1 }`,
wrapped by a tar+gzip-only libarchive, reached on `pkg_version==2` only. A
reimplementer **keeps a decompress-only zlib (inflate side) and omits liblzma,
libbz2, and the deflate/compress half of zlib** — none is on the load path.
`[HIGH × OBSERVED]`

---

## 3. Cut #3 — libsqlite3: ZERO importers, linked-but-unreached `[HIGH × OBSERVED]`

**Claim.** No `sqlite3_*` importer exists from the GPSIMD runtime path — host or
device. The runtime opens no DB, prepares no statement, runs no query. The bundled
`libsqlite3` is a vanilla SQLite 3.44.0 SDK with **no consumer**.

### 3.1 No sqlite import edge — host runtime

```text
$ nm -D --undefined-only libnrt.so.2.31.24.0 | rg -c 'sqlite3_'   -> 0
$ nm        libnrt.so.2.31.24.0 | rg -ci 'sqlite3_'                -> 0   (nothing DEFINED either)
```

The import-graph artifact agrees: the 613-entry `native_imports.json` list has
**0** `sqlite`-substring names (§4). `libnrt` neither imports nor defines a single
sqlite symbol, and `libsqlite3` is **not** in its DT_NEEDED set (the nine-entry
TIER-1 list, §1.1). `[HIGH × OBSERVED]`

### 3.2 No sqlite import edge — device-runtime wrappers (customop SDK)

The GPSIMD device-code runtime wrappers in the customop package:

```text
$ readelf -d libnrtucode.so | rg 'NEEDED'                          -> libc.so.6   (only)
$ nm -D --undefined-only libnrtucode.so | rg -c 'sqlite3_|EVP_|SSL_|inflate|lzma_|BZ2_|dlopen|dlsym'  -> 0
```

`libnrtucode.so` (and `libnrtucode_internal.so`) DT_NEEDED is `libc.so.6` **only**;
the complete UND set is ~18 glibc symbols + 3 toolchain weak symbols + two
`SUNDA_Q7_POOL_RELEASE_EXTISA_0_{JSON,SO}_get` device ISA-pool getters. **Zero**
`sqlite3_*` imports, **zero** dlopen/dlsym primitive, **zero** `"sqlite"`/`".db"`
string. The negative is mechanism-complete: static link, dynamic link, and
dlopen-by-name are all excluded. `[HIGH × OBSERVED]`

### 3.3 What `libsqlite3` actually is, and why nothing reaches it

```text
$ <c10/lib>: libsqlite3.{so,so.0,so.0.8.6,a,la} + ../include/sqlite3{,ext}.h + pkgconfig/sqlite3.pc
$ strings libsqlite3.so.0.8.6 | rg '^3\.[0-9]'   -> 3.44.0
```

It is a **complete link SDK** (shared + static + headers + pkg-config + the package's
only `.debug_info` compile unit, `sqlite3.c`), bundled into the PyTorch C10 link
tree alongside `libc10.a` and the usual third-party set
(`crypto/ssl/lzma/z/bz2/ffi/sqlite3`). Its own NEEDED is the stock distro set
(`-lm -ldl -lpthread`). The customop build driver `script/build_custom_op.py` links
`c10/lib/libc10.a` but **names neither sqlite nor crypto/ssl/lzma/z/bz2/ffi** — so
sqlite is not even pulled into the on-device custom-op image. Even `libc10.a`
itself has 0 sqlite call sites; the only `sqlite3_open/exec/prepare` *name* hits in
the whole tree are the **definitions inside `libsqlite3` itself**.

> **NOTE — scope-correction carried from DX-RT-18.** The DX-RT-18 brief named the
> sqlite subject as "libnrt.so (122 MB, debug_info)", but that binary lives in the
> *runtime-lib* package, not the customop SDK corpus where the sqlite SDK ships;
> and the cited "debug_info" actually belongs to **`libsqlite3.so`**, the only
> c10/lib payload carrying `.debug_*` sections. Both reachability halves —
> device-runtime wrappers (OBSERVED here) and host `libnrt` (§3.1, OBSERVED here;
> cross-referenced to the libnrt surface map) — are nonetheless **negative**.

### 3.4 Verdict — Cut #3

**linked-but-unreached. No runtime DB on the GPSIMD path.** sqlite contributes
zero to runtime behaviour: no cache DB, no manifest DB, no host or device DB on the
execution path. It is a PyTorch build-SDK passenger. A reimplementer **omits
libsqlite3 entirely.** `[HIGH × OBSERVED]`

---

## 4. The import graph as a single cross-cut `[HIGH × OBSERVED]`

All three negatives fall out of one artifact in one pass — the libnrt
`native_imports.json` (the `.dynsym` UND table, 613 entries):

```text
$ python3 -c "import json,re; d=json.load(open('<libnrt>_native_imports.json'));
    names=[v['name'] for v in d];
    pat=re.compile(r'inflate|lzma|bz2|zlib|archive|gz|uncompress|crc32|adler32|EVP_|SSL_|SHA|RSA_|sqlite3_|deflate', re.I);
    print('total', len(names), 'codec/crypto/sqlite hits', sum(1 for n in names if pat.search(n)))"
 total 613 codec/crypto/sqlite hits 0
```

**613 imports, 0 codec/crypto/sqlite names.** Every versioned import is a
`GLIBC_*`/`GLIBCXX_*`/`CXXABI_*`/`GCC_*` C/C++ runtime symbol. This single query is
the import-graph proof for all three cuts simultaneously, and it agrees byte-for-byte
with the `nm -D --undefined-only` counts in §§1–3 (the JSON *is* `readelf -Ws` of
the same `.dynsym`). `[HIGH × OBSERVED]`

---

## 5. Adversarial self-verification

The five strongest claims on this page, each re-challenged against the binary:

| # | Claim | Challenge | Result |
|---|---|---|---|
| 1 | **No crypto on the load path.** | "Substring scan `EVP_\|RSA_\|SHA\|X509` returns 19 symtab hits — is that real crypto?" | **Survives.** All 19 are C++ mangled-name false positives (`…RSA_PSA_E…` in `absl`/`protobuf`/`basic_string`); the word-boundary scan `\bEVP_[A-Za-z]\|\bX509_\|\bECDSA_\|\bRSA_verify` returns **0**, and there is no crypto NEEDED/dynsym/dlopen edge. CORRECTION recorded (§1.2). |
| 2 | **zlib-only inflate; lzma/bz2 absent.** | "Is some codec reached statically or via a non-gzip filter?" | **Survives.** `nm \| rg -ci 'lzma_'`=0, `'BZ2_'`=0; only `archive_read_support_filter_gzip` is registered; inflate's single caller is `gzip_filter_read@0x4d1c04`. Decompress-only (`deflate\|compress2\|gz*`=0). |
| 3 | **ZERO sqlite importers.** | "Does any binary in either package import `sqlite3_*`?" | **Survives.** `nm -D … \| rg -c 'sqlite3_'`=0 on `libnrt` and `libnrtucode{,_internal}`; the only `sqlite3_*` *name* hits anywhere are the definitions inside `libsqlite3` itself; no consumer NEEDEDs it. |
| 4 | **The inflate call site is `gzip_filter_read:0x4d1c04`, reached from `nrt_load`.** | "Re-disassemble the range; is `0x4d1c04` really `call <inflate@0x502260>`, and is the chain to `nrt_load` real?" | **Survives.** Ranged `objdump` shows `4d1c04: call 502260 <inflate>` (and `4d1b9c inflateInit2_`, `4d1c33 inflateEnd`); chain `nrt_load@0xa9fe0 →(0xaa0a2) nrt_load_util →(0xa9b62) kmgr_load_nn_nc →(0xde4c4/0xde6ca) neff_parse →(0x4ca5a5) support_filter_gzip` all re-read byte-exact. |
| 5 | **DT_NEEDED = 9 C/C++ runtime libs, no crypto/sqlite/codec.** | "Re-read `readelf -d`; is the set really crypto/sqlite/codec-free?" | **Survives.** Nine NEEDED (`libgcc_s/libutil/librt/libpthread/libdl/libstdc++/libm/libc/ld-linux`), SONAME `libnrt.so.1`, RUNPATH `/opt/aws/neuron/lib`. No `libcrypto/libssl/libsqlite3/libz/liblzma/libbz2/libarchive`. |

Two grep-precision **CORRECTIONs** are embedded in §1.2 and §1.4: the loose
crypto/cert substring scans return non-zero only on C++ ABI mangled names, never on
real OpenSSL routines; the word-boundary queries confirm the clean negative.

---

## 6. The reimplementer's takeaway `[HIGH × OBSERVED]`

A GPSIMD host that reimplements the `nrt_load → … → neff_parse` NEFF-staging path
needs, from this entire family of general-purpose libraries, **only a
decompress-only zlib (inflate side)** — and even that only to handle
`pkg_version==2` (gzip-tar) NEFFs; `pkg_version==1` (raw-tar) NEFFs touch no codec
at all. It can **safely omit libcrypto, libssl, libsqlite3, liblzma and libbz2**:

- **No crypto** — no signature/attestation/certificate verification runs at load;
  the only hashing is an *optional, unkeyed* MD5/SHA-256 integrity compare, gated on
  a per-load verify flag.
- **No second codec** — lzma/bz2 are never linked or called; the whole inner
  archive (including weight `*.npy` members) is one gzip stream or none.
- **No database** — sqlite opens nothing; there is no cache/manifest DB on the
  host or device path.

Cross-references: [The libnrt Surface Map (GPSIMD lens)](libnrt-surface.md) (the
host link graph these cuts trim); the *NEFF Version / Compatibility Model*
(`neff/version-compat.md`, the pkg1/pkg2 model that arms the inflate path); the
*Firmware Trust Chain + Threat Model* (`control/security/trust-chain-threat-model.md`,
the "no crypto root of trust from the GPSIMD path" position these import-edge cuts
substantiate); and the *Bibliography of Source Binaries*
(`appendix/bibliography-source-binaries.md`, provenance of the runtime-lib and
customop-lib packages).

---

## 7. Confidence & limitations

- **Codec identity (zlib only; lzma/bz2 absent), no crypto edge, no sqlite edge:**
  `HIGH × OBSERVED` — each rests on three independent negatives (DT_NEEDED,
  `.dynsym` UND, defined symtab) plus the import-graph JSON, all agreeing.
- **`nrt_load → … → neff_parse → gzip_filter_read → inflate` chain:** `HIGH ×
  OBSERVED` — every edge is a concrete `call <target>` re-read from the binary; the
  two `neff_parse` callers and inflate's single caller were enumerated by ranged
  disassembly.
- **Lazy-pull ordering** (filter inert until the gzip member is sniffed) and the
  **weight-bin "single gzip stream" corollary:** `MED × INFERRED` from the
  libarchive filter model + observed vtable wiring; static analysis sees registered
  edges, not the runtime pull order.
- **"Why OpenSSL/sqlite are bundled" (PyTorch third-party build deps):** `MED ×
  INFERRED` from the c10/lib layout, not a decompiled provenance record.
- **Default state of the verify flag** across all `nrt_load` callers is out of scope
  here (it is a runtime-selectable boolean written at config-init sites).
- **Method:** white-hat static analysis only (no execution). All facts read as
  derived from the shipped `libnrt.so.2.31.24.0` and customop-SDK artifacts; no
  external source tree was consulted or quoted.
