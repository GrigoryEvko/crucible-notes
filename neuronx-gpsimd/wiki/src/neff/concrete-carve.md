# A Concrete NEFF, Carved Byte-by-Byte

The [NEFF format reference](./format-reference.md) and the
[loader-side container byte format](./container-byte-format.md) describe the format
*abstractly*. This page is the **instance**: it walks the **one real NEFF container
that exists in the corpus** — a complete, valid `pkg2` model baked into the runtime
library `libnrt.so` as a `.data` constant — reading **every header field, every tar
member, every sequencer slot out of the actual shipped bytes**. Where the reference
states a fact ("the `pkg2` digest is an MD5"), this page pins it to the byte (MD5
over *which* bytes, recomputed and matched). Each hex slice below is followed by its
field decode and meaning; a reimplementer can diff their own writer against this
ground truth.

The [`pe.bin` / `pool.bin` microcode *decode* itself](./seq-microcode.md) is **not
re-derived here** — that is the job of the per-engine sequencer page. This page
carves the **container framing around it**: where those streams sit in the tar, how
the 64-byte slot header frames the bytes that page decodes, and how the producer
`.asm` shipped beside each `.bin` cross-validates the decode to the field.

> **Binary under analysis `[HIGH × OBSERVED]`.**
> `neuronx-runtime/extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so.2.31.24.0`
> — 122 956 336 B (`0x7542A30`), `sha256 956382de…02cd59a6`. ELF64 LE `DYN`, x86-64.
> The embedded NEFF begins at **file offset `0xC07E20`**. Every byte quoted below was
> read with `xxd -s <off>` / `dd` directly out of this file and independently
> re-inflated/decoded in Python; nothing is paraphrased from any external tree.

---

## 0. Is there a real `.neff`? — yes, exactly one, and it is embedded `[HIGH × OBSERVED]`

No file named `*.neff` exists anywhere in the corpus (`fd --no-ignore -e neff` → 0
hits; the only `neff`-matching paths are the `DX-NEFF-*` reports and this wiki). The
**concrete NEFF that does exist is embedded**: a complete `pkg2` NEFF is compiled into
`libnrt.so` as a `.data` constant — a built-in default/test model. Scanning the whole
`.data` window (`0xc07e00..0xc37c20`) for the gzip magic `1f 8b 08` returns a
**single** hit at `0xc08220`. There is no second container and no ambiguity.

> **GOTCHA — `.data` VMA == file offset for this binary, so `0xC07E20` is a direct
> seek.** `readelf -SW` reports section `[30] .data PROGBITS VMA 0x0000000000c07e00
> fileoff 0xc07e00 size 0x02fe20` — **delta 0**. Unlike `libtpu.so` (`.data` delta
> `0x400000`) or the GPSIMD config DLLs (delta `0x200000`), here `.text`, `.rodata`
> **and** `.data` all satisfy `VMA == file_offset`. `xxd -s 0xC07E20` lands on the
> real struct with no correction. `[HIGH × OBSERVED]`

The honest limit: this is a **minimal toy model** — one all-reduce of a 32-element
fp32 gradient, **no constant weights, no custom-op ucode library**. So the
weight-payload and ucode-lib sections of the format are *absent from this instance*
(§7); everything that is present is carved to the byte.

### Two-level layout as it sits in `.data`

```text
file 0xC07E20  +0x000   1024-byte neff_header_t                     (§1)
file 0xC08220  +0x400   gzip member, data_size = 0x6AD (1709 B)     (§2)
file 0xC088CD  +0xAAD   end of NEFF  (0xC08220 + 0x6AD)
```

---

## 1. The 1024-byte header, byte-exact `[HIGH × OBSERVED]`

Raw dump (`xxd -s 0xC07E20 -l 256 libnrt.so`, non-zero regions; bytes `0xC07E70..0xC07EC8`
are all zero — the unused tail of the 128-byte `build_version` field):

```text
00c07e20: 0200 0000 0000 0000  0004 0000 0000 0000   pkg_version=2 ; header_size=0x400
00c07e30: ad06 0000 0000 0000  0200 0000 0000 0000   data_size=0x6AD ; ver_major=2
00c07e40: 0000 0000 0000 0000  322e 302e 3231 3838   ver_minor=0 ; build "2.0.2188
00c07e50: 342e 3025 6b61 656e  612d 746f 6f6c 732f    4.0%kaena-tools/
00c07e60: 6465 7665 6c6f 7040  3663 3636 6634 6200    develop@6c66f4b\0
   …zero to 0xC07EC8…
00c07ec8: ........ 0100 0000   61b3 cab2               +0xA8 unused_0=1 ; +0xAC hash…
00c07ed0: 4369 b326 a2b3 ee40  a744 c746 3030 3030    hash[0..15]=MD5 ; "0000…
00c07ee0: 3030 3030 3030 3030  3030 3030 5618 26fb    …0000" (hash[16..31]) ; +0xCC uuid…
00c07ef0: 125f 4bb8 991c 29e0  85c8 949a 7800 0000    uuid ; +0xDC name="x"\0
   …
00c07ffc: 0100 0000                                   +0x1DC requested_tpb_count=1
00c08040: 0000 0000 0000 0000  0100 0000               +0x220 feature_bits=0 ; +0x228 vnc_size=1
```

