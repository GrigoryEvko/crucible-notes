# NVVM Container Binary Format

The NVVM container is a proprietary binary envelope that wraps LLVM bitcode with compiler metadata for transport between pipeline stages in cicc v13.0. It carries target architecture, optimization options, fast-math flags, memory window configurations, per-kernel resource tables, and the IR payload itself -- all in a single serializable blob. Two serialization paths exist: a compact binary wire format used in production (`nvcc` / `ptxas` pipelines) and an XML-based format used for debugging and interchange. This page specifies the binary format in sufficient detail to write a conformant parser and serializer.

The format is implemented across 26 functions in the `0xCCBB10`--`0xCDD2D0` address range (Cluster C in the binary layout). The six top-level entry points:

| Function | Address | Size | Role |
|----------|---------|------|------|
| `NvvmContainer_serialize` | `0xCDD2D0` | 47,540 B | Binary + XML serializer |
| `NvvmContainer_deserialize_options` | `0xCD1D80` | 51,859 B | Binary tag/value decoder |
| `NvvmContainer_parse_header` | `0xCDCA30` | 10,206 B | XML path header parser |
| `NvvmContainer_check_versions` | `0xCD41B0` | 16,708 B | Version compatibility gate |
| `NvvmContainer_validate_versions` | `0xCCD5F0` | 8,987 B | Standalone version validator |
| `NvvmContainer_init_options_struct` | `0xCCBB10` | small | Zero-init 248-byte container struct |

Supporting parsers called from `NvvmOptions_parse_compile_options` (`0xCDB4D0`, 26,643 bytes):

| Function | Address | Size | Role |
|----------|---------|------|------|
| `NvvmOptions_parse_arch_enum` | `0xCD09E0` | 14,516 B | ArchVariant enum string-to-int |
| `NvvmOptions_parse_fast_math` | `0xCCF590` | 12,771 B | FastMathOptions sub-structure |
| `NvvmOptions_parse_multi_view` | `0xCD6D20` | 12,188 B | MultiViewOptions sub-structure |
| `NvvmOptions_parse_cb_reserved_area` | `0xCCE780` | 9,802 B | CB reserved area config |
| `NvvmOptions_parse_reg_targets` | `0xCD7CE0` | 9,542 B | Register target config |
| `NvvmOptions_parse_serialize_helper` | `0xCD58A0` | 9,579 B | Option serialization helper |
| `NvvmOptions_parse_shader_const_iface` | `0xCCEEA0` | 8,355 B | ShaderConstIface (DCI) |
| `NvvmOptions_parse_align_entries` | `0xCD8610` | 6,739 B | Alignment entry config |
| `NvvmOptions_parse_pgo_section` | `0xCD02C0` | 5,482 B | PGO configuration |
| `NvvmOptions_parse_section` | `0xCD5510` | 5,166 B | Nested YAML section parser |
| `NvvmOptions_parse_memory_windows` | `0xCCE100` | 5,042 B | Memory window config |
| `NvvmOptions_parse_cbank_config` | `0xCCE4B0` | 4,173 B | Constant bank config |
| `NvvmOptions_parse_bool_or_int` | `0xCCC4A0` | small | Boolean/int option parser |
| `NvvmOptions_parse_tristate` | `0xCCCFB0` | small | Tri-state option parser |
| `NvvmOptions_parse_string` | `0xCD5150` | small | String option parser |

The finalizer knobs parser (`0xCD9990`, 31,702 bytes) is called separately to ingest the full set of NVIDIA-specific backend knobs (see [NVVMPassOptions](../config/nvvm-pass-options.md)).

Binary-level helpers:

| Function | Address | Role |
|----------|---------|------|
| `NvvmContainer_write_tag_value` | `0xCD17A0` | Write one tag/value pair (called 121 times from serializer) |
| `NvvmContainer_write_blob` | `0xCD1AB0` | Write blob data + tag reference |
| `NvvmContainer_compute_crc` | `0xCCD2B0` | CRC with seeds `0x8DF5D74C`, `0xBAA56A96` |

Global state: `qword_4F87148` holds the NVVM options global state pointer, checked by many downstream consumers.

## Binary Header

Every binary container begins with a fixed 24-byte header. The header is self-describing: `HeaderSize` at offset `0x0E` stores its own length (always 24), and two size fields partition the remainder into a scalar tag region and a blob data region.

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                     Magic  (0x7F4E5C7D)                       |  0x00
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|  Ver.Major    |  Ver.Minor    | NvvmIR.Major  | NvvmIR.Minor  |  0x04
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
| NvvmDbg.Major | NvvmDbg.Minor | Llvm.Major    | Llvm.Minor    |  0x08
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|         IRLevel (u16)         |       HeaderSize (u16)        |  0x0C
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                     ScalarFieldsEnd (u32)                     |  0x10
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                      BlobDataEnd (u32)                        |  0x14
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

```c
struct NvvmContainerBinaryHeader {
    uint32_t magic;              /* 0x00: must be 0x7F4E5C7D              */
    uint8_t  version_major;      /* 0x04: container format major (1)      */
    uint8_t  version_minor;      /* 0x05: container format minor (<=0x41) */
    uint8_t  nvvm_ir_major;      /* 0x06: NVVM IR version major (2)       */
    uint8_t  nvvm_ir_minor;      /* 0x07: NVVM IR version minor (<=0x62)  */
    uint8_t  nvvm_debug_major;   /* 0x08: debug info version major (3)    */
    uint8_t  nvvm_debug_minor;   /* 0x09: debug info version minor (<=2)  */
    uint8_t  llvm_major;         /* 0x0A: LLVM version (see encoding)     */
    uint8_t  llvm_minor;         /* 0x0B: LLVM version (see encoding)     */
    uint16_t ir_level;           /* 0x0C: IRLevel enum                    */
    uint16_t header_size;        /* 0x0E: always 24 (0x0018)              */
    uint32_t scalar_fields_end;  /* 0x10: byte offset past scalar region  */
    uint32_t blob_data_end;      /* 0x14: byte offset past blob region    */
};
```

The three data regions in order:

```
[0 .. 24)                         -- Header (fixed)
[24 .. scalar_fields_end)         -- Scalar tag/value pairs
[scalar_fields_end .. blob_data_end) -- Blob data region
```

The total container size is `blob_data_end` bytes. After the blob data region, the IR payload (LLVM bitcode, optionally compressed) follows immediately.

### LLVM Version Encoding

The `llvm_major` and `llvm_minor` bytes encode the LLVM version as a combined integer: `llvm_major * 100 + llvm_minor`. For cicc v13.0 (LLVM 20), this yields `20 * 100 + 0 = 2000`. The version check compares the combined value, not the individual bytes.

### IRLevel Enum

| Value | Name | Meaning |
|-------|------|---------|
| 0 | `NVVM_IR_LEVEL_UNIFIED_AFTER_DCI` | Default: IR after Device-Code-Interface unification |
| 1 | `NVVM_IR_LEVEL_LTO` | Link-Time Optimization IR (partially optimized) |
| 2 | `NVVM_IR_LEVEL_OPTIX` | OptiX pipeline IR |

## Scalar Tag/Value Encoding

Immediately after the 24-byte header, a sequence of (tag, value) pairs encodes every container field that differs from its default value. The encoding is a variable-length scheme optimized for small values:

```
Case 1 -- value fits in 16 bits (0x0000..0xFFFE):
  [tag : int16] [value : int16]          -- 4 bytes total

Case 2 -- value needs 32 bits:
  [tag : int16] [0xFFFF : int16] [value : int32]  -- 8 bytes total

Terminator:
  [0x0000 : int16]                       -- tag 0 ends the sequence
```

All multi-byte fields are little-endian. The sentinel value `0xFFFF` in the value slot signals that a full 32-bit value follows. This means the maximum encodable 16-bit value is `0xFFFE` (65534); values of exactly `0xFFFF` or larger require the extended form.

