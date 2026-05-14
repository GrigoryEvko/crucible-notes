# AsyncValueImpl and BLAKE3 IR Interning

## Abstract

Two pieces of `nv_tileas` infrastructure sit immediately under the warp-specialisation scheduler. The first is `AsyncValueImpl`, an 808-byte (`0x328`) heap record that anchors every `Pipe_` and `Mutex_` SSA value the scheduler manipulates; the second is a BLAKE3-based content hasher used as the keying function for several intern tables that deduplicate IR-object tuples. The two mechanisms are unrelated in purpose — one is a fat scheduler-side header, the other is a 64-bit content key — but they share callers in the same address range and they share the same allocator family, so they are documented together.

The BLAKE3 driver lives at `sub_45BF670`. It is **not** ChaCha20 even though both algorithms use the 7/8/12/16 rotation set: the binary loads the canonical SHA-256/BLAKE2/BLAKE3 IV (`0x6A09E667 0xBB67AE85 0x3C6EF372 0xA54FF53A 0x510E527F 0x9B05688C 0x1F83D9AB 0x5BE0CD19`) verbatim from `xmmword_503C080` / `xmmword_503C090` as two `_mm_load_si128` operands, contains exactly 56 sixteen-bit left-rotations per block (seven rounds of eight quarter-rounds), threads a chunk-counter and a ROOT/CHUNK_END flag bit through the compression block, and never contains the `"expand 32-byte k"` / `"expand 16-byte k"` sigma strings that any ChaCha20 implementation would carry. Five independent corroborations of this identification exist in the binary; the only feature ChaCha20 and BLAKE3 share at this depth is the rotation amounts.

## AsyncValueImpl Header

Behind every `Pipe_` or `Mutex_` SSA value the scheduler creates sits an 808-byte `AsyncValueImpl`. Three constructors allocate one: `sub_8E0070` for the `Mutex_` flavour (3240 bytes), `sub_8E9450` for the scalar `Pipe_` flavour (3157 bytes), and `sub_8EA0B0` for the tensor `Pipe_` flavour (3264 bytes). All three call `sub_44A8C20(0x328)` — a `BumpPtrAllocator`-style wrapper that hands out arena-stable pointers — then run the same 14-line initialiser prologue before specialising. Arena stability is non-negotiable: the `DenseMap<Operation*, T>` instances embedded in the header hash with `(op>>9) ^ (op>>4)`, so moving an `AsyncValueImpl` after construction would break every probe that follows.

The initialiser sets up three inline `SmallString` heads at capacity 3, four inline `SmallVector<u64,6>` heads at capacity 6, sets `hasValue` at byte 64, copies `"Mutex_"` (at `0x4607054`) or `"Pipe_"` (at `0x4607077`) into the `std::string` SSO buffer at offset 0 through `sub_44E1740`, then stitches the header into the owning builder's growable `SmallVector<AsyncValueImpl*>` at `(builder+168, +176, +180)`. Capacity encodings are `0x300000000` (low dword `size=0`, high dword `cap=3`) for the SSO strings and `0x600000000` for the inline-6 SmallVectors.