### Field-by-field decode

The header is a packed C struct; offsets are relative to `0xC07E20` and were
re-confirmed this pass by a Python `struct.unpack_from` decode (results in the table):

```c
struct neff_header_t {            // 1024 B (== header_size)
  uint64_t pkg_version;           // +0x000
  uint64_t header_size;           // +0x008
  uint64_t data_size;             // +0x010   compressed inner-archive byte count
  uint64_t neff_version_major;    // +0x018
  uint64_t neff_version_minor;    // +0x020
  char     build_version[128];    // +0x028   ASCII label, never parsed by the loader
  /* … zero gap … */
  uint32_t unused_0;              // +0x0A8   producer "data is gzip" flag
  uint8_t  hash[32];              // +0x0AC   pkg2: MD5 in [0..15]; pkg1: full SHA-256
  uint8_t  uuid[16];              // +0x0CC   RFC-4122 model UUID
  char     name[256];             // +0x0DC
  uint32_t requested_tpb_count;   // +0x1DC
  uint8_t  tpb_per_node[64];      // +0x1E0   all 0 here
  uint64_t feature_bits;          // +0x220   forward-compat trapdoor mask
  uint32_t vnc_size;              // +0x228   virtual-NeuronCore size
  /* pad to +0x400 */
};
```

| struct off | bytes (LE)                | field                 | decoded value |
|---|---|---|---|
| `+0x000` | `02 00 00 00 00 00 00 00` | `pkg_version` (u64)   | **2** — gzip-tar + MD5 |
| `+0x008` | `00 04 00 00 00 00 00 00` | `header_size` (u64)   | **`0x400`** (1024) |
| `+0x010` | `ad 06 00 00 00 00 00 00` | `data_size` (u64)     | **`0x6AD`** (1709) |
| `+0x018` | `02 00 00 00 00 00 00 00` | `neff_version_major`  | **2** (`> 2` ⇒ refuse; passes) |
| `+0x020` | `00 …`                    | `neff_version_minor`  | 0 |
| `+0x028` | `"2.0.21884.0%kaena-tools/develop@6c66f4b\0"` | `build_version[128]` | producer label, parse-free |
| `+0x0A8` | `01 00 00 00`             | `unused_0` (u32)      | **1** — producer gzip flag |
| `+0x0AC` | `61 b3 ca b2 43 69 b3 26 a2 b3 ee 40 a7 44 c7 46` | `hash[0..15]` | **MD5** `61b3cab2 4369b326 a2b3ee40 a744c746` |
| `+0x0BC` | `30 30 …`                 | `hash[16..31]`        | ASCII **`"0000000000000000"`** (see QUIRK) |
| `+0x0CC` | `56 18 26 fb 12 5f 4b b8 99 1c 29 e0 85 c8 94 9a` | `uuid[16]` | `561826fb-125f-4bb8-991c-29e085c8949a` |
| `+0x0DC` | `78 00 …`                 | `name[256]`           | **`"x"`** |
| `+0x1DC` | `01 00 00 00`             | `requested_tpb_count` | **1** (single core) |
| `+0x1E0` | `00 × 64`                 | `tpb_per_node[64]`    | all 0 |
| `+0x220` | `00 00 00 00 00 00 00 00` | `feature_bits` (u64)  | **0** (no fwd features) |
| `+0x228` | `01 00 00 00`             | `vnc_size` (u32)      | **1** (single NeuronCore) |
| `+0x400` | gzip member               | `data[data_size]`     | §2 |

> **QUIRK — `hash[16..31]` is the ASCII string `"0000…"`, not zero padding.** The
> `pkg2` integrity tag is a 16-byte MD5 living in `hash[0..15]`. The producer fills the
> 16 trailing bytes with the **ASCII digit `'0'` (`0x30`)**, not `0x00`. A reimplementer
> reading a `pkg2` hash must take **exactly `hash[0..15]`** and ignore the `0x30` tail
> — comparing the full 32 bytes against a zero-extended MD5 would fail. `[HIGH × OBSERVED]`

> **NOTE — there is no literal magic number.** Identity is *structural*: `pkg_version ∈
> {1,2}`, `header_size == 0x400`, `data_size ≤ file_size − 0x400`, `neff_version_major
> ≤ 2`, and a gzip stream at `+0x400`. `unused_0 == 1` correlates with `pkg2` but the
> loader gates on `pkg_version`, not on `unused_0`. `[CARRIED — format-reference §0]`

---

## 2. The gzip inner archive — carved, inflated, integrity-verified `[HIGH × OBSERVED]`