The serializer (`sub_CD17A0`, called 121 times from `NvvmContainer_serialize`) writes each tag/value pair using this scheme. The deserializer enters a switch loop over tags 1--402, decoding each value and writing it to the appropriate offset in the deserialized container struct.

### Delta Encoding Strategy

The serializer allocates a default-initialized 440-byte Options struct and compares each field in the current Options against the corresponding default. Only fields that differ from the default are written as tag/value pairs. This makes typical containers very compact -- a standard compilation targeting SM 89 with `-O2` might emit fewer than 20 tag/value pairs, covering just `SmMajor`, `SmMinor`, `CompileMode`, and a handful of target-specific flags.

The deserializer reverses this: it allocates a default Options struct first, then overwrites individual fields as tags are encountered. Unknown tags are silently skipped, which is the mechanism that provides forward compatibility -- a newer serializer can emit tags that an older deserializer simply ignores.

## Blob Data Region

Tags in the 200+ and 400+ ranges reference variable-length data stored in the blob region. The scalar value for a blob tag is the **byte offset** into the blob region where the data begins. The blob region starts at `scalar_fields_end` bytes from the container start.

To resolve a blob reference: `blob_ptr = container_base + scalar_fields_end + offset_value`.

Blob entries do not carry explicit length fields in the tag/value stream. The deserializer knows each blob type's expected size from the tag ID (e.g., tag 201 is always 24 bytes, tag 203 is always 40 bytes). Variable-length blobs like strings (tags 209, 210, 213, 216, 217) are null-terminated. Length-prefixed blobs (tag 218) carry a 4-byte length prefix.

## Complete Tag Table

144 distinct tag IDs organized into six ranges. The "Offset" column refers to the byte position within the deserialized 440-byte Options struct.

### Range 1--39: Core Scalar Options

| Tag | Type | Name | Options Offset | Notes |
|-----|------|------|----------------|-------|
| 1 | int32 | `SmMajor` | +0 (ArchVariant) | SM major version (e.g., 8 for SM 89) |
| 2 | int32 | `SmMinor` | +0 (ArchVariant) | SM minor version (e.g., 9 for SM 89) |
| 3 | int32 | `NumRegs` | +216 | Register count hint |
| 4 | int32 | `NumBarriers` | +220 | Barrier count |
| 5 | int32 | `SharedMemorySize` | +224 | Shared memory size in bytes |
| 6 | int32 | `VertexMode` | +72 | See VertexMode enum |
| 7 | bit | `ReserveLocalAddressZero` | +20 bit 0 | Reserve address 0 in local memory |
| 8 | bit | `FastMath.IgnoreInf` | +200 bit 0 | Treat infinities as NaN |
| 9 | bit | `FastMath.IgnoreNaN` | +200 bit 1 | Assume no NaN values present |
| 10 | bit | `FastMath.IgnoreSignedZero` | +200 bit 2 | Ignore sign of zero |
| 11 | bit | `FastMath.ReorderFloat` | +200 bit 3 | Allow float reordering |
| 12 | bit | `FastMath.ReorderHalf` | +200 bit 4 | Allow half-precision reordering |
| 13 | bit | `FastMath.Ftz` | +200 bit 5 | Flush denormals to zero |
| 14 | bit | `FastMath.FastSqrt` | +200 bit 6 | Use fast sqrt approximation |
| 15 | bit | `FastMath.Fmad` | +200 bit 7 | Allow fused multiply-add |
| 16 | bit | `FastMath.AllowRcpRsqToSqrt` | +201 bit 0 | Allow `rcp(rsqrt(x))` to `sqrt(x)` |
| 17 | bit | `FastMath.CanReorderFloatDistribute` | +201 bit 1 | Allow distributive reordering |
| 18 | int32 | `FastMath.Reserved` | +204 | Reserved fast-math field |
| 19 | int32 | `MaxRRegsAllowed` | +216 | Maximum registers per thread (primary) |
| 20 | int32 | `SchedRegTarget` | +220 | Scheduling register pressure target |
| 21 | int32 | `UnrollControl` | +224 | Unroll factor control |
| 22 | bool | `AcceleratedArch` | +232 | True for `sm_XXa` variants |
| 23 | bool | `StdELF` | +233 | Use standard ELF output format |
| 24 | int32 | `MaxRRegsAllowed2` | +216 | Secondary max-regs (override) |
| 25 | int32 | `SchedRegTarget2` | +220 | Secondary sched target |
| 26 | bit | `FastMath.ReassociateFloatAddOverMad` | +201 bit 2 | Float add reassociation over MAD |
| 27 | bit | `ForceImmediateConstants` | +20 bit 1 | Force immediate constant loading |
| 28 | bit | `HideFunctions` | +20 bit 2 | Hide internal functions from output |
| 29 | bit | `UseDX10AddressInRange` | +20 bit 3 | DX10 address range mode |
| 30 | int32 | `UnrollControl2` | +224 | Secondary unroll control |
| 31 | bit | `FastMath.NoFloatMAD` | +201 bit 3 | Disable float MAD formation |
| 32 | bool | `AcceleratedArch2` | +232 | Secondary accelerated-arch flag |
| 33 | bit | `FastMath.LaxFP16ApproximateDivision` | +201 bit 4 | Lax FP16 approximate division |
| 34 | bool | `StdELF2` | +233 | Secondary StdELF |
| 35 | int32 | `ShaderCodegenSelMask` | +236 | Shader codegen selection bitmask |
| 36 | bool | `OmegaPtxErrorHandling` | +240 | Enable Omega-style PTX error handling |
| 37 | int32 | `FDLInsertMode` | +244 | See FDLInsertMode enum |
| 38 | bit | `IsPIC` | +20 bit 4 | Position-independent code flag |
| 39 | bit | `NoSpillsConstraint` | +20 bit 5 | Hard constraint: no register spills |

### Tag 99: Compression Metadata

| Tag | Type | Name | Notes |
|-----|------|------|-------|
| 99 | int32 | `CompressAlgoId` | Compression algorithm selector for IR payload |

When present, the IR payload following the blob region is compressed. The value selects a codec via `sub_16886D0(algo_id)`. If the value is 0, the runtime substitutes the default algorithm ID `0x75D49913` (1,977,119,507 decimal). The codec is a pluggable compression/encryption layer accessed through four function pointers:

```c
/* Compression codec API (addresses in the 0x1688xxx range) */
void *codec_acquire(uint32_t algo_id);            /* sub_16886D0 */
int   codec_compress(void *codec, void *data,
                     size_t size);                 /* sub_1688730 */
int   codec_decompress(void *codec, void *data,
                       size_t size);               /* sub_16887A0 */
void  codec_release(void *codec);                  /* sub_1688720 */
```

The write path in `NvvmContainer_serialize` (`0xCDD2D0`) compresses the LLVM bitcode payload via `sub_C8D290`, then computes a CRC hash via `NvvmContainer_compute_crc` (`0xCCD2B0`) with the two seed values `-1914584148` (`0x8DF5D74C`) and `-1162247642` (`0xBAA56A96`). The CRC value is stored as the `CompressAlgoId` tag 99 value, which doubles as an integrity check token: the deserializer uses the same CRC seeds to verify the payload before decompression.

The compression subsystem lives outside the main container cluster at addresses `0x16886D0`--`0x16887A0`, in the utility library region of the binary.

### Range 101--173: Extended Target Options

These tags configure per-kernel and target-specific hardware parameters. Most map into a sub-structure accessed through the Options struct. The "Byte.Bit" column indicates the packed bitfield location within the target options sub-structure.