```c
struct AsyncValueImpl {
  /*+0x000*/  char     name_dataplus[8];        // std::string GCC SSO: data ptr
  /*+0x008*/  uint64_t name_length;             // string size
  /*+0x010*/  char     name_inline[16];         // SSO inline buffer ("Mutex_\0" or "Pipe_\0")
  /*+0x020*/  void*    producerType;            // Operation* (Pipe_/Pipe-T) or 0 (Mutex_)
  /*+0x028*/  void*    consumerType;            // Operation*
  /*+0x030*/  void*    producerPayload;         // first qword of *a7[2]
  /*+0x038*/  void*    consumerPayload;         // first qword of *a7[3]
  /*+0x040*/  uint8_t  hasValue;                // 1 once emitPayload runs
  /*+0x041*/  uint8_t  _pad0[7];
  /*+0x048*/  uint32_t regionStageKind;         // 1 for Mutex_; looked up for Pipe_
  /*+0x04c*/  uint16_t okFlag;                  // Optional<u8>: {hasValue, value}
  /*+0x04e*/  uint16_t payloadFlag;             // Optional<u8>; Pipe_ writes 0x0101
  /*+0x050*/  SmallString_48  tag1;             // inline cap=3 (data@80, size/cap@88, inline@96)
  /*+0x090*/  SmallString_48  tag2;             // inline cap=3 (data@144, size/cap@152, inline@160)
  /*+0x0d0*/  uint8_t  kind;                    // 0=scalar Pipe_, 1=Mutex_ or Pipe-T
  /*+0x0d1*/  uint8_t  _pad1[7];
  /*+0x0d8*/  DenseMap_48 chainMapA;            // <Op*, SmallVector<u64,0>>; 48-byte bucket
  /*+0x0f0*/  DenseMap_48 chainMapB;            // symmetric consumer-side
  /*+0x108*/  SmallVector_u64_6  stageVecA;     // 64 B, cap=6 inline (data@264)
  /*+0x148*/  SmallVector_u64_6  stageVecB;     // 64 B, cap=6 inline (data@328)
  /*+0x188*/  DenseMap_16 indexMap0;            // <Op*, i32>; 16-byte bucket (Mutex_ primary)
  /*+0x1a0*/  DenseMap_16 indexMap1;            // symmetric
  /*+0x1b8*/  uint32_t statusBits0;             // OR-accumulated by emitPayload
  /*+0x1bc*/  uint32_t statusBits1;             // consumer-side analogue
  /*+0x1c0*/  void*    scheduleMirror;          // cached Schedule::opToStage data ptr
  /*+0x1c8*/  uint64_t _reserved1;
  /*+0x1d0*/  uint32_t scheduleCapacity;
  /*+0x1d4*/  uint32_t _pad2;
  /*+0x1d8*/  SmallVector_Op_6  producerOps;    // 64 B inline-6 (data@472, size@480, cap@484)
  /*+0x218*/  SmallVector_Op_6  consumerOps;    // 64 B inline-6 (data@536)
  /*+0x258*/  SmallVector_u64_0 producerOrders; // 24 B zero-inline (data@600)
  /*+0x270*/  SmallVector_u64_0 producerStages; // 24 B (data@624)
  /*+0x288*/  SmallVector_u64_0 consumerOrders; // 24 B (data@648)
  /*+0x2a0*/  SmallVector_u64_0 consumerStages; // 24 B (data@672)
  /*+0x2b8*/  SmallVector_u64_0 producerPairsA; // 24 B; packed (stage<<32 | order)
  /*+0x2d0*/  SmallVector_u64_0 consumerPairsA; // 24 B
  /*+0x2e8*/  SmallVector_u64_0 producerPairsB; // 24 B
  /*+0x300*/  SmallVector_u64_0 consumerPairsB; // 24 B
  /*+0x318*/  __m128i  statusWord;              // Optional<RegisterSlot>, init from xmmword_4607080
};
static_assert(sizeof(struct AsyncValueImpl) == 0x328, "808 bytes");
```

The dual DenseMap widths are intentional and every reader depends on them. 48-byte-stride maps at `+0x0d8` / `+0x0f0` carry `SmallVector<u64,0>` values (the order set observed for each chained operation); 16-byte-stride maps at `+0x188` / `+0x1a0` carry raw `i32` indices. Both share the same `(op>>9)^(op>>4)` hash, the same tombstone `(Operation*)-4096` and empty `(Operation*)-8192` sentinels, and the same `4*(size+1) >= 3*capacity` rehash threshold. Mixing the bucket strides corrupts every later read — every consumer indexes by absolute byte offset.

## Construction Prologue

All three constructors run the same 14-line initialiser before specialising. Every embedded inline buffer becomes self-pointer-valid before any subsequent write touches it — deferring this step breaks the SmallString / SmallVector inline-vs-heap discriminator that downstream code relies on.