Gzip header (`xxd -s 0xC08220 -l 16`):

```text
00c08220: 1f8b 0800 0000 0000  0003 ed5b 4b6f dc36
          ^^^^ magic 1f 8b   ^^ CM=08 deflate
                ^^ FLG=00 (no FNAME/EXTRA)   ^^^^^^^^ MTIME=0
                                  ^^ XFL=00  ^^ OS=03 (Unix)
                                              └─ deflate bitstream (ed 5b 4b …)
```

**The gzip span is exactly `data_size` `[HIGH × OBSERVED]`.** Carving `0x6AD` (1709)
bytes at `0xC08220` and feeding them to inflate succeeds with **no trailing garbage** —
the header's `data_size` is the precise gzip member length.

```text
inflate(1709 B gzip)            →  20 480 B (0x5000)  POSIX tar
gzip trailer  CRC32 = 0xED4F0AFC ,  ISIZE = 20480 (0x5000)
recomputed zlib.crc32(tar)      = 0xED4F0AFC          → MATCH
len(tar) == ISIZE               = 20480               → MATCH
tar ends in two 512-byte zero blocks (standard EOF)   → present
```

### The `pkg2` integrity hash, pinned to its input bytes `[HIGH × OBSERVED]`

```text
header.hash[0..15]            = 61b3cab2 4369b326 a2b3ee40 a744c746
md5sum(carved 1709-B gzip)    = 61b3cab24369b326a2b3ee40a744c746   → EXACT MATCH
md5sum(inflated 20480-B tar)  = 0a677f860f5c7289e66cdc2dd413f44f   → does NOT match
```

**Conclusion: the `pkg2` MD5 is computed over the *compressed* gzip payload** (the
`data` field, for `data_size` bytes) — **not** over the decompressed tar. The
reference said "MD5 (16 B)"; this carve pins it to "MD5 over the on-disk gzip data,"
recomputation-proven. Confirmed at the decompiled control-flow level in
`neff_parse` (`0x4ca3f0`, host x86, plain Hex-Rays):

```c
// neff_parse @0x4ca3f0, lines 226-235
nlog_write(…, "Verifying the md5 hash");
v77 = v12->data_size;                                 // == header.data_size (0x6AD)
if (v77 == 0)
    __assert_fail("len > 0",
        "/opt/workspace/KaenaRuntime/kelf/neff.cpp", 0x40u,
        "void md5(const uint8_t*, size_t, uint8_t*)");
MD5_Init(&s2);
MD5_Update(&s2, v12->data, v77);                      // v12->data == header + 0x400
MD5_Final(result, &s2);
if (memcmp(result, header.hash) != 0)                 // compares 16 B
    nlog_write(…, "MD5 mismatch!");
```

The digest is **unkeyed** (integrity, not authentication) and only checked when
`nrt_load`'s verify flag is set. `[CARRIED — format-reference §1; cross-ref the
zlib-only inflate via the libarchive gzip filter]`

### Container-gate bytes confirmed in the same decompile `[HIGH × OBSERVED]`

```c
// neff_parse @0x4ca3f0
if (neff_version_major > 2)                                  // line 171 — version ceiling
    nlog_write(…, "NEFF version: %lu.%lu is not supported …");
if (neff_version_major == 2 &&
    (v12->feature_bits & 0x7FFFFFFFF8000000LL) != 0)         // line 176 — fwd-compat trapdoor
    nlog_write(…, "… compiled by a newer version of Neuron compiler …");
if (neff_size - 1024 < data_size)                            // line 194 — data-fit; 1024 == header_size
    nlog_write(…, "Invalid NEFF data size(%lx vs %lx)", data_size, neff_size);