| Tag | Type | Name | Location | Notes |
|-----|------|------|----------|-------|
| 101 | bool | `HasTextureOps` | offset 0 | Target supports texture operations |
| 102 | bool | `HasSurfaceOps` | offset 0 | Target supports surface operations |
| 103 | bool | `HasAtomics` | offset 0 | Target supports atomic operations |
| 104 | bool | `HasVote` | offset 0 | Target supports warp vote intrinsics |
| 105 | int32 | `MaxThreadsPerBlock` | offset 4 | Maximum CTA thread count |
| 106 | byte | `PreferL1SizeFlag` | offset 8 | L1 cache vs shared memory preference |
| 107 | bool | `HasWarpShuffle` | offset 0 | Target supports warp shuffle |
| 108 | bool | `HasFunnelShift` | offset 0 | Target supports funnel shift |
| 109 | int32 | `CBankOfstLow` | offset 12 | Constant bank offset lower bound |
| 110 | int32 | `CBankOfstHi` | offset 16 | Constant bank offset upper bound |
| 111 | int32 | `CBankSize` | offset 20 | Constant bank size in bytes |
| 112 | bit | `Bit0_68` | byte 68, bit 0 | Target capability flag |
| 113 | bit | `Bit1_68` | byte 68, bit 1 | Target capability flag |
| 114 | bit | `Bit2_68` | byte 68, bit 2 | Target capability flag |
| 115 | bit | `Bit3_68` | byte 68, bit 3 | Target capability flag |
| 116 | bit | `Bit4_68` | byte 68, bit 4 | Target capability flag |
| 117 | bit | `Bit5_68` | byte 68, bit 5 | Target capability flag |
| 118 | bit | `Bit7_68` | byte 68, bit 7 | Target capability flag (bit 6 skipped) |
| 119 | bit | `EnableCoalesce` | byte 69, bit 0 | Enable memory coalescing optimization |
| 120 | bit | `EnableVectorize` | byte 69, bit 2 | Enable auto-vectorization |
| 121 | 2-bit | `CompactionMode` | byte 69, bits 3--4 | Thread compaction strategy (0--3) |
| 122 | int32 | `StackFrameSize` | offset 96 | Stack frame size in bytes |
| 123 | int32 | `StackAlignment` | offset 100 | Stack alignment requirement |
| 124 | int32 | `ParamSpaceSize` | offset 104 | Parameter space size |
| 125 | int32 | `ParamAlignment` | offset 108 | Parameter space alignment |
| 126 | int32 | `LocalMemSize` | offset 116 | Local memory size per thread |
| 127 | int32 | `SharedBankConfig` | offset 156 | Shared memory bank configuration |
| 128 | int32 | `MinGridSize` | offset 248 | Minimum grid size for occupancy |
| 129 | int32 | `MaxGridDimX` | offset 252 | Maximum X-dimension grid size |
| 130 | int32 | `SharedMemPerBlock` | offset 264 | Shared memory per block |
| 131 | 2-bit | `WarpScheduleMode` | byte 70, bits 0--1 | Warp scheduling strategy |
| 132 | bit | `EnablePrefetch` | byte 70, bit 2 | Enable memory prefetch instructions |
| 133 | bit | `Bit4_70` | byte 70, bit 4 | Target capability flag |
| 134 | bit | `Bit5_70` | byte 70, bit 5 | Target capability flag |
| 135 | bit | `Bit6_70` | byte 70, bit 6 | Target capability flag |
| 136 | bit | `Bit7_70` | byte 70, bit 7 | Target capability flag |
| 137 | int32 | `MaxDynShared` | offset 268 | Maximum dynamic shared memory |
| 138 | bool | `HasLDG` | offset 5 | Target supports LDG instruction |
| 139 | bit | `Bit1_71` | byte 71, bit 1 | Target capability flag |
| 140 | bit | `Bit2_71` | byte 71, bit 2 | Target capability flag |
| 141 | bool | `HasBarrierReduce` | offset 40 | Target supports barrier-reduce |
| 142 | int32 | `CacheConfig` | offset 280 | Cache configuration selector |
| 143 | bit | `Bit6_68` | byte 68, bit 6 | Target capability flag |
| 144 | bit | `Bit3_71` | byte 71, bit 3 | Target capability flag |
| 145 | bit | `Bit0_71` | byte 71, bit 0 | Target capability flag |
| 146 | int32 | `ConstBankSize` | offset 256 | Constant bank total size |
| 147 | int32 | `ShMemBankStride` | offset 152 | Shared memory bank stride |
| 148 | 2-bit | `ScheduleMode2` | byte 71, bits 4--5 | Secondary scheduling mode |
| 149 | bit | `Bit6_71` | byte 71, bit 6 | Target capability flag |
| 150 | bit | `Bit7_71` | byte 71, bit 7 | Target capability flag |
| 151 | int32 | `LocalMemAlignment` | offset 112 | Local memory alignment |
| 152 | bit | `EnableBarrierOpt` | byte 69, bit 5 | Enable barrier optimization |
| 153 | bit | `Bit0_72` | byte 72, bit 0 | Target capability flag |
| 154 | bit | `Bit6_69` | byte 69, bit 6 | Target capability flag |
| 155 | bit | `Bit7_69` | byte 69, bit 7 | Target capability flag |
| 156 | bit | `Bit1_72` | byte 72, bit 1 | Target capability flag |
| 157 | bool | `HasDP4A` | offset 1 | Target supports DP4A dot-product |
| 158 | bit | `Bit3_72` | byte 72, bit 3 | Target capability flag |
| 159 | int32 | `ConstBankSize2` | offset 260 | Secondary constant bank size |
| 160 | int32 | `MaxRegsPerThread` | offset 284 | Hard limit on registers per thread |
| 161 | int32 | `ClusterSize` | offset 276 | Thread block cluster size (SM 90+) |
| 162 | bit | `Bit4_72` | byte 72, bit 4 | Target capability flag |
| 163 | bit | `Bit5_72` | byte 72, bit 5 | Target capability flag |
| 164 | bit | `Bit6_72` | byte 72, bit 6 | Target capability flag |
| 165 | bit | `Bit7_72` | byte 72, bit 7 | Target capability flag |
| 166 | int32 | `MaxCTAPerSM` | offset 160 | Maximum CTAs per SM |
| 167 | int32 | `TexIndirectLimit` | offset 272 | Texture indirect access limit |
| 168 | bit | `Bit0_432` | byte 432, bit 0 | Extended capability flag |
| 169 | bit | `Bit1_432` | byte 432, bit 1 | Extended capability flag |
| 170 | bit | `Bit2_432` | byte 432, bit 2 | Extended capability flag |
| 171 | bool | `HasTMAOps` | offset 289 | Target supports TMA operations (SM 90+) |
| 172 | bit | `Bit3_70` | byte 70, bit 3 | Target capability flag |
| 173 | bool | `HasTCGen05` | offset 290 | Target supports TCGen05 (SM 100+) |

### Range 201--218: Blob Data Tags

| Tag | Size | Name | Description |
|-----|------|------|-------------|
| 201 | 24 B | `MemoryWindowCBank` | 3 memory window entries for constant bank (see below) |
| 202 | 24 B | `MemoryWindowLocal` | 3 memory window entries for local memory |
| 203 | 40 B | `MemoryWindowShared` | 10 x `uint32_t` for shared memory windows + flags |
| 204 | 48 B | `MultiViewOptions` | Multi-view rendering header + typed arrays |
| 205 | var | `TargetResourceTable` | 24-byte header + 36 bytes per entry |
| 206 | var | `PerKernelCBankOffsets` | 4-byte count + 4 bytes per kernel |
| 207 | var | `PerKernelStackSizes` | 4-byte count + 4 bytes per kernel |
| 208 | var | `PerKernelSMEMSizes` | 8-byte count + 8 bytes per kernel |
| 209 | var | `TargetFuncName` | Null-terminated string |
| 210 | var | `TargetEntryName` | Null-terminated string |
| 211 | 8 B | `PerKernelQWORD` | 8-byte per-kernel datum |
| 212 | 12 B | `ExtraMemParams` | 8 + 4 bytes of memory parameters |
| 213 | var | `AuxString1` | Null-terminated auxiliary string |
| 214 | var | `PerKernelRegisters` | 4-byte count + 4 bytes per kernel |
| 215 | var | `PerKernelBarriers` | 4-byte count + 4 bytes per kernel |
| 216 | var | `AuxString2` | Null-terminated auxiliary string |
| 217 | var | `AuxString3` | Null-terminated auxiliary string |
| 218 | var | `AuxByteArray` | 4-byte length prefix + raw bytes |