```c
void asyncvalue_init(struct AsyncValueImpl *v) {
  memset(v, 0, 0x328);

  /* three SmallString<48> heads: data ptr -> own inline buffer */
  ((uint64_t*)v)[0]  = (uint64_t)(v + 16);     /* name (std::string SSO) */
  ((uint64_t*)v)[10] = (uint64_t)(v + 96);     /* tag1.data -> tag1.inline */
  ((uint64_t*)v)[18] = (uint64_t)(v + 160);    /* tag2.data -> tag2.inline */
  ((uint64_t*)v)[11] = 0x300000000ULL;         /* tag1: size=0, cap=3 */
  ((uint64_t*)v)[19] = 0x300000000ULL;         /* tag2: size=0, cap=3 */

  /* four SmallVector<u64,6> heads: data ptr -> own inline storage */
  ((uint64_t*)v)[33] = (uint64_t)(v + 280);    /* stageVecA */
  ((uint64_t*)v)[41] = (uint64_t)(v + 344);    /* stageVecB */
  ((uint64_t*)v)[59] = (uint64_t)(v + 488);    /* producerOps */
  ((uint64_t*)v)[67] = (uint64_t)(v + 552);    /* consumerOps */
  ((uint64_t*)v)[34] = 0x600000000ULL;         /* stageVecA: size=0, cap=6 */
  ((uint64_t*)v)[42] = 0x600000000ULL;         /* stageVecB */
  ((uint64_t*)v)[60] = 0x600000000ULL;         /* producerOps */
  ((uint64_t*)v)[68] = 0x600000000ULL;         /* consumerOps */
}
```

Each constructor then writes its flavour-discriminating fields. `sub_8E0070` writes `kind = 1` at `+208`, `regionStageKind = 1` at `+72`, copies `"Mutex_"` into the SSO, threads producer/consumer index ranges through `sub_8D9750` into `producerOps` / `consumerOps`, runs the two parallel hash-table fill loops over `indexMap0` / `indexMap1`, and ends by calling `sub_8F7900` to compute the `okFlag` Optional at `+76`. `sub_8E9450` leaves `kind = 0`, looks up `regionStageKind` from the region-tree map at `(builder+104)` through `sub_8DA7D0`, writes `"Pipe_"` into the SSO, and conditionally writes `payloadFlag = 0x0101` at `+78` when no producer-side range is supplied. `sub_8EA0B0` is the tensor variant — same shape as the scalar `Pipe_` plus an extra arm threading two additional `SmallVector` arguments through `chainMapA` / `chainMapB`.

The shared tail `sub_8E7A70` (`Pipe::emitPayload`) drives the transition from CONSTRUCTED to PAYLOADED. It copies `*(a7+16)` and `*(a7+24)` into `producerPayload` / `consumerPayload`, sets `hasValue = 1`, caches `Schedule::opToStage`'s data pointer into `scheduleMirror`, populates the four `SmallVector<u64,0>` quadruples by joining each producer/consumer op against the scheduler's stage and order maps, then loads the 16-byte Optional<RegisterSlot> status word from `xmmword_4607080` into `statusWord` at `+792`. The header carries no atomic refcount: lifetime is arena-based, and teardown only runs on the failure-to-append path in the constructor through `sub_8DB490` followed by `free`.

## parseFromAttrs

`sub_8FB180` is the Schedule-side companion that lets the verifier and a few lowering passes rebuild an in-memory `Schedule` from MLIR attributes attached to the schedule-owning operation. It reads two `DenseI64ArrayAttr` attributes — `"nv_tile.aws.stage"` and `"nv_tile.aws.order"` — through the discardable-attribute fast path (`sub_446DC50`) when the op's discardable bit is set, or the inherent-attribute dictionary walker (`sub_440E370`) otherwise, validates both type tags against `&unk_5BE5F40`, then walks the block's operation list in lock-step with the two arrays.