// line 220: pkg_version switch default → "Unsupported NEFF packager: %lu"
archive_read_open_memory(a, data, data_size);                // line 245 — in-memory tar walk, no temp file
```

This fixture passes every gate: `major 2`, `feature_bits 0` (trapdoor inert),
`pkg_version 2`, `data_size 0x6AD ≤ 0xAAD − 0x400`, MD5 match.

---

## 3. The tar member tree — 19 members, offsets mapped `[HIGH × OBSERVED]`

Re-walked with an independent 512-byte-block parser over the inflated tar. Each member
is a 512-byte ustar header followed by `ceil(size/512)` data blocks:

| member          | hdr_off | data_off | size | blks | typeflag |
|---|---|---|---|---|---|
| `kelf-a.json`   | 0       | 512      | 129  | 1 | `0` (file) |
| `neff.json`     | 1024    | 1536     | 980  | 2 | `0` |
| `sg00/` (dir)   | 2560    | 3072     | 0    | 0 | **`5`** (dir) |
| `sg00/pe.json`  | 3072    | 3584     | 63   | 1 | `0` |
| `sg00/def.json` | 4096    | 4608     | 1421 | 3 | `0` |
| `sg00/pool.bin` | 6144    | 6656     | 192  | 1 | `0` *(§5)* |
| `sg00/sp.asm`   | 7168    | 7680     | 0    | 0 | `0` |
| `sg00/pe.bin`   | 7680    | 8192     | 64   | 1 | `0` *(§5)* |
| `sg00/dve.json` | 8704    | 9216     | 65   | 1 | `0` |
| `sg00/act.json` | 9728    | 10240    | 182  | 1 | `0` |
| `sg00/dve.bin`  | 10752   | 11264    | 0    | 0 | `0` |
| `sg00/dve.asm`  | 11264   | 11776    | 0    | 0 | `0` |
| `sg00/act.bin`  | 11776   | 12288    | 0    | 0 | `0` |
| `sg00/sp.bin`   | 12288   | 12800    | 0    | 0 | `0` |
| `sg00/sp.json`  | 12800   | 13312    | 63   | 1 | `0` |
| `sg00/pool.asm` | 13824   | 14336    | 126  | 1 | `0` |
| `sg00/pe.asm`   | 14848   | 15360    | 153  | 1 | `0` |
| `sg00/pool.json`| 15872   | 16384    | 1042 | 3 | `0` |
| `sg00/act.asm`  | 17920   | 18432    | 0    | 0 | `0` |

The last header (`act.asm`, size 0) occupies `17920..18432`; from **`18432`** onward
the archive is **2048 zero bytes** to the `20480` end (the two mandatory EOF zero
blocks + GNU record padding). `20480 == gzip ISIZE` — the inflate is consistent
end-to-end.

> **CORRECTION (vs the loader-side `container-byte-format.md`).** That page labels the
> `pkg2` inner archive a **"POSIX-pax tar."** The carved member-0 ustar header shows it
> is **plain GNU tar**: magic `ustar ` (`0x75 73 74 61 72 20`, trailing **space**) +
> version ` \0` (`0x20 00`) at `+0x100`/`+0x106`, GNU **base-256** `uid`/`gid` (high bit
> `0x80` set), and **zero PAX/GNU-longname extension records** (no `typeflag 'x'/'g'/'L'/'K'`
> anywhere in the archive). The dialect is GNU `ustar`, not POSIX pax. `[HIGH × OBSERVED]`

### Member-0 ustar header, decoded byte-by-byte `[HIGH × OBSERVED]`

`xxd` of the first 320 bytes of the inflated tar:

```text
00000000: 6b65 6c66 2d61 2e6a 736f 6e00 …            name[100]   "kelf-a.json\0"
00000060: 0000 0000 3030 3030 3634 3400 8000 0000    mode[8]="0000644\0" ; uid[8] base-256→
00000070: 2552 e077 8000 0000 23c7 3fa1 3030 3030    …0x2552e077 ; gid[8] base-256→0x23c73fa1
00000080: 3030 3030 3230 3100 3134 3731 3432 3530    size[12]="00000000201\0"(=129) ; mtime[12]=
00000090: 3630 3000 3031 3531 3232 0020 3000 0000    "14714250600\0" ; chksum[8]="015122\0 " ; typeflag '0'
00000100: 0075 7374 6172 2020 0069 6d6d 696e 6b69    magic "ustar "+ver " \0" ; uname "imminki
00000110: 6e00 …                                     n\0"
00000120: 0000 0000 0000 0000  0064 6f6d 6169 6e5e   gname "domain^
00000130: 7573 6572 7300 …                            users\0"
```

```c
struct posix_tar_header {        // 512 B, GNU ustar dialect
  char name[100];    // +0x000  "kelf-a.json"
  char mode[8];      // +0x064  "0000644\0"     → octal 0644
  char uid[8];       // +0x06C  80 00 00 00 25 52 e0 77  → GNU base-256 = 626188407
  char gid[8];       // +0x074  80 00 00 00 23 c7 3f a1  → GNU base-256 = 600260513
  char size[12];     // +0x07C  "00000000201\0" → octal 0201 = 129  (== member size)
  char mtime[12];    // +0x088  "14714250600\0" → octal
  char chksum[8];    // +0x094  "015122\0 "     → octal 015122 (recomputed: MATCH)
  char typeflag;     // +0x09C  '0' regular file ; '5' for the sg00/ dir entry
  char linkname[100];// +0x09D  empty
  char magic[6];     // +0x100  "ustar "        ← GNU dialect (trailing space)
  char version[2];   // +0x106  " \0"
  char uname[32];    // +0x109  "imminkin"
  char gname[32];    // +0x129  "domain^users"
  /* devmajor/minor empty — no device member */
};
```

The 8-byte octal `chksum` field `015122` (= 6738) **recomputes exactly** as
`sum(header[:148]) + 8×0x20 + sum(header[156:512])` (the chksum field counted as eight
spaces) — the header is internally consistent.

### Loader skip-rules, checked against this instance `[HIGH × OBSERVED]`

- **No** member name ends in the `…checksum` suffix → this producer emitted no
  per-file checksum side-files, so the loader's `memcmp`-skip path is inert here.
- **No** member begins `./` → no two-char prefix strip needed.
- The `sg00/` directory entry carries `typeflag '5'`; the loader's tar walk skips
  `AE_IFDIR` members. Every non-directory member goes straight into `neff_t::files`.

---

## 4. The JSON members — verbatim `[HIGH × OBSERVED]`

### `kelf-a.json` (129 B) — the kelf descriptor

```json
{ "graphs": [ { "definition": "sg00/def.json", "name": "sg00" } ],
  "version": "0.5", "target": "*" }