### Range 301--309: Extended Int32 Fields

| Tag | Type | Name | Options Offset | Notes |
|-----|------|------|----------------|-------|
| 301 | int32 | `ExtOpt.Field344` | +344 | Cluster/group configuration selector |
| 302 | int32 | `ExtOpt.Field348` | +348 | Extended option |
| 303 | int32 | `ExtOpt.Field352` | +352 | Extended option |
| 304 | int32 | `ExtOpt.Field356` | +356 | Extended option |
| 305 | int32 | `ExtOpt.Field360` | +360 | Extended option |
| 306 | int32 | `ExtOpt.Field400` | +400 | Extended option |
| 307 | int32 | `ExtOpt.Field364` | +364 | Extended option |
| 308 | int32 | `ExtOpt.Field368` | +368 | Extended option |
| 309 | int32 | `ExtOpt.Field372` | +372 | Extended option |

### Range 351--353: Extended Int64 Blob References

| Tag | Size | Name | Options Offset |
|-----|------|------|----------------|
| 351 | 8 B | `ExtOpt.QWord376` | +376 |
| 352 | 8 B | `ExtOpt.QWord384` | +384 |
| 353 | 8 B | `ExtOpt.QWord392` | +392 |

### Range 401--402: Structured Blob Data

These tags are conditionally parsed based on the value of tag 301 (`ExtOpt.Field344`):

| Tag | Condition | Size | Name | Notes |
|-----|-----------|------|------|-------|
| 401 | `Field344 == 1` | 56+ B | `TMADescriptor` | SM 90 Hopper TMA bulk-copy descriptors. 44-byte fixed header + 16 bytes per entry. |
| 402 | `Field344 == 4` | 40+ B | `TCGen05Config` | SM 100 Blackwell TCGen05 tensor configurations. 32-byte fixed header + 12 bytes per entry. |

The conditional parsing means a single container cannot carry both TMA and TCGen05 data -- the `Field344` value selects which hardware generation's tensor memory interface is active.

#### TMADescriptor Layout (Tag 401, Field344 == 1)

TMA (Tensor Memory Access) descriptors configure `cp.async.bulk` operations on SM 90 Hopper. The TMA descriptor extraction is performed by `sub_9483E0` during intrinsic lowering. The blob layout:

```c
struct TMADescriptor {
    /* +0  */ uint32_t num_entries;          /* Number of TMA descriptors     */
    /* +4  */ uint32_t dimensionality;       /* 1d..5d tensor rank            */
    /* +8  */ uint32_t element_size;         /* Bytes per element             */
    /* +12 */ uint32_t interleave_layout;    /* Memory interleave pattern     */
    /* +16 */ uint32_t swizzle_mode;         /* Swizzle mode selector         */
    /* +20 */ uint32_t fill_mode;            /* Out-of-bounds fill behavior   */
    /* +24 */ uint32_t [5] global_dims;      /* Global tensor dimensions      */
    /* +44 */ /* --- 16 bytes per entry --- */
    /*        uint32_t box_dim;              Per-entry box dimension          */
    /*        uint32_t stride;               Per-entry stride                 */
    /*        uint32_t elem_stride;          Per-entry element stride         */
    /*        uint32_t reserved;             Reserved/padding                 */
};
```

See [SM 90 Hopper](../targets/sm90-hopper.md) for the TMA instruction format and the `cp.async.bulk.tensor.g2s.tile.{1d,2d,3d,4d,5d}` intrinsic family.

#### TCGen05Config Layout (Tag 402, Field344 == 4)

TCGen05 (Tensor Core Generation 5) configurations describe Blackwell SM 100 tensor memory operations. The TCGen05 instruction set includes `tcgen05.alloc`, `tcgen05.dealloc`, `tcgen05.commit`, `tcgen05.fence`, `tcgen05.wait`, and `tcgen05.relinquish.alloc` -- all gated by the SM 100 arch-conditional check at `sub_30462A0`. The blob layout:

```c
struct TCGen05Config {
    /* +0  */ uint32_t num_entries;          /* Number of TCGen05 configs     */
    /* +4  */ uint32_t accumulator_size;     /* Accumulator memory size       */
    /* +8  */ uint32_t commit_mode;          /* Commit mode (multicast flags) */
    /* +12 */ uint32_t fence_mode;           /* Fence mode selector           */
    /* +16 */ uint32_t [4] reserved;         /* Reserved fields               */
    /* +32 */ /* --- 12 bytes per entry --- */
    /*        uint32_t config_id;            TCGen05 config identifier        */
    /*        uint32_t fragment_count;       Number of fragments              */
    /*        uint32_t flags;                Per-config flags                 */
};
```

See [SM 100 Blackwell](../targets/sm100-blackwell.md) for the TCGen05 instruction set and the `tcgen05.*` intrinsic family.

## Deserialized Container Struct

After parsing, the container is represented as a 248-byte in-memory structure allocated by `NvvmContainer_init_options_struct` (`0xCCBB10`). This struct holds the container metadata plus a pointer to the full 440-byte Options struct.

```c
struct NvvmContainerHeader {          /* 248 bytes total                   */
    /* 0x00 */ uint32_t sm_major;     /* Tag 1: SM major version           */
    /* 0x04 */ uint32_t sm_minor;     /* Tag 2: SM minor version           */
    /* 0x08 */ uint32_t num_regs;     /* Tag 3                             */
    /* 0x0C */ uint32_t num_barriers; /* Tag 4                             */
    /* 0x10 */ uint32_t shared_mem_size; /* Tag 5                          */
    /* 0x14 */ uint8_t  flags_14;     /* Packed bits: tags 7,27,28,29,38,39*/
    /*         bit 0: ReserveLocalAddressZero  (tag 7)                     */
    /*         bit 1: ForceImmediateConstants  (tag 27)                    */
    /*         bit 2: HideFunctions            (tag 28)                    */
    /*         bit 3: UseDX10AddressInRange     (tag 29)                   */
    /*         bit 4: IsPIC                    (tag 38)                    */
    /*         bit 5: NoSpillsConstraint       (tag 39)                   */
    /* 0x15 */ uint8_t  _pad15[3];
    /* 0x18 */ uint8_t  multi_view_options[48]; /* Tag 204 blob            */
    /* 0x48 */ uint32_t vertex_mode;  /* Tag 6                             */
    /* 0x4C */ uint8_t  _pad4c[4];
    /* 0x50 */ uint32_t max_rregs;    /* Tag 19                            */
    /* 0x54 */ uint32_t sched_reg_target; /* Tag 20                        */
    /* 0x58 */ uint32_t unroll_control; /* Tag 21                          */
    /* 0x5C */ uint8_t  _pad5c[4];
    /* 0x60 */ uint8_t  mem_win_cbank[24];  /* Tag 201 blob                */
    /* 0x78 */ uint8_t  mem_win_local[24];  /* Tag 202 blob                */
    /* 0x90 */ uint8_t  mem_win_shared[40]; /* Tag 203 blob                */
    /* 0xB8 */ uint8_t  _padb8[12];
    /* 0xC4 */ uint8_t  accelerated_arch; /* Tag 22                        */
    /* 0xC5 */ uint8_t  std_elf;      /* Tag 23                            */
    /* 0xC6 */ uint8_t  _padc6[2];
    /* 0xC8 */ uint8_t  fast_math[8]; /* Tags 8-17,26,31,33 bitfields     */
    /* 0xD0 */ uint8_t  _padd0[8];
    /* 0xD8 */ uint32_t max_rregs_2;  /* Tag 24                            */
    /* 0xDC */ uint32_t sched_reg_2;  /* Tag 25                            */
    /* 0xE0 */ uint32_t unroll_ctl_2; /* Tag 30                            */
    /* 0xE4 */ uint32_t compress_algo_id; /* Tag 99                        */
    /* 0xE8 */ uint8_t  omega_ptx_err; /* Tag 32                           */
    /* 0xE9 */ uint8_t  std_elf_2;    /* Tag 34                            */
    /* 0xEA */ uint8_t  _padea[2];
    /* 0xEC */ uint32_t shader_cg_sel; /* Tag 35                           */
    /* 0xF0 */ uint8_t  fdl_bit;      /* Tag 36                            */
    /* 0xF1 */ uint8_t  _padf1[3];
    /* 0xF4 */ uint32_t fdl_insert_mode; /* Tag 37                         */
};
/* sizeof(NvvmContainerHeader) == 248 (0xF8) */
```