Each `(op, stage, order)` triple drops into a pair of 16-byte-stride `DenseMap<Operation*, int32_t>` instances laid out exactly like the `indexMap0` / `indexMap1` maps inside `AsyncValueImpl` — same hash, same sentinels, same load factor. The accumulator is a 60-byte `Schedule` struct: owning op at `+0`, stage map at `+8/+16/+24`, order map at `+32/+40/+48`, valid flag at `+56`. A type-tag mismatch on either side clears the valid flag and returns immediately; the downstream verifier treats `!valid` as "schedule not parsed".

## BLAKE3 Driver

Four entry points reach tileiras' IR-interning callers: `blake3_init` at `sub_45BEC80`, `blake3_update` at `sub_45BECE0`, `blake3_finalize` at `sub_45BF540`, and a CPU-feature dispatcher pair at `sub_45BF620` / `sub_45BF670` that tail-call the actual compression routines at `sub_45BF840` (in-place state update) and `sub_45C03D0` (output-emitting variant). The dispatchers consult a lazy-initialised feature mask at `dword_5B3761C` built from CPUID(0/1/7) and `xgetbv` results; in this binary the mask only ever gates between "uninitialised" and "baseline scalar", so the AVX2 / AVX-512 specialisations the upstream BLAKE3 reference would carry have been stripped.

The initialiser loads the canonical 64-byte IV (the 8-word constant shared by SHA-256, BLAKE2 and BLAKE3) into the first 64 bytes of the hasher state, then zeroes the counter, the buffer, and the flags. The state is 1976 bytes — observed as `_BYTE v34[1976]` in `sub_3CC6B10`, and `_BYTE v8[1920]` plus an 8-byte counter slack in `sub_3C92D50`. Both call sites write `1` into the byte at offset 1912 — BLAKE3's default flags (0) combined with the chunk-state `block_len` field set to 1. The binary never sets a non-zero key; `blake3_hasher_init` always loads the full IV, so this is plain hash mode — never keyed-hash, never derive-key.

The compression block runs seven rounds of eight quarter-rounds — count the `__ROL4__(..., 16)` invocations in `sub_45BF840` and you get exactly 56 — and adds the IV words 0x6A09E667 / 0xBB67AE85 / 0x3C6EF372 / 0xA54FF53A back into the rotated state at the end. The finalizer in `sub_45BF540` runs an outer loop with a chunk counter (`v44` in the decompile) and ORs the ROOT flag (`8`) into the final block's domain-separation byte; for short last blocks the `CHUNK_END | ROOT` combination falls out of the `(buffer_len == 0)` guard. Stream ciphers carry no ROOT / CHUNK_START / CHUNK_END flags, no chaining-value tree, and would never load the SHA-256 IV. This driver is BLAKE3.

```c
/* The canonical 4-call sequence used by every interning caller. */
uint64_t blake3_intern_key(void *parent_ptr, int32_t i, int32_t j) {
  uint8_t  hasher[1976];           /* sizeof(blake3_hasher); 1976 in this build */
  uint64_t digest;                 /* 8-byte truncated output, used as table key */

  sub_45BEC80(hasher);             /* blake3_hasher_init: loads IV from xmmword_503C080/090 */
  sub_45BECE0(hasher, &parent_ptr, 8);  /* update: parent pointer       */
  sub_45BECE0(hasher, &i,          4);  /* update: first int32          */
  sub_45BECE0(hasher, &j,          4);  /* update: second int32         */
  sub_45BF540(hasher, &digest,     8);  /* finalize: 8-byte XOF emit    */
  return digest;
}
```

Every consumer treats the 8-byte truncated digest as a 64-bit hash. Nothing in the binary uses the full 256-bit BLAKE3 output, and nothing uses BLAKE3 in keyed-hash or derive-key mode. Swapping BLAKE3 for upstream MLIR's `llvm::hash_combine` (SipHash-derived) is unusual but harmless — the same key shape and bucket policy as stock MLIR `StorageUniquer` flows through it. The most plausible motivation is determinism across a polyglot toolchain where one component is a Rust crate that bundles BLAKE3 directly.