```

One graph, `sg00`, defined by `sg00/def.json`. `target "*"` ⇒ **architecture-neutral**:
`kelf::load`'s arch gate matches `"*"` for any device, so no `NEFF_ARCH_INCOMPAT`
(there is no `arch_id`/codename gate to fail here — for fixtures that *do* pin one,
`arch_id 0x0c` ⇒ codename CAYMAN / NC-v3). `version "0.5"` is parse-only.

### `neff.json` (980 B) — the TVM/NNVM host-graph manifest

```text
nodes: [ {op "null", name "input", is_param "0"},
         {op "tvm_op", name "sg_tonga0",
          attrs{func_name "__kelf", kelf "kelf-a.json",
                num_inputs "1", num_outputs "1"}, output "output"} ]
arg_nodes:[0]   heads:[[1,0,0]]
attrs: shape list_shape [[32],[32]] ; storage_id [0,1] ;
       dltype list_str ["float32","float32"]
node_row_ptr:[0,1,2]
```

One input (32 floats) feeding one fused `__kelf` tonga op producing one output
(32 floats) — the host-graph framing around the single device sub-graph `sg00`.

### `sg00/def.json` (1421 B) — the section catalog + device-side var ABI

The keys consumed by `kelf_load_from_neff` in fixed order:

```text
"act":"act.json"  "dve":"dve.json"  "pe":"pe.json"  "pool":"pool.json"
"debug_info": { "wavegraph":"wavegraph-bin.json" }    ← references an ABSENT member (§7)
"dma_queue": {
   "q_gradient_in":  { owner "pool", semaphore 10, type "in"  },
   "q_gradient_out": { owner "pool", semaphore 11, type "out" } }