The Options pointer is stored at offset 208 (0xD0) of the container header during deserialization -- the container header acts as both a data holder and an index into the full Options struct.

## Options Struct (440 bytes)

The full compiler options structure is allocated separately and linked from the container header. It is parsed by `NvvmOptions_parse_compile_options` (`0xCDB4D0`, 26,643 bytes) in the XML path, or populated field-by-field from tags in the binary path.

```c
struct NvvmOptions {                   /* 440 bytes total                  */
    /* +0   */ uint32_t arch_variant;  /* ArchVariant enum                 */
    /* +4   */ uint32_t compile_mode;  /* CompileMode enum                 */
    /* +8   */ uint32_t opt_level;     /* OptLevel enum                    */
    /* +12  */ uint32_t debug_info;    /* DebugInfo enum                   */
    /* +16  */ uint32_t client_version;
    /* +20  */ uint8_t  flags_20;      /* Packed booleans: 6 bits          */
    /*          bit 0: ReserveLocalAddressZero                             */
    /*          bit 1: ForceImmediateConstants                             */
    /*          bit 2: HideFunctions                                       */
    /*          bit 3: UseDX10AddressInRange                               */
    /*          bit 4: IsPIC                                               */
    /*          bit 5: NoSpillsConstraint                                  */
    /* +21  */ uint8_t  _pad21[3];
    /* +24  */ uint8_t  multi_view[48]; /* MultiViewOptions sub-structure  */
    /* +72  */ uint32_t vertex_mode;    /* VertexMode enum                 */
    /* +76  */ uint8_t  _pad76[4];
    /* +80  */ uint8_t  dci_info[120];  /* DCIInfo sub-structure           */
    /* +200 */ uint8_t  fast_math_byte0; /* FastMath bits 0-7              */
    /* +201 */ uint8_t  fast_math_byte1; /* FastMath bits 8-12             */
    /* +202 */ uint8_t  _pad202[2];
    /* +204 */ uint32_t fast_math_reserved;
    /* +208 */ uint8_t  _pad208[8];
    /* +216 */ uint32_t max_rregs_allowed;
    /* +220 */ uint32_t sched_reg_target;
    /* +224 */ uint32_t unroll_control;
    /* +228 */ uint32_t okey;           /* CompressAlgoId / OKey           */
    /* +232 */ uint8_t  accelerated_arch;
    /* +233 */ uint8_t  std_elf;
    /* +234 */ uint8_t  _pad234[2];
    /* +236 */ uint32_t shader_codegen_sel_mask;
    /* +240 */ uint8_t  omega_ptx_error_handling;
    /* +241 */ uint8_t  _pad241[3];
    /* +244 */ uint32_t fdl_insert_mode;
    /* +248 */ uint8_t  target_opts[192]; /* Extended target options (tags 101-173) */
};
/* sizeof(NvvmOptions) == 440 (0x1B8) */
```

### DCIInfo Sub-Structure (Options +80, 120 bytes)

The Device-Code-Interface sub-structure at offset +80 contains the shader constant interface and constant bank reserved area configurations. Parsed by `NvvmOptions_parse_shader_const_iface` (`0xCCEEA0`, 8,355 bytes) and `NvvmOptions_parse_cb_reserved_area` (`0xCCE780`, 9,802 bytes).

**ShaderConstIface XML fields** (from `sub_CCEEA0`):

| Field | Type | Description |
|-------|------|-------------|
| `OptimizerConstBank` | int32 | Constant bank index used by the optimizer |
| `DriverConstBank` | int32 | Constant bank index used by the driver |
| `BindlessTextureBank` | int32 | Constant bank for bindless texture handles |
| `LocalMemoryWindow` | struct | Memory window config for local memory |
| `SharedMemoryWindow` | struct | Memory window config for shared memory |
| `VectorizeAndRemapTLD` | bool | Enable vectorization and TLD remapping |
| `ELFControlsDCI` | bool | ELF controls DCI interface layout |
| `DiscardDefaultValueOutputs` | bool | Discard outputs that match default values |

**CBReservedArea XML fields** (from `sub_CCE780`):

| Field | Type | Description |
|-------|------|-------------|
| `ByteOffsetToEndOfReservedArea` | int32 | End-of-reserved-area offset in constant bank |
| `CbAddressBitsInReservedVABase` | int32 | Address bits for reserved virtual address base |
| `CbBankToReservedVABase` | int32 | Constant bank index for reserved VA base |
| `ForceHighLatencyConstExpr` | bool | Force high-latency constant expression evaluation |
| `ReservedCbReadBank` | int32 | Reserved constant bank read bank index |

### MultiViewOptions Sub-Structure (Options +24, 48 bytes)

The multi-view rendering options sub-structure at offset +24 carries graphics pipeline multi-view configuration. Parsed by `NvvmOptions_parse_multi_view` (`0xCD6D20`, 12,188 bytes). Serialized as blob tag 204.

| Field | Type | Description |
|-------|------|-------------|
| `NumViews` | int32 | Number of rendering views |
| `NominalViewIDs` | int32[] | Array of nominal view identifiers |
| `PerViewRTIndexConstants` | int32[] | Per-view render target index constants |
| `EnableViewInstanceMask` | bool | Enable per-view instance masking |
| `ComputePerPatchAttribsForViewZero` | bool | Compute per-patch attributes for view 0 |
| `IsImplicit` | bool | Implicit multi-view mode |

### CompileMode Enum

| Value | Name | Meaning |
|-------|------|---------|
| 0 | `NVVM_COMPILE_MODE_WHOLE_PROGRAM_ABI` | Whole-program with ABI compliance |
| 1 | `NVVM_COMPILE_MODE_WHOLE_PROGRAM_NOABI` | Whole-program without ABI (internal) |
| 2 | `NVVM_COMPILE_MODE_SEPARATE_ABI` | Separate compilation (relocatable, `--device-c`) |
| 3 | `NVVM_COMPILE_MODE_EXTENSIBLE_WHOLE_PROGRAM_ABI` | Extensible whole-program with ABI |

### OptLevel Enum

| Value | Name |
|-------|------|
| 0 | `NVVM_OPT_LEVEL_NONE` |
| 1 | `NVVM_OPT_LEVEL_1` |
| 2 | `NVVM_OPT_LEVEL_2` (default) |
| 3 | `NVVM_OPT_LEVEL_3` |

### DebugInfo Enum

| Value | Name |
|-------|------|
| 0 | `NVVM_DEBUG_INFO_NONE` (default) |
| 1 | `NVVM_DEBUG_INFO_LINE_INFO` |
| 2 | `NVVM_DEBUG_INFO_DWARF` |

### VertexMode Enum

| Value | Name |
|-------|------|
| 0 | `NVVM_VERTEX_MODE_SINGLE` |
| 1 | `NVVM_VERTEX_MODE_A` |
| 2 | `NVVM_VERTEX_MODE_B` |
| 3 | `NVVM_VERTEX_MODE_AB` |

### FDLInsertMode Enum

| Value | Name |
|-------|------|
| 0 | `NVVM_FDL_MODE_NONE` |
| 1 | `NVVM_FDL_MODE_ALL` |
| 2 | `NVVM_FDL_MODE_APP` |

## ArchVariant Enum