## Five Interning Callers

Five caller families consume the 8-byte digest, each driving a different intern-table shape. The shared input is always a small tuple — `(pointer, int32, int32)` or a trivial extension of it.

`sub_2CC9780` is the RB-tree caller. It hashes `(parent_op, i, j)`, walks an `std::map`-style red-black tree anchored at on-stack sentinel `&v343` / `&v348` with left, right, and parent pointers at node offsets `+16`, `+24`, and `+0`, and tests the 8-byte digest against a key field at `+32`. Insertion uses the standard top-down `(unsigned __int64)ptr <= v42[4]` comparison; lookup short-circuits on equality. This is the deduplication path for the IR-construction routine's most complex sub-tree.

`sub_3CC1560` is the primary open-addressing intern. Capacity at `+56`, table base at `+40`, occupancy at `+48`, power-of-two capacity rounded via popcount, `4*(occ+1) >= 3*cap` rehash threshold. The sentinels are not the usual `-4096` / `-8192`: they are `tomb = qword_5BDD9D8` and `empty = unk_5BDD9E0`, two address-space-stable constants the binary keeps in `.bss`. The successful-insert return value is `table_index + 4096`, where `4096` is a "no inline value" sentinel that distinguishes "key was newly created and has no associated payload yet" from a real index.

`sub_3CC1E30` and `sub_3CC2680` are byte-identical siblings of `sub_3CC1560` against different IR-object kinds — same capacity-mask probing, same `qword_5BDD9D8` / `unk_5BDD9E0` sentinels, same `4096` "no inline value" return convention. The three live as separate functions instead of a single templated body because the inline equality test against the original key tuple differs slightly per IR-object kind.

`sub_3C92D50` is the vector-of-tuples hasher. It takes an `__int64 *a1[]` of length `a2` whose elements are 16-byte `(u64, u32, u32)` records, runs the standard `init → update(parent_ptr, 8) → update(i32, 4) → update(j32, 4)` sequence in a loop, and finalizes once at the end. The 8-byte digest is the intern key for a flat vector table; the stack frame declares `_BYTE v8[1920]` for the hasher state and writes `v8[1912] = 1` at the chunk-state `block_len` slot.

`sub_3CC6B10` is the heaviest consumer: a buffer-plus-sidecar hasher with a 1976-byte hasher state on its stack (`_BYTE v34[1976]`) and a `v32 = 0x400000000LL` initialiser that packs `(flags<<32) | block_len = (4 << 32) | 0` — i.e. the BLAKE3 `CHUNK_END` flag prearmed for short content. The caller is the buffer-plus-sparse-table content hasher that 1560 / 1E30 / 2680 fan out into when their inline key tuples reference a content blob rather than a pointer.

## State Machine

An `AsyncValueImpl` cycles through four observable states across the five constructors and the shared tail. `sub_44A8C20(0x328)` followed by `memset(0)` produces ZEROED. The initialiser prologue produces SKELETON: every SmallString and SmallVector head points at its own inline storage, every DenseMap is empty. Writing the name through `sub_44E1740` plus the `kind` / `regionStageKind` fields produces CONSTRUCTED. Running `sub_8E7A70` produces PAYLOADED: `hasValue = 1`, the four DenseMaps populated, the eight `SmallVector<u64,0>` quadruples filled, the `statusWord` at `+792` loaded from `xmmword_4607080`. No observable transition leads back from PAYLOADED — teardown is arena discard, not per-object destruction.

The six fields that encode this state machine — `hasValue` at byte 64, `kind` at byte 208, `regionStageKind` at byte 72, `okFlag` at byte 76, `payloadFlag` at byte 78, and the two OR-accumulated `statusBits` dwords at bytes 440 and 444 — are read by absolute offset throughout the scheduler. Reordering any of them breaks `ListSchedule::verify` (`sub_8F5410`), `LoopSchedule::verify` (`sub_8F80E0`), and the dispatch hub `Schedule::verifyStageOrder` (`sub_8F87A0`).