"git_version":""  "name":"definition"  "version":"0.6-"
"replica_groups":[ [] ]                  (one empty group — no real collective set)
"runtime_statebuffer_reservation":[]     (no EVTACCEL SB carveout — optional, OK)
```

### The var table — the device-side I/O ABI, carved `[HIGH × OBSERVED]`

```text
"var": {                                   dense var_id 0..4
  "SB":           { type "state-buffer",                              var_id 1 }
  "input":        { #transfer-type "input",  type "input",  size 128, element_size 4,
                    internal_shape [1,1,1,32],                        var_id 0 }
  "output":       { #transfer-type "output", type "output", size 128, element_size 4,
                    internal_shape [1,1,1,32],
                    #file-name "value_nn__output:0-simout.npy",       var_id 2 }
  "gradient_in":  { #transfer-type "tmp-buf", type "tmp-buf", size 128, var_id 3 }
  "gradient_out": { #transfer-type "tmp-buf", type "tmp-buf", size 128, var_id 4 }
}
```

| name | `var_id` | type | size / shape | role |
|---|---|---|---|---|
| `input`        | **0** | `input`        | 128 B = 32 × fp32, `[1,1,1,32]` | user input |
| `SB`           | **1** | `state-buffer` | — | on-chip state buffer |
| `output`       | **2** | `output`       | 128 B = 32 × fp32, `[1,1,1,32]` | user output |
| `gradient_in`  | **3** | `tmp-buf`      | 128 B | all-reduce scratch (src) |
| `gradient_out` | **4** | `tmp-buf`      | 128 B | all-reduce scratch (dst) |

Var-table facts `[HIGH × OBSERVED]`:

- `var_id` are **dense 0,1,2,3,4** (`max+1 == count == 5`) → passes `check_var_ids`.
- `num_outputs == 1` (`> 0`) → passes the "no Outputs" reject.
- The `output`'s `#file-name` names a **simulator** `.npy`, but that file is **not** in
  the tar (§7). `output` is an OUTPUT var (the runtime fills it at execution), **not** a
  packed constant — the file-name is a naming hint, not a weight payload. **This NEFF
  carries no constant/weight tar member** (the `numpy_load`/`MR_BUFFER` weight path is
  unexercised here).

The engine descriptor JSONs are thin: `pe.json` / `dve.json` / `sp.json` are bare
`{ "dma":[], "instr":"<eng>.bin", "name":"<eng>_array_json" }`; `act.json` adds
`"act_info":"act_info.json"` (absent member, §7) and `"act_semaphore":9`.
`pool.json` (1042 B) carries the two DMA descriptor blocks fired by the triggers (§5.3).

---

## 5. The engine `.bin` sequencer slots — the container framing `[HIGH × OBSERVED]`

Engine `.bin` sizes in this instance: `pe.bin` 64 B (1 slot), `pool.bin` 192 B (3 slots),
and `act.bin` / `dve.bin` / `sp.bin` = **0 bytes** (empty placeholders → the loader emits
an empty `instr_set` and the "Engine %s not found … empty placeholder" WARN path).

Every slot is **64 bytes**. The shared 4-byte slot header — kept **identical** to the
[`seq-microcode` decode](./seq-microcode.md) — is:

```c
struct tpb_inst_header {       // first 4 B of every 64-B slot
  uint8_t opcode;        // +0x00  TONGA_ISA_TPB_OPCODE = base | (engine << 5)
  uint8_t inst_word_len; // +0x01  == 0x10 == 16 NWORDS × 4 B = 64-B slot length
  uint8_t debug_cmd;     // +0x02
  uint8_t debug_hint;    // +0x03
};                       // ENGINE: PE=0 ACT=1 POOL=2 ALL=3 RT=6 SIM=7
```

> **GOTCHA — `byte1 = 0x10` is the slot-length marker, not part of a 16-bit opcode.** A
> naive reader of the little-endian lead word sees `0x10C8` (pe.bin) or `0x10C1`/`0x10A0`
> (pool.bin) and reports a 16-bit opcode. It is **`{opcode = byte0, inst_word_len =
> byte1 = 0x10 = 16 words = 64 B}`** — the `0x10` is constant for every slot. The older
> "16-bit LE `0x10C8`" reading is **corrected**; pin `byte0` as the 1-byte
> `TONGA_ISA_TPB_OPCODE`. `[HIGH × OBSERVED — matches seq-microcode §1]`

### 5.1 `pe.bin` (64 B, one slot) — the container around the collective record

`xxd` of the `pe.bin` member (`md5 f5c01e49…`):

```text
00: c8 10 00 00  04 0a 13 16  00 00 00 00  04 0a 00 00
10: 03 00 00 00  04 00 00 00  20 00 00 00  00 00 00 00
20: 01 00 00 00  00 × 28
```

Container-frame decode (the field *semantics* are the [`seq-microcode`](./seq-microcode.md)
page's; here we show the bytes frame exactly one slot):

```c
// pe.bin slot 0 — header + collective payload
+0x00  opcode        = 0xC8   // 0x08 | (RT(6)<<5) — RT-class pseudo
+0x01  inst_word_len = 0x10   // 16 words = 64 B
+0x02  debug_cmd=0  +0x03 debug_hint=0
+0x04  wait_mode  = 0x04  ($S >= val)   wait_idx  = 10   ($S[10])
+0x06  update_mode= 0x13  (inc@complete) update_idx= 22   ($S[22])
+0x08  semaphore_value = 0
+0x0C  op = 0x04 (ADD)   dtype = 0x0A (FP32)   group_id = 0
+0x10  input_tensor_id  = 3   (gradient_in)
+0x14  output_tensor_id = 4   (gradient_out)
+0x18  num_elements     = 32  (0x20, u64)
+0x20  ctype = 0x01 (ALL_REDUCE) ; rest 0
```

> **NOTE — `0xC8` mnemonic is generation-dependent; the encoding is stable.** The shipped
> ulib 0.21.2 header names RT base `0x08` = `PSEUDO_READ_VAR_ADDR`. This sample is a newer
> NEURON_ISA NEFF: its `pe.asm` prints `PSEUDO_TRIGGER_COLLECTIVE` and the 64-byte payload
> decodes byte-exact as a collective record. RT base `0x08` was reassigned across
> generations; what is stable is `0x08 | (RT<<5) = 0xC8`. Pin the encoding, not the
> mnemonic. `[HIGH × OBSERVED — matches seq-microcode §2.1 CORRECTION]`

The producer `pe.asm` shipped beside the `.bin` confirms the decode to the field:

```text
PSEUDO_TRIGGER_COLLECTIVE $S[10]>0 $S[22]++@complete ctype=ALL_REDUCE
  input_tensor_id=3 output_tensor_id=4 num_elements=32 dtype=fp32 op=ADD group_id=0;
```

### 5.2 `pool.bin` (192 B, three slots) — and the proof of the 8-byte events dialect

`xxd` of the `pool.bin` member (`md5 25daba76…`), slots at `+0x00 / +0x40 / +0x80`:

```text
slot0 @+0x00:  c1 10 00 00 00 00 00 00  00 00 00 00 71 5f 67 72   "…q_gr
       +0x10:  61 64 69 65 6e 74 5f 69 6e 00 …                     adient_in\0"
slot1 @+0x40:  c1 10 00 00 04 16 00 00  00 00 00 00 71 5f 67 72   "…q_gr
       +0x50:  61 64 69 65 6e 74 5f 6f 75 74 00 …                  adient_out\0"
slot2 @+0x80:  a0 10 00 00 04 0b 00 00  00 …
```

```c
// pool.bin slot 0 — DMA trigger, no wait
+0x00 opcode 0xC1  // 0x01 | (RT(6)<<5)  PSEUDO_DMA_TRIGGER ; word_len 0x10
+0x04 wait_mode=0  wait_idx=0  (no wait) ; update_mode=0 update_idx=0
+0x0C dma_queue_name = "q_gradient_in"   ; block_id 0 (use_raw_count=0)

// pool.bin slot 1 — DMA trigger gated on $S[22]
+0x00 opcode 0xC1  PSEUDO_DMA_TRIGGER ; word_len 0x10
+0x04 wait_mode=0x04  wait_idx=0x16 (=22)  → wait $S[22]>=val
+0x0C dma_queue_name = "q_gradient_out"   ; block_id 0

// pool.bin slot 2 — SEQ control op (class 5, not a TPB engine)
+0x00 opcode 0xA0  // base 0x00 | (class 5 << 5) — EVENT_SEMAPHORE, SP/SEQ control
+0x04 wait_mode=0x04  wait_idx=0x0B (=11)  → wait $S[11]>=val ; no name
```

> **GOTCHA — the queue name decodes cleanly *only* at `slot+0x0C`, which proves the
> 8-byte events dialect.** The two `events` half-words (`wait` at `+0x04`, `update` at
> `+0x06..`) occupy bytes `+0x04..+0x0B`, so the variable payload (the queue name)
> starts at `+0x0C`. A 4-byte-events reading would place the name at `+0x08` and split
> `"q_gradient_in"` across the `events` field — the string would be corrupt. The clean
> ASCII at `+0x0C` is direct evidence for the NEURON_ISA 8-byte `events` layout.
> `[HIGH × OBSERVED]`

The producer `pool.asm` confirms all three slots:

```text
PSEUDO_DMA_TRIGGER q_gradient_in block_id=0;
PSEUDO_DMA_TRIGGER $S[22]>0 q_gradient_out block_id=0;
EVENT_SEMAPHORE $S[11]>0;
```

### 5.3 The descriptor blocks (`pool.json` `dma`) — the data movement the triggers fire

```text
block id 0  q_gradient_in : from "input"        → to "gradient_in"
            from_sizes[128,1] from_steps[1,128] to_sizes[128,1] to_steps[1,128]
block id 0  q_gradient_out: from "gradient_out" → to "output"   (same shape)
```

So slot0's `PSEUDO_DMA_TRIGGER(q_gradient_in)` drives the `input → gradient_in` copy,
and slot1 (after `$S[22]`) drives `gradient_out → output`.

### 5.4 The end-to-end dataflow of this concrete model `[INFERRED from the bytes above]`

```text
POOL slot0 : DMA input(0) → gradient_in(3)              (q_gradient_in, sets $S[10])
PE   slot  : wait $S[10]; ALL_REDUCE(ADD,fp32)
             gradient_in(3) → gradient_out(4), 32 elems; set $S[22] on complete
POOL slot1 : wait $S[22]; DMA gradient_out(4) → output(2)  (q_gradient_out, sets $S[11])
POOL slot2 : EVENT_SEMAPHORE wait $S[11]                (gate completion)
```

One toy all-reduce of a 32-float gradient, end-to-end, in four sequencer slots. The
DMA descriptors are the carved [DGE program](../dma/dge-microop-encoding.md)'s payload
seen from the NEFF container side.

---

## 6. The complete byte map of this NEFF `[HIGH × OBSERVED]`

```text
file offset    span         what
-------------  -----------  ----------------------------------------------------------
0xC07E20       0x000..0x3FF 1024-B neff_header_t (§1): pkg2, hdr 0x400, data 0x6AD,
                            ver 2.0, MD5 61b3…, uuid 5618…, name "x", tpb_count 1,
                            feature_bits 0, vnc 1
0xC08220       gzip 0x6AD   gzip(deflate) member, OS=Unix, CRC 0xED4F0AFC,
               (1709 B)     ISIZE 20480 ; MD5 of these bytes == header.hash[0..15]
0xC088CD       end          (0xC08220 + 0x6AD) — NEFF total length 0xAAD from header
```

Inner tar (20480 B after inflate), member → `(hdr_off, data_off, size)`:

```text
kelf-a.json      (0,     512,   129)    neff.json     (1024,  1536,  980)
sg00/  dir       (2560,  3072,    0)    sg00/pe.json  (3072,  3584,   63)
sg00/def.json    (4096,  4608, 1421)    sg00/pool.bin (6144,  6656,  192)*
sg00/sp.asm      (7168,  7680,    0)    sg00/pe.bin   (7680,  8192,   64)*
sg00/dve.json    (8704,  9216,   65)    sg00/act.json (9728, 10240,  182)
sg00/dve.bin     (10752,11264,    0)    sg00/dve.asm  (11264,11776,    0)
sg00/act.bin     (11776,12288,    0)    sg00/sp.bin   (12288,12800,    0)
sg00/sp.json     (12800,13312,   63)    sg00/pool.asm (13824,14336,  126)
sg00/pe.asm      (14848,15360,  153)    sg00/pool.json(15872,16384, 1042)
sg00/act.asm     (17920,18432,    0)    [EOF zero blocks 18432..20480]
(* = the two engine programs framed in §5.)
```

---

## 7. Referenced-but-absent members — what this instance does *not* carry `[HIGH × OBSERVED]`

Two members are **named by JSON but absent from the tar**:

- `wavegraph-bin.json` (`def.json` `debug_info.wavegraph`) — pre-lowering IR, a debug
  artifact, stripped from this production build.
- `act_info.json` (`act.json` `act_info`) — activation metadata, absent because the
  ACT engine is empty here.

And the `output` var's `#file-name` simulator `.npy` is absent.

> **NOTE — a NEFF reader must tolerate dangling JSON references to optional members.**
> `neff_get_file_content` returns "not found" for these debug/IR/sim files and the
> loader proceeds — they are not load-critical. `[HIGH × OBSERVED]`

`act.bin` / `dve.bin` / `sp.bin` are 0 bytes — the ACT, DVE, and SP engines carry no
program in this model.

**Absent entirely from this instance** (exercised elsewhere, carried not carved): weight/
constant `.npy`/`.bin` payloads, the `ucode_lib` custom-op manifest + device-code BIN,
`sb_carveout` (EVTACCEL), and replica/src-target collective routing with real groups.
Those remain in the [format reference](./format-reference.md), not carved here, because
this minimal fixture does not contain them.

---

## 8. Reproduction & cross-check `[HIGH × OBSERVED]`

```bash
LIB=…/aws-neuronx-runtime-lib_2.31.24.0-…/opt/aws/neuron/lib/libnrt.so.2.31.24.0
xxd -s 0xC07E20 -l 1024 "$LIB"                     # the header
dd if="$LIB" bs=1 skip=$((0xC08220)) count=$((0x6ad)) of=inner.gz
gunzip -c inner.gz > inner.tar                      # 1709 → 20480 B
md5sum inner.gz   # 61b3cab24369b326a2b3ee40a744c746  == header.hash[0..15]
# gzip ISIZE 20480 / CRC 0xED4F0AFC both match the inflated tar
tar -tvf inner.tar                                  # 19 members
```

**Two independent extraction paths agree byte-for-byte.** Re-carving the gzip from the
ELF `.data` bytes and inflating in Python yields a tar whose `md5 == 0a677f86…`,
identical to the corpus binwalk extraction at
`neuronx-runtime/binwalk/extract_libnrt_so/libnrt.so.extracted/C08220/decompressed.bin`.
Per-member MD5s match the binwalk tar tree: `def.json f642c38b…`, `pe.bin f5c01e49…`,
`pool.bin 25daba76…`, and the carved gzip md5 equals the header hash.

---

## 9. Cross-ties

- [`neff/format-reference.md`](./format-reference.md) — the abstract spec; this page is
  its single concrete instance.
- [`neff/container-byte-format.md`](./container-byte-format.md) — the loader-side header
  struct + gate decompile. This page **corrects** its "POSIX-pax tar" label to **GNU
  ustar** (§3) and confirms its MD5-over-compressed-bytes finding by recomputation.
- [`neff/seq-microcode.md`](./seq-microcode.md) — the 64-byte sequencer word decode
  (`byte0 = opcode = base|(engine<<5)`, `byte1 = 0x10`, the `0xC8`/`0xC1`/`0xA0`
  mnemonic notes). This page carves the **container framing** around those exact bytes;
  the per-field semantics are deferred to it.
- [`dma/dge-microop-encoding.md`](../dma/dge-microop-encoding.md) — the carved DGE
  program whose descriptors are the `pool.json` `dma` blocks fired by the triggers (§5.3).

---

## Guard restatement

Every byte-level claim above is a direct read of the shipped `libnrt.so` `.data` bytes
(`xxd`/`dd` at `0xC07E20`/`0xC08220`), the inflated inner tar, and the producer-shipped
`.asm`/`.json` members inside it — independently recomputed (MD5, CRC32, ustar checksum)
and cross-checked against the corpus binwalk extraction for byte-identity. Control-flow
facts (the MD5/feature_bits/data-fit gates) are from the `libnrt.so` decompiled
`neff_parse` (`0x4ca3f0`, host x86, plain Hex-Rays). The 64-byte sequencer slots are
framed by documented struct layout and cross-validated against the producer `.asm`
disassembly shipped in the same tar — not by any device-side disassembler. Honest limit:
this is one minimal embedded fixture (no weights, no custom-op ucode lib); those format
sections are carried from the reference, not carved, because this instance does not
contain them.