The architecture enum uses a numeric encoding where the value equals `major * 10 + minor` for older architectures and `major * 10 + minor` (with 3-digit major) for Blackwell. There are two parallel enum spaces: "virtual" architecture variants (used for `compute_XX` targets) and "HW" variants (used for `sm_XX` real silicon targets). The virtual variants are serialized by name in the XML format via `NvvmOptions_parse_arch_enum` (`0xCD09E0`, 14,516 bytes).

### Virtual Architecture Variants

| Enum Name | Numeric Value | Generation | SM |
|-----------|---------------|------------|-----|
| `NVVM_ARCH_KEPLER_3_0` | 30 | Kepler | 3.0 |
| `NVVM_ARCH_KEPLER_3_2` | 32 | Kepler | 3.2 |
| `NVVM_ARCH_KEPLER_3_5` | 35 | Kepler | 3.5 |
| `NVVM_ARCH_KEPLER_3_7` | 37 | Kepler | 3.7 |
| `NVVM_ARCH_MAXWELL_5_0` | 50 | Maxwell | 5.0 |
| `NVVM_ARCH_MAXWELL_5_2` | 52 | Maxwell | 5.2 |
| `NVVM_ARCH_MAXWELL_5_3` | 53 | Maxwell | 5.3 |
| `NVVM_ARCH_PASCAL_6_0` | 60 | Pascal | 6.0 |
| `NVVM_ARCH_PASCAL_6_1` | 61 | Pascal | 6.1 |
| `NVVM_ARCH_PASCAL_6_2` | 62 | Pascal | 6.2 |
| `NVVM_ARCH_VOLTA_7_0` | 70 | Volta | 7.0 |
| `NVVM_ARCH_VOLTA_7_2` | 72 | Volta | 7.2 |
| `NVVM_ARCH_TURING_7_3` | 73 | Turing | 7.3 |
| `NVVM_ARCH_TURING_7_5` | 75 | Turing | 7.5 |
| `NVVM_ARCH_AMPERE_8_0` | 80 | Ampere | 8.0 |
| `NVVM_ARCH_AMPERE_8_2` | 82 | Ampere | 8.2 |
| `NVVM_ARCH_AMPERE_8_6` | 86 | Ampere | 8.6 |
| `NVVM_ARCH_AMPERE_8_7` | 87 | Ampere | 8.7 |
| `NVVM_ARCH_AMPERE_8_8` | 88 | Ampere | 8.8 |
| `NVVM_ARCH_ADA_8_9` | 89 | Ada Lovelace | 8.9 |
| `NVVM_ARCH_HOPPER_9_0` | 90 | Hopper | 9.0 |
| `NVVM_ARCH_BLACKWELL_10_0` | 100 | Blackwell | 10.0 |
| `NVVM_ARCH_BLACKWELL_10_1` | 101 | Blackwell | 10.1 |
| `NVVM_ARCH_BLACKWELL_10_3` | 103 | Blackwell | 10.3 |
| `NVVM_ARCH_BLACKWELL_11_0` | 110 | Blackwell (Jetson Thor) | 11.0 |
| `NVVM_ARCH_BLACKWELL_12_0` | 120 | Blackwell (RTX 50xx / Pro) | 12.0 |
| `NVVM_ARCH_BLACKWELL_12_1` | 121 | Blackwell (DGX Spark) | 12.1 |

Note: `NVVM_ARCH_BLACKWELL_10_1` maps to `__CUDA_ARCH` 1010, while `NVVM_ARCH_BLACKWELL_11_0` maps to `__CUDA_ARCH` 1100. Despite both being in the `BLACKWELL` family, they are distinct architectures with separate entries in the processor table. sm_110 (Jetson Thor) was originally designated sm_101 before being renumbered to its own 11.x line.

### HW Architecture Variants

The HW variants use a `major * 1000 + minor * 10` encoding for their internal numeric values. These map to real silicon rather than virtual compute capabilities:

| Enum Name | Internal Value | Notes |
|-----------|---------------|-------|
| `NVVM_ARCH_HW_SM_5_0` | 500 | Maxwell HW baseline |
| ... | ... | One entry per supported HW SM through 9.0 |
| `NVVM_ARCH_HW_SM_10_0` | 1000 | Blackwell datacenter |
| `NVVM_ARCH_HW_SM_10_1` | 1010 | Blackwell Ultra (GB300) |
| `NVVM_ARCH_HW_SM_10_3` | 1030 | Blackwell variant |
| `NVVM_ARCH_HW_SM_10_4` | 1200 | Maps to SM 120 value -- not publicly documented |

The `HW_SM_10_4 = 1200` mapping is notable: SM 10.4 in the HW enum space corresponds to the SM 120 consumer architecture. This reveals that "SM 120" is internally considered a Blackwell 10.4 die variant, not a separate generation.

## FastMathOptions Bitfields

The fast-math configuration occupies two bytes at Options offset +200 and +201, with an additional int32 at +204. Each bit independently controls one floating-point relaxation.

### Byte +200 (tags 8--15)

```
  Bit 7   Bit 6   Bit 5   Bit 4   Bit 3   Bit 2   Bit 1   Bit 0
+-------+-------+-------+-------+-------+-------+-------+-------+
|  Fmad | Fast  |  Ftz  |Reorder|Reorder|Ignore | Ignore|Ignore |
|       | Sqrt  |       | Half  | Float | Sign0 |  NaN  |  Inf  |
+-------+-------+-------+-------+-------+-------+-------+-------+
  tag 15  tag 14  tag 13  tag 12  tag 11  tag 10  tag 9   tag 8
```

### Byte +201 (tags 16--17, 26, 31, 33)

```
  Bit 7   Bit 6   Bit 5   Bit 4   Bit 3   Bit 2   Bit 1   Bit 0
+-------+-------+-------+-------+-------+-------+-------+-------+
|       |       |       | Lax   | No    |Reassoc|CanReor| Allow |
|       |       |       | FP16  | Float | Float |derDist| Rcp   |
|       |       |       | Div   | MAD   |AddMAD |ribute | Rsq   |
+-------+-------+-------+-------+-------+-------+-------+-------+
                          tag 33  tag 31  tag 26  tag 17  tag 16
```

### FastMath Divide Sub-Enum

The `Divide` field within FastMathOptions is a nested enum serialized by name in the XML path:

| Value | Name | Meaning |
|-------|------|---------|
| 0 | `NVVM_FAST_MATH_DIVIDE_PRECISE_NO_FTZ` | IEEE-compliant division, no flush-to-zero |
| 1 | `NVVM_FAST_MATH_DIVIDE_PRECISE_ALLOW_FTZ` | IEEE division with FTZ permitted |
| 2 | `NVVM_FAST_MATH_DIVIDE_FULL_RANGE_APPROX` | Full-range approximation |
| 3 | `NVVM_FAST_MATH_DIVIDE_FAST_APPROX` | Fast approximation (least precise) |

These correspond to the nvcc flags `-prec-div=1` (precise) and `-prec-div=0` (fast), with FTZ interaction determined by `-ftz`.

### Complete FastMath XML Field Inventory

The full set of XML field names parsed by `NvvmOptions_parse_fast_math` (`0xCCF590`, 12,771 bytes):

| XML Field Name | Binary Tag | Type | Description |
|----------------|-----------|------|-------------|
| `IgnoreInf` | 8 | bit | Treat infinities as NaN |
| `IgnoreNaN` | 9 | bit | Assume no NaN values present |
| `IgnoreSignedZero` | 10 | bit | Ignore sign of zero |
| `ReorderFloat` | 11 | bit | Allow float reordering |
| `ReorderHalf` | 12 | bit | Allow half-precision reordering |
| `Ftz` | 13 | bit | Flush denormals to zero |
| `FastSqrt` | 14 | bit | Use fast sqrt approximation |
| `Fmad` | 15 | bit | Allow fused multiply-add |
| `AllowRcpRsqToSqrt` | 16 | bit | Allow `rcp(rsqrt(x))` to `sqrt(x)` |
| `CanReorderFloatDistribute` | 17 | bit | Allow distributive reordering |
| `ReassociateFloatAddOverMad` | 26 | bit | Float add reassociation over MAD |
| `NoFloatMAD` | 31 | bit | Disable float MAD formation |
| `LaxFP16ApproximateDivision` | 33 | bit | Lax FP16 approximate division |
| `Divide` | -- | enum | Division precision sub-enum (above) |