## How to Recognize in a Binary

Three independent fingerprints identify the AsyncValueImpl path with no ambiguity:

- The constructor signature is an `sub_44A8C20(0x328)` allocation immediately followed by a
  `memset(0, 0x328)` and then a sequence of self-pointer initialisers writing the inline-buffer
  addresses (`v+16`, `v+96`, `v+160`, `v+280`, `v+344`, `v+488`, `v+552`) into their owning header
  slots. Any function with this exact prologue is `Mutex_` (`sub_8E0070`), scalar `Pipe_`
  (`sub_8E9450`), or tensor `Pipe_` (`sub_8EA0B0`).
- The capacity-encoding immediates `0x300000000` (size=0, cap=3) and `0x600000000` (size=0, cap=6)
  appearing in pairs identify the SmallString and SmallVector head initialisers, respectively. These
  immediates are unambiguous in `0xN00000000` form because no DenseMap sentinel produces values in
  this range.
- The literal strings `"Mutex_"` at `0x4607054` and `"Pipe_"` at `0x4607077`, both interned through
  `sub_44E1740` into the std::string SSO at byte 0 of the header, locate the flavour switch.

The BLAKE3 driver is identified by the SHA-256/BLAKE2/BLAKE3 IV pair at `xmmword_503C080` and
`xmmword_503C090` (`0x6A09E667 0xBB67AE85 ...`), the 1976-byte hasher-state stack frame in callers,
and the `chunk_state.block_len = 1` write at byte 1912 of that frame. The absence of `"expand
32-byte k"` or `"expand 16-byte k"` strings anywhere in the surrounding code, plus the presence of
the ROOT/CHUNK_START/CHUNK_END flag bits, rules out ChaCha20 — the only feature ChaCha20 and BLAKE3
share at this depth is the quarter-round rotation amounts.

## Consumers

The `AsyncValueImpl` headers are produced during `MaterializeSchedule` and consumed thereafter by
every pass that walks `Pipe_` or `Mutex_` SSA values: the verifier (`ListSchedule::verify` at
`sub_8F5410`, `LoopSchedule::verify` at `sub_8F80E0`, `Schedule::verifyStageOrder` at `sub_8F87A0`),
the warp-specialisation legaliser, and the `nv_tileas`-to-`nvvm` lowering patterns that translate
each handle into an mbarrier or a token-passing sequence. The `Pipe_` / `Mutex_` IR-level view of
these values is documented in [Pipe and Mutex Value Layout](../scheduler/pipe-mutex-value-layout.md);
the scheduler that materialises them is in [Modulo Scheduler and Rau-Style
Placement](../scheduler/modulo-scheduler-and-rau.md).

The BLAKE3 intern tables are consumed by five families of IR-object dedup paths (`sub_2CC9780`,
`sub_3CC1560`, `sub_3CC1E30`, `sub_3CC2680`, `sub_3C92D50`, `sub_3CC6B10`) that all key on the same
8-byte truncated digest and reuse the same probe machinery documented in
[Container Fingerprints](container-fingerprints.md).

## Cross-References

[Storage Uniquer and Context Implementation](storage-uniquer-and-context-impl.md) describes the
canonical two-level uniquing model that the BLAKE3 intern tables fit into.
[Pipe and Mutex Value Layout](../scheduler/pipe-mutex-value-layout.md) is the IR-level view of the
SSA values that `AsyncValueImpl` backs.
[Modulo Scheduler and Rau-Style Placement](../scheduler/modulo-scheduler-and-rau.md) documents the
scheduler that owns the `AsyncValueImpl` instances and drives `Pipe::emitPayload`.
[Operation Layout](operation-layout.md) describes the `mlir::Operation` pointer that the DenseMaps
inside `AsyncValueImpl` key on.
[Container Fingerprints](container-fingerprints.md) catalogues the open-addressing probes that the
BLAKE3 digest feeds into.