The `Divide` field is serialized as a nested enum element in XML; in the binary format it is encoded as part of the fast-math reserved int32 at Options +204 (tag 18).

## Memory Window Configuration

Memory windows define how the compiler maps address spaces to hardware memory banks. Three window types are serialized as blobs via tags 201--203, parsed by `NvvmOptions_parse_cbank_config` (`0xCCE4B0`) and `NvvmOptions_parse_memory_windows` (`0xCCE100`).

### MemoryWindow Type Enum

| Value | Name | Meaning |
|-------|------|---------|
| 0 | `NVVM_MEMORY_WINDOW_SPECIAL_REGISTER` | Accessed via special registers |
| 1 | `NVVM_MEMORY_WINDOW_CBANK` | Constant bank window |
| 2 | `NVVM_MEMORY_WINDOW_IMMEDIATE` | Immediate offset addressing |

### Window Entry Layout (8 bytes)

```c
struct MemoryWindowEntry {
    uint32_t window_type;   /* MemoryWindow type enum      */
    uint32_t cbank;         /* Constant bank index         */
    /* The following are part of the containing blob: */
    /* uint32_t cbank_ofst_low;  -- lower bound of offset range */
    /* uint32_t cbank_ofst_hi;   -- upper bound of offset range */
};
```

- Tag 201 (`MemoryWindowCBank`): 24 bytes = 3 entries of `{window_type, cbank, low, hi}` truncated to fit, or 3 x 8 bytes depending on sub-field packing.
- Tag 202 (`MemoryWindowLocal`): 24 bytes, same structure.
- Tag 203 (`MemoryWindowShared`): 40 bytes = 10 x `uint32_t` values encoding shared memory bank strides, offsets, and configuration flags.

## Version Compatibility Logic

Version checking is the first operation performed on a container buffer, implemented in `NvvmContainer_check_versions` (`0xCD41B0`). The logic is conservative on major versions and lenient on minor versions:

```
1. Verify magic == 0x7F4E5C7D
   Fail: return NULL (not a container)

2. Version.Major must == 1
   Fail: "NvvmContainer major version N not compatible" → return NULL

3. Version.Minor compared to 0x41 (65)
   If container minor > tool minor:
     Warning: "Linked container's NvvmContainer minor version N newer than tool"
   Parse continues regardless.

4. NvvmIRVersion.Major must == 2
   Fail: "NvvmIR major version N not compatible" → return NULL

5. NvvmIRVersion.Minor compared to 0x62 (98)
   If container minor > tool minor: warning, parse continues.

6. NvvmDebugVersion.Major must == 3
   Fail: "NvvmDebug major version N not compatible" → return NULL

7. NvvmDebugVersion.Minor compared to 2
   If container minor > tool minor: warning, parse continues.

8. LlvmVersion (major*100 + minor) must be <= 2000
   Fail: "LLVM version N not compatible" → return NULL
```

A separate standalone validator (`0xCCD5F0`) adds a mode-dependent check: in binary dump mode (`a5=0`), the LLVM version must be exactly 20; in normal mode (`a5=1`), it must be `<= 20`.

The philosophy is clear: major version bumps signal breaking format changes and are hard failures. Minor version bumps add new tags but never change existing tag semantics -- the delta encoding and unknown-tag-skipping design ensures forward compatibility.

### Current Version Constants (cicc v13.0)

| Field | Major | Minor |
|-------|-------|-------|
| Version (container format) | 1 | 0x41 (65) |
| NvvmIRVersion | 2 | 0x62 (98) |
| NvvmDebugVersion | 3 | 2 |
| LlvmVersion | 20 | 0 |

## XML Serialization Format

The XML path (`NvvmContainer_parse_header` at `0xCDCA30`) uses NVIDIA's YAML-based serialization framework with virtual dispatch. The top-level XML document contains these elements:

```xml
<NvvmContainer>
  <Version major="1" minor="65"/>
  <NvvmIRVersion major="2" minor="98"/>
  <NvvmDebugVersion major="3" minor="2"/>
  <LlvmVersion major="20" minor="0"/>
  <IRLevel>NVVM_IR_LEVEL_UNIFIED_AFTER_DCI</IRLevel>
  <Options>
    <ArchVariant>NVVM_ARCH_ADA_8_9</ArchVariant>
    <CompileMode>NVVM_COMPILE_MODE_WHOLE_PROGRAM_ABI</CompileMode>
    <OptLevel>NVVM_OPT_LEVEL_2</OptLevel>
    <DebugInfo>NVVM_DEBUG_INFO_NONE</DebugInfo>
    <FastMathOptions>
      <Ftz>1</Ftz>
      <Fmad>1</Fmad>
      <Divide>NVVM_FAST_MATH_DIVIDE_FAST_APPROX</Divide>
      ...
    </FastMathOptions>
    <MaxRRegsAllowed>255</MaxRRegsAllowed>
    ...
  </Options>
  <IsBinary>1</IsBinary>
  <Module>... base64-encoded LLVM bitcode ...</Module>
</NvvmContainer>
```

All enum values are serialized by their full string names (e.g., `NVVM_COMPILE_MODE_SEPARATE_ABI`), not by numeric value. The XML format does **not** use delta encoding -- every field is written regardless of whether it matches the default, making XML containers significantly larger but human-readable.

## Serialization Flow

The serializer (`0xCDD2D0`) has two modes controlled by parameter `a3`: binary (`a3=1`) and XML (`a3=0`).

### Binary Serialization (`a3=1`)

```
 1. Compute version fields (use defaults if not set):
      Version        = {1, 0x41}
      NvvmIRVersion  = {2, 0x62}
      NvvmDebugVersion = {3, 2}
      LlvmVersion    = {20, 0}

 2. Allocate 248-byte NvvmContainerHeader (zeroed)
 3. Allocate 440-byte default Options struct
 4. Allocate two growable arrays:
      scalar_tags[]   -- int32 entries for tag/value pairs
      blob_data[]     -- byte entries for blob payloads

 5. For each field in current Options vs. default Options:
      If field differs:
        Scalar → sub_CD17A0(scalar_tags, tag_id, value)
        Blob   → sub_CD1AB0(blob_data, scalar_tags, tag_id, ptr, size)

 6. Optional IR compression:
      If a4 flag set:
        Compress LLVM bitcode via sub_C8D290
        Compute CRC via sub_CCD2B0 → store as tag 99
        Compress via sub_1688730(codec, data, size)

 7. Append terminator: tag 0 to scalar_tags
 8. Write 24-byte header (with computed ScalarFieldsEnd, BlobDataEnd)
 9. Write scalar_tags array
10. Write blob_data array
11. Write compressed or raw IR payload
```

### Deserialization (`0xCD1D80`)

```
 1. Verify magic == 0x7F4E5C7D
 2. Allocate 248-byte NvvmContainerHeader
 3. Allocate 440-byte Options struct with defaults
 4. Store Options pointer at container header offset 208
 5. Compute tag_ptr = buffer + header_size  (from offset 0x0E)
 6. Compute blob_base = buffer + scalar_fields_end  (from offset 0x10)
 7. Enter switch loop:
      Read tag (int16), decode value (int16 or sentinel + int32)
      Switch on tag (103 unique case labels):
        Tags 1-39:    → write scalar to Options field
        Tag 99:       → store compression algo ID
        Tags 101-173: → write to extended target options
        Tags 201-218: → resolve blob offset, copy blob data
        Tags 301-309: → write to extended int32 fields
        Tags 351-353: → copy 8-byte blob to extended fields
        Tags 401-402: → conditionally parse structured blob
      Tag 0 → exit loop
 8. If tag 99 present: decompress IR payload
 9. Return container pointer
```

## Annotated Hex Dump

A minimal container targeting SM 89 (Ada Lovelace) with default options (only `SmMajor` and `SmMinor` differ from defaults):

```
Offset  Hex                                        Decoded
------  -----------------------------------------  ---------------------------------
0x0000  7D 5C 4E 7F                                Magic: 0x7F4E5C7D
0x0004  01 41                                      Version: 1.65
0x0006  02 62                                      NvvmIRVersion: 2.98
0x0008  03 02                                      NvvmDebugVersion: 3.2
0x000A  14 00                                      LlvmVersion: 20.0
0x000C  00 00                                      IRLevel: 0 (UNIFIED_AFTER_DCI)
0x000E  18 00                                      HeaderSize: 24
0x0010  2C 00 00 00                                ScalarFieldsEnd: 44
0x0014  2C 00 00 00                                BlobDataEnd: 44 (no blobs)

--- Scalar tag/value region ---
0x0018  01 00 08 00                                Tag 1 (SmMajor) = 8
0x001C  02 00 09 00                                Tag 2 (SmMinor) = 9
0x0020  0D 00 01 00                                Tag 13 (Ftz) = 1
0x0024  0F 00 01 00                                Tag 15 (Fmad) = 1
0x0028  00 00                                      Terminator (tag 0)
0x002A  00 00                                      Padding to alignment

--- Blob data region ---
(empty -- ScalarFieldsEnd == BlobDataEnd)

--- IR payload follows at offset 0x002C ---
0x002C  DE C0 17 0B ...                            LLVM bitcode (0xDEC0170B magic)
```

This example shows the efficiency of delta encoding: only 4 tag/value pairs (16 bytes of tags) plus the 24-byte header produce a fully-specified container. All other fields (CompileMode, OptLevel, DebugInfo, all target options) inherit their defaults during deserialization.

A container with a 32-bit value would look like:

```
0x00XX  13 00 FF FF  00 04 00 00                   Tag 19 (MaxRRegsAllowed) = 1024
                                                   (0xFFFF sentinel, then 0x0400 LE)
```

## Pipeline Integration

The container serves as the inter-stage transport format within the cicc compilation pipeline. Two entry paths exist:

| Path | Entry Function | Address | Pipeline |
|------|---------------|---------|----------|
| Path A (LibNVVM) | `nvvmCompileProgram` dispatcher | `0x9047E0` | 3-phase: LNK -> OPT -> LLC |
| Path B (standalone) | `cicc_main` orchestrator | `0x12642A0` | 4-stage: LNK -> OPT -> OPTIXIR -> LLC |

Both paths deserialize the container at phase 1, then translate Options into per-stage compiler flags:

- `SmMajor` / `SmMinor` from tags 1--2 become `-mcpu=sm_XX`
- `FastMath.Ftz` from tag 13 becomes `-nvptx-f32ftz`
- `FastMath.Fmad` from tag 15 becomes the IEEE mode flag
- `OptLevel` becomes `-nvptx-opt-level=N`
- `CompileMode == 2` (SEPARATE_ABI) adds `--device-c`
- `IRLevel == 1` (LTO) enters the [LTO pipeline](../lto/index.md) with partially-optimized bitcode
- `IRLevel == 2` (OPTIX) activates the [OptiX IR](../pipeline/optix-ir.md) stage (bit 6 of pipeline bitmask) and disables LICM and IP-MSP

The container format is the single source of truth for all compilation parameters. When cicc is invoked by nvcc, the driver serializes its accumulated flags into a container, passes the container as input, and cicc deserializes it back into compiler options. This round-trip through binary serialization ensures that all pipeline stages see exactly the same configuration, eliminating the flag-parsing divergence that would otherwise arise from each stage having its own CLI parser.

### YAML Serialization Framework

The XML/YAML path uses a generic serialization framework built on a bundled YAML parser/emitter library (Cluster A: `0xCB0000`--`0xCBFA60`). The library provides:

| Function | Address | Role |
|----------|---------|------|
| `yaml_parser_main` | `0xCB9640` | Top-level YAML parser (25,873 bytes) |
| `yaml_emitter_main_loop` | `0xCBDA10` | Main YAML emitter loop (23,583 bytes) |
| `yaml_scanner_scan_tokens` | `0xCB7E40` | Token scanner (17,924 bytes) |
| `yaml_parser_parse_flow` | `0xCB8C00` | Flow-style parsing (15,188 bytes) |
| `yaml_parser_load_document` | `0xCBA570` | Document loader/resolver (9,695 bytes) |

The serialization framework uses virtual dispatch: each serializable type registers a serialize/deserialize function pair, and the framework dispatches based on the YAML node type (scalar=1, sequence, mapping). All enum values are serialized by their full string names (`NVVM_COMPILE_MODE_SEPARATE_ABI`, `NVVM_ARCH_ADA_8_9`, etc.), not by numeric value.

### Finalizer Knobs Integration

The container Options struct also feeds into the NVIDIA finalizer knobs system through `NvvmOptions_parse_finalizer_knobs` (`0xCD9990`, 31,702 bytes -- the 7th largest function in the binary). This parser ingests the complete set of NVIDIA-specific backend configuration knobs:

- Shader pipeline controls: `PromoteHalf`, `PromoteFixed`, `USePIXBAR`, `VSIsVREnabled`, `VSIsLastVTGStage`
- Codegen controls: `DisablePredication`, `DisableXBlockSched`, `EnableJumpTable`, `ScheduleKils`
- Memory controls: `DoMMACoalescing`, `AssumeConvertMemoryToRegProfitable`
- Barrier controls: `DisableERRBARAfterMEMBAR`, `GenConvBranchForWarpSync`
- PGO controls: `PGOEpoch`, `PGOBatchSize`, `PGOCounterMemBaseVAIndex`
- Per-CTA controls: `CTASizeX`, `CTASizeY`, `CTASizeZ`, `SharedMemorySize`, `SMemScratchBase`
- Register controls: `MaxActiveWarpsPerSM`, `NumReservedUReg`, `NumScratchURegs`

These knobs are distinct from the NVVMPassOptions system (see [NVVMPassOptions](../config/nvvm-pass-options.md)) -- the finalizer knobs configure the backend code generator, while NVVMPassOptions configure the optimization pipeline.

## Tag Summary Statistics

| Range | Count | Description |
|-------|-------|-------------|
| 1--39 | 38 | Core scalar options (SM version, fast-math, unroll, flags) |
| 99 | 1 | Compression metadata |
| 101--173 | 73 | Extended target options (hardware capabilities, memory config) |
| 201--218 | 18 | Blob data (memory windows, resource tables, strings) |
| 301--309 | 9 | Extended int32 fields (cluster config, extended options) |
| 351--353 | 3 | Extended int64 blob references |
| 401--402 | 2 | Structured conditional blobs (TMA / TCGen05) |
| **Total** | **144** | **Distinct tag IDs across 6 ranges** |

The deserializer switch statement has 103 unique case labels -- the remaining 41 tags share code paths with other tags (e.g., all single-bit tags in a byte share a case that reads the bit position from a secondary table).

## Cross-References

- [NVVMPassOptions](../config/nvvm-pass-options.md) -- 221-slot optimization pipeline configuration
- [Pipeline Entry](../pipeline/entry.md) -- LibNVVM API and CLI entry points
- [OptiX IR](../pipeline/optix-ir.md) -- IRLevel=2 OptiX pipeline
- [LTO Pipeline](../lto/index.md) -- IRLevel=1 link-time optimization
- [SM 90 Hopper](../targets/sm90-hopper.md) -- TMA descriptor usage (tag 401)
- [SM 100 Blackwell](../targets/sm100-blackwell.md) -- TCGen05 config usage (tag 402)
- [Bitcode I/O](../infra/bitcode-io.md) -- LLVM bitcode reader/writer wrapping the IR payload
- [nvcc Interface](../pipeline/nvcc-interface.md) -- Driver-to-cicc container passing
