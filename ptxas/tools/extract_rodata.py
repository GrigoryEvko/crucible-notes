#!/usr/bin/env python3
"""
ptxas v13.0.88 .rodata table extractor.

Extracts all known static constant tables from the ptxas binary's .rodata
section for use in an SMT-based SASS code generator. Produces structured
JSON files that an SMT encoder can consume directly.

Usage:
    python3 extract_rodata.py [--binary PATH] [--output DIR]

Defaults:
    --binary ../ptxas
    --output ../extracted/
"""

import argparse
import codecs
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

# ─── Binary geometry (ptxas v13.0.88, non-PIE x86-64 ELF) ─────────────

VA_BASE        = 0x400000
TEXT_START      = 0x403520
TEXT_END        = 0x1CE2DE2
RODATA_START    = 0x1CE2E00
RODATA_END      = 0x240BF90
DATA_REL_START  = 0x29F8D60


# ─── BinaryReader ───────────────────────────────────────────────────────

class BinaryReader:
    """Memory-mapped reader with VA-to-file-offset translation."""

    def __init__(self, path: str):
        self.path = path
        with open(path, 'rb') as f:
            self.data = f.read()
        self.size = len(self.data)

    def _off(self, va: int) -> int:
        off = va - VA_BASE
        if not (0 <= off < self.size):
            raise ValueError(f"VA 0x{va:X} -> offset 0x{off:X} out of range (binary size 0x{self.size:X})")
        return off

    def u8(self, va: int) -> int:
        return self.data[self._off(va)]

    def u16(self, va: int) -> int:
        return struct.unpack_from('<H', self.data, self._off(va))[0]

    def u32(self, va: int) -> int:
        return struct.unpack_from('<I', self.data, self._off(va))[0]

    def i32(self, va: int) -> int:
        return struct.unpack_from('<i', self.data, self._off(va))[0]

    def u64(self, va: int) -> int:
        return struct.unpack_from('<Q', self.data, self._off(va))[0]

    def ptr(self, va: int) -> int:
        return self.u64(va)

    def xmm(self, va: int) -> tuple:
        """Read 128-bit xmmword as (lo_u64, hi_u64)."""
        off = self._off(va)
        lo, hi = struct.unpack_from('<QQ', self.data, off)
        return (lo, hi)

    def xmm_dwords(self, va: int) -> tuple:
        """Read 128-bit xmmword as 4 x u32."""
        off = self._off(va)
        return struct.unpack_from('<4I', self.data, off)

    def read_bytes(self, va: int, n: int) -> bytes:
        off = self._off(va)
        return self.data[off:off + n]

    def cstring(self, va: int, max_len: int = 256) -> str:
        off = self._off(va)
        end = self.data.index(b'\x00', off, off + max_len)
        return self.data[off:end].decode('ascii', errors='replace')

    def u32_array(self, va: int, count: int) -> list:
        off = self._off(va)
        return list(struct.unpack_from(f'<{count}I', self.data, off))

    def u16_array(self, va: int, count: int) -> list:
        off = self._off(va)
        return list(struct.unpack_from(f'<{count}H', self.data, off))

    def ptr_array(self, va: int, count: int) -> list:
        off = self._off(va)
        return list(struct.unpack_from(f'<{count}Q', self.data, off))

    def is_in_text(self, va: int) -> bool:
        return TEXT_START <= va < TEXT_END

    def is_in_rodata(self, va: int) -> bool:
        return RODATA_START <= va < RODATA_END

    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def rot13(s: str) -> str:
    return codecs.decode(s, 'rot_13')


# ─── Table 1: ROT13 Opcode Name Table ──────────────────────────────────

# The InstructionInfo constructor at sub_BE7390 / sub_7A5D10 stores 322
# name entries as {char*, uint64} pairs at object+4184. The 322 ROT13
# opcode name strings are packed in REVERSE order (opcode 321 "LAST" first,
# opcode 0 "ERRBAR" last) in a dense region at 0x21C1336-0x21C1DDE.
# We extract them by byte-scanning this region and reversing.

# Dense packed opcode name region (reverse order: LAST..ERRBAR)
OPCODE_NAME_REGION_START = 0x21C1336  # first byte of "YNFG" (rot13 of "LAST")
OPCODE_NAME_REGION_END   = 0x21C1DDE  # byte after ERRBAR's NUL terminator
OPCODE_NAME_COUNT        = 322

# SM generation boundary markers (verified against InstructionInfo constructor)
SM_BOUNDARIES = {
    "SM70_LAST": 136, "SM73_FIRST": 137, "SM73_LAST": 171,
    "SM82_FIRST": 172, "SM82_LAST": 193,
    "SM86_FIRST": 194, "SM86_LAST": 199,
    "SM89_FIRST": 200, "SM89_LAST": 205,
    "SM90_FIRST": 206, "SM90_LAST": 252,
    "SM100_FIRST": 253, "SM100_LAST": 280,
    "SM104_FIRST": 281, "SM104_LAST": 320,
    "LAST": 321
}

# Validation: known opcode-to-mnemonic mappings from decompiled code
OPCODE_SPOT_CHECKS = {
    0: "ERRBAR", 1: "IMAD", 7: "ISETP", 18: "FSETP", 19: "MOV",
    23: "PLOP3", 25: "NOP", 52: "AL2P_INDEXED", 54: "BMOV_B",
    61: "BAR", 67: "BRA", 71: "CALL", 72: "RET", 77: "EXIT",
    93: "OUT_FINAL", 94: "LDS", 95: "STS", 96: "LDG", 97: "STG",
    102: "ATOM", 104: "RED", 111: "MEMBAR", 119: "SHFL", 122: "DFMA",
    130: "HSET2", 135: "INTRINSIC", 136: "SM70_LAST", 137: "SM73_FIRST",
    171: "SM73_LAST", 172: "SM82_FIRST", 193: "SM82_LAST",
    206: "SM90_FIRST", 252: "SM90_LAST", 253: "SM100_FIRST",
    280: "SM100_LAST", 281: "SM104_FIRST", 320: "SM104_LAST", 321: "LAST",
}


def extract_opcode_names(br: BinaryReader) -> dict:
    """Extract 322-entry ROT13 opcode name table from the dense packed
    string region in .rodata. Names are stored in reverse order (opcode 321
    first, opcode 0 last). We scan the region byte-by-byte, collect all
    NUL-terminated strings, then reverse to get opcode-indexed order."""

    # Byte-scan the dense packed region for NUL-terminated ASCII strings.
    # We read raw bytes directly to avoid cstring() issues with max_len.
    start_off = br._off(OPCODE_NAME_REGION_START)
    end_off = br._off(OPCODE_NAME_REGION_END)
    raw_data = br.data[start_off:end_off]

    forward_entries = []  # in file order: opcode 321 first, opcode 0 last
    pos = 0
    while pos < len(raw_data):
        if raw_data[pos] == 0:
            pos += 1
            continue
        nul = raw_data.find(b'\x00', pos)
        if nul < 0:
            break
        try:
            s = raw_data[pos:nul].decode('ascii')
            d = rot13(s)
            va = OPCODE_NAME_REGION_START + pos
            forward_entries.append({"va": va, "rot13": s, "mnemonic": d, "length": len(s)})
        except UnicodeDecodeError:
            pass
        pos = nul + 1

    # Reverse to get opcode-indexed order (index 0 = ERRBAR, index 321 = LAST)
    entries = list(reversed(forward_entries))

    # Validate count
    if len(entries) != OPCODE_NAME_COUNT:
        print(f"    WARNING: Expected {OPCODE_NAME_COUNT} opcode names, found {len(entries)}", file=sys.stderr)

    # Spot-check known opcodes
    spot_check_failures = []
    for idx, expected in OPCODE_SPOT_CHECKS.items():
        if idx < len(entries):
            actual = entries[idx]["mnemonic"]
            if actual != expected:
                spot_check_failures.append(f"opcode {idx}: expected {expected}, got {actual}")
        else:
            spot_check_failures.append(f"opcode {idx}: index out of range")

    if spot_check_failures:
        for f in spot_check_failures:
            print(f"    SPOT CHECK FAIL: {f}", file=sys.stderr)

    # Also scan the extended mnemonic region (0x2034000-0x203A000) for Mercury names
    mercury_start = 0x2034000
    mercury_end = 0x203A000
    merc_start_off = br._off(mercury_start)
    merc_end_off = br._off(mercury_end)
    merc_data = br.data[merc_start_off:merc_end_off]

    mercury_names = []
    pos = 0
    while pos < len(merc_data):
        if merc_data[pos] == 0:
            pos += 1
            continue
        nul = merc_data.find(b'\x00', pos)
        if nul < 0:
            break
        try:
            s = merc_data[pos:nul].decode('ascii')
            if len(s) >= 4 and all(c.isalnum() or c in '_.' for c in s):
                decoded = rot13(s)
                if decoded.startswith(('MERCURY_', 'HMMA', 'IMMA', 'BMMA', 'DMMA',
                                       'QMMA', 'OMMA', 'GMMA', 'UTC', 'FENCE',
                                       'SYNCS', 'CCTL', 'ACQBULK')):
                    va = mercury_start + pos
                    mercury_names.append({"va": va, "rot13": s, "mnemonic": decoded})
        except UnicodeDecodeError:
            pass
        pos = nul + 1

    return {
        "opcode_names": {
            "primary_count": len(entries),
            "primary_region": f"0x{OPCODE_NAME_REGION_START:X}-0x{OPCODE_NAME_REGION_END:X}",
            "entries": entries,
            "sm_boundaries": SM_BOUNDARIES,
            "spot_check_failures": spot_check_failures,
        },
        "mercury_extended_names": {
            "count": len(mercury_names),
            "entries": mercury_names,
        }
    }


# ─── Table 2: Encoding Category Map ────────────────────────────────────

ENCODING_CAT_MAP_VA = 0x21C0E00
ENCODING_CAT_MAP_COUNT = 322


def extract_encoding_category_map(br: BinaryReader) -> dict:
    entries = br.u32_array(ENCODING_CAT_MAP_VA, ENCODING_CAT_MAP_COUNT)
    is_identity = all(entries[i] == i for i in range(ENCODING_CAT_MAP_COUNT))
    return {
        "encoding_category_map": {
            "count": ENCODING_CAT_MAP_COUNT,
            "source_va": f"0x{ENCODING_CAT_MAP_VA:X}",
            "is_identity": is_identity,
            "entries": entries,
        }
    }


# ─── Table 3: Encoding Format Descriptors ──────────────────────────────

# The format descriptor region is a contiguous array of 38 entries at 136-byte
# stride starting at 0x23F1D70, ending at 0x23F31A0.  Each entry contains a
# 16-byte xmmword header followed by 3 x 10 DWORD arrays (slot_sizes,
# slot_types, slot_flags).  The first DWORD of the xmmword is the format ID
# used by the encoder.  wiki_encoder_count is 0 for descriptors not yet
# catalogued in the wiki.
#
# Indices 34-37 were previously missed.  They are referenced by sub_E82E40
# and 15+ encoder constructors:
#   34 @ 0x23F2F80: 2-slot [14,17]     (sub_E82E40)
#   35 @ 0x23F3008: 2-slot [14,17]     (heavily used by encoder ctors)
#   36 @ 0x23F3090: 3-slot [14,17,33]
#   37 @ 0x23F3118: 3-slot [10,17,33]

FORMAT_DESCRIPTOR_REGION_START = 0x23F1D70
FORMAT_DESCRIPTOR_STRIDE = 136
FORMAT_DESCRIPTOR_COUNT = 38

# Labels for the 16 descriptors previously identified in the wiki.
# Key: VA -> (label, wiki_encoder_count).
_WIKI_LABELS = {
    0x23F1D70: ("64b_B",      70),
    0x23F1DF8: ("128b_0x03", 202),
    0x23F1F08: ("64b_A",     215),
    0x23F1F90: ("64b_C",      20),
    0x23F2018: ("128b_0x07",  26),
    0x23F2128: ("128b_0x09",   2),
    0x23F21B0: ("128b_0x0A", 135),
    0x23F2238: ("64b_D",      17),
    0x23F2348: ("128b_0x0D",  11),
    0x23F25F0: ("128b_0x12",  21),
    0x23F2678: ("128b_0x13", 143),
    0x23F2810: ("128b_0x16",   6),
    0x23F29A8: ("128b_0x19", 152),
    0x23F2C50: ("64b_E",       1),
    0x23F2DE8: ("128b_0x21",   2),
    0x23F2EF8: ("128b_0x23",   9),
}


def extract_format_descriptors(br: BinaryReader) -> dict:
    """Extract encoding format descriptors by scanning the contiguous 34-entry
    region at 136-byte stride.  Each descriptor is:
    - 16 bytes: xmmword (format metadata: format_id, slot_count, width_code, ...)
    - 40 bytes: 10 x u32 slot_sizes (0xFFFFFFFF = unused)
    - 40 bytes: 10 x u32 slot_types (0xFFFFFFFF = unused)
    - 40 bytes: 10 x u32 slot_flags (0xFFFFFFFF = unused)
    Total: 136 bytes per descriptor."""

    results = []
    for idx in range(FORMAT_DESCRIPTOR_COUNT):
        va = FORMAT_DESCRIPTOR_REGION_START + idx * FORMAT_DESCRIPTOR_STRIDE

        if not br.is_in_rodata(va):
            print(f"    WARNING: Format descriptor [{idx}] VA 0x{va:X} not in .rodata", file=sys.stderr)

        lo, hi = br.xmm(va)
        fmt_id = br.u32(va)  # first DWORD is the format ID

        # The three 10-DWORD arrays follow immediately after the xmmword
        arr_base = va + 16
        slot_sizes = br.u32_array(arr_base, 10)
        slot_types = br.u32_array(arr_base + 40, 10)
        slot_flags = br.u32_array(arr_base + 80, 10)

        # Convert 0xFFFFFFFF sentinel to -1
        slot_sizes = [x if x != 0xFFFFFFFF else -1 for x in slot_sizes]
        slot_types = [x if x != 0xFFFFFFFF else -1 for x in slot_types]
        slot_flags = [x if x != 0xFFFFFFFF else -1 for x in slot_flags]

        active = sum(1 for s in slot_sizes if s != -1)

        # Derive instruction width from active slot count: 1 slot = 64-bit, 2+ = 128-bit
        width = 64 if active <= 1 else 128

        # Use wiki label if catalogued, otherwise auto-generate from format ID
        if va in _WIKI_LABELS:
            label, enc_count = _WIKI_LABELS[va]
        else:
            label = f"{width}b_0x{fmt_id:02X}_idx{idx}"
            enc_count = 0

        # Validate: active slots should have reasonable sizes (1-64 bits)
        for i, s in enumerate(slot_sizes):
            if s != -1 and (s < 1 or s > 64):
                print(f"    WARNING: {label} slot_sizes[{i}] = {s} (outside 1-64 range)", file=sys.stderr)

        # Validate: unused slots should be contiguous at the end
        found_unused = False
        for i, s in enumerate(slot_sizes):
            if s == -1:
                found_unused = True
            elif found_unused:
                print(f"    WARNING: {label} has gap in slot_sizes at index {i}", file=sys.stderr)
                break

        results.append({
            "index": idx,
            "va": f"0x{va:X}",
            "label": label,
            "format_id": fmt_id,
            "instruction_width": width,
            "xmmword_lo": f"0x{lo:016X}",
            "xmmword_hi": f"0x{hi:016X}",
            "slot_sizes": slot_sizes,
            "slot_types": slot_types,
            "slot_flags": slot_flags,
            "active_slots": active,
            "wiki_encoder_count": enc_count,
            "descriptor_size_bytes": FORMAT_DESCRIPTOR_STRIDE,
        })

    return {"format_descriptors": results}


# ─── Table 4: Opcode-to-Encoding Table ─────────────────────────────────

OPCODE_ENC_TABLE_VA = 0x22B4B60
OPCODE_ENC_TABLE_COUNT = 222
OPCODE_ENC_SENTINEL = 355


def extract_opcode_to_encoding(br: BinaryReader) -> dict:
    """Extract word_22B4B60: the opcode-to-encoding-slot lookup table.
    Used by the mega-selector sub_C0EB10 PATH B as:
        if (opcode <= 0xDD) encoding_index = word_22B4B60[opcode]
    This is an array of 222 u16 entries (opcodes 0-221 = 0x00-0xDD).
    Sentinel value 355 (0x163) means "no encoding / extended opcode path"."""

    entries = br.u16_array(OPCODE_ENC_TABLE_VA, OPCODE_ENC_TABLE_COUNT)
    non_zero = sum(1 for e in entries if e != 0)
    sentinel_count = sum(1 for e in entries if e == OPCODE_ENC_SENTINEL)
    max_val = max(entries) if entries else 0

    # Validate: no entry should exceed 355 (the sentinel)
    invalid = [(i, e) for i, e in enumerate(entries) if e > OPCODE_ENC_SENTINEL]
    if invalid:
        for idx, val in invalid[:5]:
            print(f"    WARNING: opcode_to_encoding[{idx}] = {val} exceeds sentinel {OPCODE_ENC_SENTINEL}", file=sys.stderr)

    return {
        "opcode_to_encoding": {
            "count": OPCODE_ENC_TABLE_COUNT,
            "sentinel_value": OPCODE_ENC_SENTINEL,
            "source_va": f"0x{OPCODE_ENC_TABLE_VA:X}",
            "non_zero_count": non_zero,
            "sentinel_count": sentinel_count,
            "max_value": max_val,
            "entries": [
                {"opcode": i, "encoding_slot": entries[i]}
                for i in range(OPCODE_ENC_TABLE_COUNT)
            ],
        }
    }


# ─── Table 5: Occupancy Constants ──────────────────────────────────────

# Labels derived from sub_ABF250 SM-conditional dispatch and sub_AAFCF0
# profile initialization.  Each xmmword is 4 x u32 occupancy parameters.
OCCUPANCY_XMMWORDS = [
    (0x229C400, "sm90_regfile_params"),      # [6, 128, 32768, 255]
    (0x229C410, "sm90_granularity_params"),   # [63, 7, 7, 16]
    (0x229C420, "barrier_cta_params"),        # [4, 2048, 8, 2]
    (0x229C430, "sm60_sm70_max_warps"),       # [32, 32, 64, 32]
    (0x229C440, "sm53_sm62_regfile_params"),  # [6, 128, 256, 255]
    (0x229C450, "sm35_sm37_max_warps"),       # [32, 32, 48, 16]
    (0x229C460, "sm3x_sm5x_max_warps"),      # [32, 32, 48, 24]
    (0x229C470, "sm70plus_granularity"),      # [80, 7, 7, 16]
]


def extract_occupancy_constants(br: BinaryReader) -> dict:
    entries = []
    for va, label in OCCUPANCY_XMMWORDS:
        dwords = br.xmm_dwords(va)
        entries.append({
            "va": f"0x{va:X}",
            "label": label,
            "dwords": list(dwords),
            "dwords_hex": [f"0x{d:08X}" for d in dwords],
        })

    # Also extract the occupancy formula constants:
    # sub_A99FE0: max_warps = (-granularity & (2 * half_reg_file / regs)) - offset
    # The 3 operands come from profile object fields set by sub_AAFCF0 from these xmmwords.

    return {
        "occupancy_constants": {
            "count": len(entries),
            "formula": "max_warps = (-granularity & (2 * half_reg_file / regs)) - offset",
            "formula_source": "sub_A99FE0 (7 lines)",
            "entries": entries,
        }
    }


# ─── Table 6: Shared Memory Configuration Tables ───────────────────────

SMEM_GLOBAL_TABLE_VA = 0x21FB640
SMEM_SM75_TABLE_VA   = 0x21D9168


def extract_shared_memory_configs(br: BinaryReader) -> dict:
    """Extract shared memory configuration tables.
    The global table at 0x21FB640 contains 11 ascending size values (0 to 335872)
    terminated by a 0. The sm_75 table at 0x21D9168 has 3 entries for Turing."""

    # Read global table: scan for monotonically ascending values then a zero
    global_sizes = []
    va = SMEM_GLOBAL_TABLE_VA
    for i in range(30):
        val = br.u32(va + i * 4)
        if i > 0 and val == 0:
            break
        if i > 0 and val < global_sizes[-1]:
            # Non-monotonic: end of table
            break
        global_sizes.append(val)

    # Read sm_75 table: 3 entries (0, 32768, 65536) then 0 terminator
    sm75_raw = br.u32_array(SMEM_SM75_TABLE_VA, 10)
    sm75_sizes = []
    for i, v in enumerate(sm75_raw):
        if i > 0 and v == 0:
            break
        sm75_sizes.append(v)

    # Validate: global table sizes should be multiples of 1024 (except 0)
    for s in global_sizes:
        if s != 0 and s % 1024 != 0:
            print(f"    WARNING: SMEM global size {s} not 1KB-aligned", file=sys.stderr)

    return {
        "shared_memory_configs": {
            "global_table": {
                "va": f"0x{SMEM_GLOBAL_TABLE_VA:X}",
                "count": len(global_sizes),
                "sizes_bytes": global_sizes,
                "sizes_kb": [s // 1024 for s in global_sizes if s > 0],
            },
            "sm_75": {
                "va": f"0x{SMEM_SM75_TABLE_VA:X}",
                "count": len(sm75_sizes),
                "sizes_bytes": sm75_sizes,
                "sizes_kb": [s // 1024 for s in sm75_sizes if s > 0],
            },
        }
    }


# ─── Table 7: ISel Dispatch Sub-Tables ─────────────────────────────────

ISEL_DISPATCH_VA = 0x22AD9D0
ISEL_DISPATCH_END = 0x22B14B0  # byte after last valid ptr; ASCII "RBG\0" at 0x22B14B0
ISEL_SENTINEL_VA = 0xBA9E23  # no-match stub inside sub_BA9D00


def extract_isel_dispatch_tables(br: BinaryReader) -> dict:
    total_bytes = ISEL_DISPATCH_END - ISEL_DISPATCH_VA
    count = total_bytes // 8
    ptrs = br.ptr_array(ISEL_DISPATCH_VA, count)

    # Validate pointers: every entry must be a valid .text address
    valid = sum(1 for p in ptrs if br.is_in_text(p))
    sentinel = sum(1 for p in ptrs if p == ISEL_SENTINEL_VA)
    unique = len(set(ptrs))

    # Range check: all pointers should be within the ISel DAG pattern matcher range
    # (0xB28F60-0xB7D000) or the sentinel (0xBA9E23) or nearby mega-selector code
    isel_range_count = sum(1 for p in ptrs if 0xB00000 <= p <= 0xBC0000)
    invalid_ptrs = [(i, p) for i, p in enumerate(ptrs) if not br.is_in_text(p)]

    if invalid_ptrs:
        for idx, p in invalid_ptrs[:5]:
            print(f"    WARNING: ISel dispatch[{idx}] = 0x{p:X} is not in .text", file=sys.stderr)

    if valid != count:
        print(f"    WARNING: {count - valid} ISel dispatch pointers are not valid .text addresses", file=sys.stderr)

    return {
        "isel_dispatch_tables": {
            "source_va": f"0x{ISEL_DISPATCH_VA:X}",
            "end_va": f"0x{ISEL_DISPATCH_END:X}",
            "total_pointers": count,
            "valid_text_pointers": valid,
            "invalid_text_pointers": count - valid,
            "sentinel_count": sentinel,
            "sentinel_va": f"0x{ISEL_SENTINEL_VA:X}",
            "unique_targets": unique,
            "isel_range_pointers": isel_range_count,
            "pointers": [f"0x{p:X}" for p in ptrs],
        }
    }


# ─── Table 8: Encoding Lookup Sub-Tables ──────────────────────────────
#
# The region 0x22A1500-0x22A1D58 is NOT a flat 576 x u32 array.  It contains:
#   (a) 0x22A1500-0x22A16D8: 8 small u32 lookup sub-tables with sentinels
#       and dedicated accessor functions
#   (b) 0x22A16E0-0x22A18E0: 128 packed u16 pairs (256 x u16)
#   (c) 0x22A18E0-0x22A1D50: additional u32 sub-tables
#   (d) 0x22A1D58+: version strings ("9.0", "10.0", ...) -- NOT encoding data
#
# Each sub-table in (a) has: N valid u32 entries, then 0-padding, then a
# sentinel value.  The sentinel is the last nonzero u32 before the padding
# reaches the next sub-table boundary.

_ENC_SUBTABLES = [
    # (va, count, sentinel, accessor, label)
    (0x22A1500,  3, 1230, "sub_AF55F0", "enc_lookup_0"),
    (0x22A1520,  8, 1216, "sub_AF55A0", "enc_lookup_1"),
    (0x22A1540,  5, 1199, "sub_AF5510", "enc_lookup_2"),
    (0x22A1558,  3, 1195, "sub_AF54F0", "enc_lookup_3"),
    (0x22A1570,  5, 1174, "sub_AF5450", "enc_lookup_4"),
    (0x22A1588,  3, 1170, "sub_AF5430", "enc_lookup_5"),
    (0x22A15A0,  5, 1162, "sub_AF5410", "enc_lookup_6"),
    (0x22A15C0, 12, 1149, "sub_AF53F0", "enc_lookup_7"),
]

_ENC_PACKED_U16_VA    = 0x22A16E0
_ENC_PACKED_U16_COUNT = 256   # 128 pairs = 256 u16 values
_ENC_PACKED_U16_END   = 0x22A18E0

_ENC_EXTRA_VA  = 0x22A18E0
_ENC_EXTRA_END = 0x22A1D50

_ENC_VERSION_STRINGS_VA = 0x22A1D58  # "9.0", "10.0", etc. -- not encoding data


def extract_encoding_constants(br: BinaryReader) -> dict:
    """Extract structured encoding lookup sub-tables, packed u16 pairs,
    and additional sub-tables from the 0x22A1500-0x22A1D50 region."""

    # (a) 8 small u32 lookup sub-tables
    subtables = []
    for va, count, sentinel, accessor, label in _ENC_SUBTABLES:
        entries = br.u32_array(va, count)
        # Validate sentinel: read the DWORD immediately after the entries
        # (accounting for padding/alignment)
        subtables.append({
            "label": label,
            "va": f"0x{va:X}",
            "count": count,
            "sentinel": sentinel,
            "accessor": accessor,
            "entries": entries,
        })

    # (b) 128 packed u16 pairs
    u16_entries = br.u16_array(_ENC_PACKED_U16_VA, _ENC_PACKED_U16_COUNT)
    pairs = []
    for i in range(0, _ENC_PACKED_U16_COUNT, 2):
        pairs.append([u16_entries[i], u16_entries[i + 1]])

    # (c) Additional u32 sub-tables (0x22A18E0-0x22A1D50)
    extra_count = (_ENC_EXTRA_END - _ENC_EXTRA_VA) // 4
    extra_entries = br.u32_array(_ENC_EXTRA_VA, extra_count)
    extra_non_zero = sum(1 for e in extra_entries if e != 0)

    return {
        "encoding_lookup_tables": {
            "region_va": "0x22A1500",
            "region_end": "0x22A1D50",
            "note": "Structured sub-tables, NOT a flat u32 array. "
                    "Version strings at 0x22A1D58+ are excluded.",
            "subtables": {
                "count": len(subtables),
                "entries": subtables,
            },
            "packed_u16_pairs": {
                "va": f"0x{_ENC_PACKED_U16_VA:X}",
                "end": f"0x{_ENC_PACKED_U16_END:X}",
                "pair_count": len(pairs),
                "pairs": pairs,
            },
            "extra_subtables": {
                "va": f"0x{_ENC_EXTRA_VA:X}",
                "end": f"0x{_ENC_EXTRA_END:X}",
                "u32_count": extra_count,
                "non_zero_count": extra_non_zero,
                "entries": extra_entries,
            },
        }
    }


# ─── Table 9: Phase Name Table ─────────────────────────────────────────

PHASE_NAME_TABLE_VA = 0x22BD0C0
PHASE_NAME_COUNT = 159


def extract_phase_names(br: BinaryReader) -> dict:
    ptrs = br.ptr_array(PHASE_NAME_TABLE_VA, PHASE_NAME_COUNT)
    entries = []
    for i, p in enumerate(ptrs):
        if br.is_in_rodata(p):
            name = br.cstring(p, max_len=80)
        else:
            name = f"<invalid_ptr_0x{p:X}>"
        entries.append({
            "index": i,
            "name": name,
            "string_va": f"0x{p:X}",
        })

    return {
        "phase_names": {
            "source_va": f"0x{PHASE_NAME_TABLE_VA:X}",
            "count": PHASE_NAME_COUNT,
            "entries": entries,
        }
    }


# ─── Table 10: Tier 2 Modifier Tables ──────────────────────────────────

TIER2_GROUPS = [
    ("group_A_maxwell_turing",     0x202A280, 12, "sm_50-sm_75"),  # extends to 0x202A340
    ("group_B_ampere_ada",         0x22F1B30,  3, "sm_80-sm_89"),
    ("group_D_lovelace_hopper",    0x22F1BA0,  2, "sm_89-sm_90"),
    ("group_E_blackwell_dc",       0x22F1AA0,  4, "sm_100-sm_103"),
    ("group_F_blackwell_consumer", 0x22F1C20,  2, "sm_120-sm_121"),
    ("group_G_cross_arch",         0x23B2DE0,  1, "cross-architecture"),
]


def extract_tier2_modifiers(br: BinaryReader) -> dict:
    groups = []
    for label, va, count, sm_range in TIER2_GROUPS:
        entries = []
        for j in range(count):
            addr = va + j * 16
            lo, hi = br.xmm(addr)
            entries.append({
                "offset": j * 16,
                "lo": f"0x{lo:016X}",
                "hi": f"0x{hi:016X}",
                "dwords": list(br.xmm_dwords(addr)),
            })
        groups.append({
            "label": label,
            "va_start": f"0x{va:X}",
            "sm_range": sm_range,
            "entry_count": count,
            "entries": entries,
        })

    return {"tier2_modifier_tables": {"groups": groups}}


# ─── Table 11: Knob Name Strings ───────────────────────────────────────

# Knob names are ROT13-encoded strings in .rodata referenced by ctor_005.
# The knob descriptor table itself is in .bss (runtime-initialized).
# We extract only the name strings from .rodata.
#
# IMPORTANT: The knob region also contains plaintext constant strings
# (shader stage names like VERTEX/COMPUTE, error messages, etc.) that
# are NOT ROT13-encoded.  We tag each entry with is_rot13 to distinguish.

KNOB_STRING_REGIONS = [
    (0x21B6000, 0x21C1000, "OCG knobs (ctor_005)"),
    (0x21DB000, 0x21DE000, "DAG knobs (ctor_007)"),
]

# Known plaintext constants in the knob regions that are NOT ROT13-encoded.
# These are shader stage names, config strings, and function/pass names
# embedded alongside actual ROT13-encoded knob names.
_KNOB_PLAINTEXT_CONSTANTS = frozenset({
    "NamedPhases", "VERTEX", "VERTEX_A", "VERTEX_AB", "VERTEX_B",
    "TESSELLATION", "TESSELLATION_INIT", "PIXEL", "GEOMETRY", "COMPUTE",
    "DUMP_KNOBS_TO_FILE",
    # Function/pass names that appear as plaintext in the knob region
    "ParseKnobValue", "ReadKnobsFile", "ScheduleInstructions",
    "OptimizeNaNOrZero", "ConvertMemoryToRegisterOrUniform",
})

# ROT13-encoded instruction mnemonics and enum constants embedded in the knob
# region that are NOT knob names.  These are opcode/instruction identifiers
# referenced by knob descriptors (e.g., at 0x21B6896-0x21B68F0 and in the
# DAG region).  Keyed by their ROT13 form as it appears in the binary.
_KNOB_FALSE_POSITIVE_ROT13 = frozenset({
    # Instruction mnemonics (ROT13 form -> decoded)
    "KZNQ",     # XMAD
    "FGF",      # STS
    "FGT",      # STG
    "ZRZONE",   # MEMBAR
    "YQFZ",     # LDSM
    "YQF",      # LDS
    "YQTFGF",   # LDGSTS
    "YQT",      # LDG
    "VZZN",     # IMMA
    "VQC",      # IDP
    "VZNQ",     # IMAD
    "UZZN",     # HMMA
    "USZN2",    # HFMA2
    "SSZN",     # FFMA
    "QZZN",     # DMMA  (appears in both OCG and DAG regions)
    "QSZN",     # DFMA
    "NEEVIRF",  # ARRIVES
    "ABAR",     # NONE
    "GRKF",     # TEXS
    # DAG region false positives
    "XU64",     # KH64
    "DMMA",     # QZZN  (reversed: DMMA in binary decodes to QZZN)
    "LSU_T",    # YFH_G (not a knob)
})


def _is_rot13_encoded(raw_str: str) -> bool:
    """Determine if a binary string is ROT13-encoded (actual knob name)
    or plaintext (non-knob constant that happens to be in the region).

    ROT13 is a self-inverse cipher, so it's mathematically impossible to
    distinguish ROT13 from plaintext without semantic knowledge.  We use
    an explicit allowlist of known plaintext constants (shader stage names,
    function names, config strings) and treat everything else as ROT13,
    which is correct for the vast majority of entries in the knob regions."""
    return raw_str not in _KNOB_PLAINTEXT_CONSTANTS


def _is_knob_false_positive(raw_str: str) -> bool:
    """Check if a ROT13-form string is a known false positive (instruction
    mnemonic or enum constant, not a knob name)."""
    return raw_str in _KNOB_FALSE_POSITIVE_ROT13


def extract_knob_strings(br: BinaryReader) -> dict:
    """Extract ROT13-encoded knob name strings from .rodata.
    Uses direct byte scanning to avoid cstring() issues.
    Each entry includes is_rot13 flag: True = actual knob name (ROT13 in
    binary, 'name' field is the decoded human-readable form), False =
    plaintext constant ('rot13' field is the actual identifier)."""

    all_knobs = []
    for region_start, region_end, label in KNOB_STRING_REGIONS:
        start_off = br._off(region_start)
        end_off = br._off(region_end)
        raw = br.data[start_off:end_off]

        pos = 0
        while pos < len(raw):
            if raw[pos] == 0:
                pos += 1
                continue
            nul = raw.find(b'\x00', pos)
            if nul < 0:
                break
            try:
                s = raw[pos:nul].decode('ascii')
                if (len(s) >= 3 and
                    all(c.isalnum() or c == '_' for c in s) and
                    any(c.isupper() for c in s)):
                    # Skip known false positives (instruction mnemonics, enum
                    # constants) before decoding
                    if _is_knob_false_positive(s):
                        pos = nul + 1
                        continue
                    decoded = rot13(s)
                    # Filter: knob names are CamelCase or UPPER_CASE
                    if decoded[0].isupper() and len(decoded) >= 3:
                        is_rot13 = _is_rot13_encoded(s)
                        va = region_start + pos
                        all_knobs.append({
                            "va": f"0x{va:X}",
                            "rot13": s,
                            "name": decoded if is_rot13 else s,
                            "is_rot13": is_rot13,
                            "region": label,
                        })
            except UnicodeDecodeError:
                pass
            pos = nul + 1

    rot13_count = sum(1 for k in all_knobs if k["is_rot13"])
    plain_count = len(all_knobs) - rot13_count

    return {
        "knob_strings": {
            "total_count": len(all_knobs),
            "rot13_count": rot13_count,
            "plaintext_count": plain_count,
            "regions": [{"start": f"0x{s:X}", "end": f"0x{e:X}", "label": l}
                        for s, e, l in KNOB_STRING_REGIONS],
            "entries": all_knobs,
        }
    }


# ─── Table 12: High-Entropy Blob Metadata ─────────────────────────────

# The .rodata section contains a ~2.80 MB region of near-maximum entropy
# (8.00 bits/byte), spanning VA 0x1D4FF40 to 0x201CE00. This is likely
# compressed or encrypted data (e.g., NVVM IR templates, pre-built code
# sequences, or encoded lookup tables). We document its boundaries and
# compute a fingerprint without extracting the raw data.
#
# Layout immediately preceding the blob:
#   0x1D4B778 - 0x1D4D938 : 8-byte pointer table (1080 entries, points to 0x60xxxx)
#   0x1D4D938 - 0x1D4D950 : 24 bytes zero alignment padding
#   0x1D4D950 - 0x1D4FF40 : 16-byte blob offset index (607 entries, 9712 bytes)
#                            Each entry: u64 offset into the blob + u64 tag (0 or 3).
#                            Offsets are monotonically decreasing, pointing into
#                            the 0x1F1xxxx-0x201xxxx range (within the blob itself).
#                            7 entries carry tag=3 (clustered at 0x1D4DA60-0x1D4DAC0);
#                            the remaining 600 entries have tag=0.
#   0x1D4FF40 - 0x201CE00 : High-entropy SASS blob (pure data, no header)
#
# NOTE: The 512-byte region 0x1D4FE00-0x1D50000 that appeared as a "gap" is
# fully accounted for: bytes 0x1D4FE00-0x1D4FF40 are the last 20 entries of
# the blob offset index, and bytes 0x1D4FF40-0x1D50000 are the first 192 bytes
# of the high-entropy blob. There is no padding or gap here.

HIGH_ENTROPY_BLOB_START = 0x1D4FF40
HIGH_ENTROPY_BLOB_END   = 0x201CE00

# The blob offset index that precedes the blob.
BLOB_OFFSET_INDEX_START = 0x1D4D950
BLOB_OFFSET_INDEX_END   = 0x1D4FF40  # == HIGH_ENTROPY_BLOB_START
BLOB_OFFSET_INDEX_ENTRY_COUNT = 607


def extract_high_entropy_blob_metadata(br: BinaryReader) -> dict:
    """Document the high-entropy blob region in .rodata without extracting
    the raw bytes (it would be ~2.8 MB of incompressible data).
    Computes SHA-256 fingerprint and boundary entropy measurements.

    The blob at 0x1D4FF40 begins immediately with high-entropy data (no
    structured header). The preceding blob offset index (607 x 16-byte
    entries at 0x1D4D950-0x1D4FF40) is extracted separately."""
    import math

    blob_start_off = br._off(HIGH_ENTROPY_BLOB_START)
    blob_end_off = br._off(HIGH_ENTROPY_BLOB_END)
    blob_size = blob_end_off - blob_start_off
    blob_data = br.data[blob_start_off:blob_end_off]

    # SHA-256 of the blob
    blob_hash = hashlib.sha256(blob_data).hexdigest()

    # Extract the preceding blob offset index
    idx_start_off = br._off(BLOB_OFFSET_INDEX_START)
    idx_end_off = br._off(BLOB_OFFSET_INDEX_END)
    idx_data = br.data[idx_start_off:idx_end_off]
    idx_entry_count = (idx_end_off - idx_start_off) // 16

    index_entries = []
    tagged_count = 0
    for i in range(idx_entry_count):
        off = i * 16
        offset_val = struct.unpack_from('<Q', idx_data, off)[0]
        tag = struct.unpack_from('<Q', idx_data, off + 8)[0]
        entry_va = BLOB_OFFSET_INDEX_START + i * 16
        index_entries.append({
            "va": f"0x{entry_va:X}",
            "offset": f"0x{offset_val:X}",
            "tag": int(tag),
        })
        if tag != 0:
            tagged_count += 1

    # Overall entropy of the blob
    freq = [0] * 256
    for b in blob_data:
        freq[b] += 1
    entropy = 0.0
    for f in freq:
        if f > 0:
            p = f / blob_size
            entropy -= p * math.log2(p)

    # Entropy at boundaries
    def page_entropy(page_data: bytes) -> float:
        f = [0] * 256
        for b in page_data:
            f[b] += 1
        e = 0.0
        n = len(page_data)
        for c in f:
            if c > 0:
                p = c / n
                e -= p * math.log2(p)
        return e

    first_page_ent = page_entropy(blob_data[:4096])
    last_page_ent = page_entropy(blob_data[-4096:])

    header_hex = blob_data[:16].hex()

    return {
        "high_entropy_blob": {
            "start_va": f"0x{HIGH_ENTROPY_BLOB_START:X}",
            "end_va": f"0x{HIGH_ENTROPY_BLOB_END:X}",
            "size_bytes": blob_size,
            "size_mb": round(blob_size / (1024 * 1024), 2),
            "sha256": blob_hash,
            "overall_entropy_bits": round(entropy, 4),
            "first_page_entropy": round(first_page_ent, 4),
            "last_page_entropy": round(last_page_ent, 4),
            "first_16_bytes_hex": header_hex,
            "blob_offset_index": {
                "start_va": f"0x{BLOB_OFFSET_INDEX_START:X}",
                "end_va": f"0x{BLOB_OFFSET_INDEX_END:X}",
                "entry_count": idx_entry_count,
                "size_bytes": idx_end_off - idx_start_off,
                "tagged_entries": tagged_count,
                "tag_values": "0 (normal) or 3 (7 entries at 0x1D4DA60-0x1D4DAC0)",
                "offset_range": f"{index_entries[-1]['offset']}-{index_entries[0]['offset']}" if index_entries else "N/A",
                "note": "Monotonically decreasing offsets into the SASS blob. "
                        "Each entry is 16 bytes: u64 blob_offset + u64 tag.",
            },
            "note": "~2.8 MB of near-maximum entropy data (8.00 bits/byte) starting "
                    "immediately at 0x1D4FF40. Preceded by a 607-entry blob offset "
                    "index (0x1D4D950-0x1D4FF40). Likely compressed/encrypted "
                    "NVVM IR templates, pre-built code sequences, or encoded lookup tables.",
        }
    }


# ─── Table 13: Universal Slot Array Template ─────────────────────────

# The most-referenced table in the encoding pipeline (7,302 references).
# Located at VA 0x23F1C60, immediately before the format descriptors.
# Structure: 3 arrays of 10 DWORDs (120 bytes total).
#   slot_sizes[10] at +0   : bit-widths for each slot position
#   slot_types[10] at +40  : type codes (0xFFFFFFFF = unused)
#   slot_flags[10] at +80  : flag values (0xFFFFFFFF = unused)
# Active slots use sizes 3,2,4,6,8 (total 23 bits); only slot[4] has
# a type (14) and flag (0).

UNIVERSAL_SLOT_TEMPLATE_VA = 0x23F1C60
UNIVERSAL_SLOT_TEMPLATE_SIZE = 120  # 30 DWORDs


def extract_universal_slot_template(br: BinaryReader) -> dict:
    """Extract the universal slot array template: the default slot geometry
    used as a base by the encoding pipeline.  3 x DWORD[10] at 0x23F1C60."""

    dwords = br.u32_array(UNIVERSAL_SLOT_TEMPLATE_VA, 30)
    slot_sizes = list(dwords[0:10])
    slot_types = list(dwords[10:20])
    slot_flags = list(dwords[20:30])

    sentinel = 0xFFFFFFFF

    # Convert sentinels to -1 for JSON readability
    sizes_clean = [s if s != sentinel else -1 for s in slot_sizes]
    types_clean = [t if t != sentinel else -1 for t in slot_types]
    flags_clean = [f if f != sentinel else -1 for f in slot_flags]

    active = sum(1 for s in slot_sizes if s != sentinel)
    total_bits = sum(s for s in slot_sizes if s != sentinel)

    # Validate: active slots should have sizes in 1-64 range
    for i, s in enumerate(slot_sizes):
        if s != sentinel and (s < 1 or s > 64):
            print(f"    WARNING: universal_slot_template slot_sizes[{i}] = {s} outside 1-64", file=sys.stderr)

    # Validate: unused slots contiguous at end
    found_unused = False
    for i, s in enumerate(slot_sizes):
        if s == sentinel:
            found_unused = True
        elif found_unused:
            print(f"    WARNING: universal_slot_template gap in slot_sizes at index {i}", file=sys.stderr)
            break

    return {
        "universal_slot_template": {
            "source_va": f"0x{UNIVERSAL_SLOT_TEMPLATE_VA:X}",
            "size_bytes": UNIVERSAL_SLOT_TEMPLATE_SIZE,
            "reference_count": 7302,
            "active_slots": active,
            "total_bits": total_bits,
            "slot_sizes": sizes_clean,
            "slot_types": types_clean,
            "slot_flags": flags_clean,
            "raw_dwords": [f"0x{d:08X}" for d in dwords],
        }
    }


# ─── Table 14: Encoding Bitfield Lookup Table ────────────────────────

# Maps modifier combinations to bit positions within the SASS instruction
# word.  4096 entries x 8 bytes each (u32 field_a, u32 field_b).
# Sentinel 0xFFFFFFFF = "not applicable".  ~98% fill rate.
# Immediately follows the format descriptors region.
# Field A is predominantly a .text VA (function pointer) or a small integer.
# Field B is predominantly 0, with a handful of small-value entries.

BITFIELD_LOOKUP_VA    = 0x23F2E00
BITFIELD_LOOKUP_END   = 0x23FAE00
BITFIELD_LOOKUP_COUNT = 4096
BITFIELD_LOOKUP_ENTRY_SIZE = 8


def extract_encoding_bitfield_lookup(br: BinaryReader) -> dict:
    """Extract the 4096-entry encoding bitfield lookup table.
    Each entry is (u32, u32).  Computes fill rate, value ranges,
    and distribution statistics for the SMT encoder."""
    from collections import Counter

    sentinel = 0xFFFFFFFF
    entries = []
    a_vals = []
    b_vals = []
    both_sentinel = 0
    a_only_sentinel = 0
    b_only_sentinel = 0
    both_valid = 0

    for i in range(BITFIELD_LOOKUP_COUNT):
        va = BITFIELD_LOOKUP_VA + i * BITFIELD_LOOKUP_ENTRY_SIZE
        a = br.u32(va)
        b = br.u32(va + 4)

        a_is_sent = (a == sentinel)
        b_is_sent = (b == sentinel)

        if a_is_sent and b_is_sent:
            both_sentinel += 1
        elif a_is_sent:
            a_only_sentinel += 1
        elif b_is_sent:
            b_only_sentinel += 1
        else:
            both_valid += 1

        if not a_is_sent:
            a_vals.append(a)
        if not b_is_sent:
            b_vals.append(b)

        # Store compactly: sentinel -> null in JSON
        entries.append([
            None if a_is_sent else a,
            None if b_is_sent else b,
        ])

    active = BITFIELD_LOOKUP_COUNT - both_sentinel
    fill_rate = active / BITFIELD_LOOKUP_COUNT

    # Classify field A values
    text_va_count = sum(1 for a in a_vals if TEXT_START <= a < TEXT_END)
    small_val_count = sum(1 for a in a_vals if a < TEXT_START)

    # Field B distribution
    b_dist = Counter(b_vals)
    b_distribution = [{"value": v, "count": c} for v, c in b_dist.most_common()]

    # Field A: top 30 most common values
    a_dist = Counter(a_vals)
    a_top = [
        {"value": val, "value_hex": f"0x{val:X}", "count": cnt}
        for val, cnt in a_dist.most_common(30)
    ]

    stats = {
        "total_entries": BITFIELD_LOOKUP_COUNT,
        "active_entries": active,
        "fill_rate": round(fill_rate, 4),
        "fill_rate_pct": f"{fill_rate * 100:.1f}%",
        "both_sentinel": both_sentinel,
        "a_sentinel_b_valid": a_only_sentinel,
        "a_valid_b_sentinel": b_only_sentinel,
        "both_valid": both_valid,
        "field_a": {
            "non_sentinel_count": len(a_vals),
            "min": min(a_vals) if a_vals else None,
            "max": max(a_vals) if a_vals else None,
            "unique_values": len(set(a_vals)),
            "text_va_pointers": text_va_count,
            "small_values": small_val_count,
            "top_30": a_top,
        },
        "field_b": {
            "non_sentinel_count": len(b_vals),
            "min": min(b_vals) if b_vals else None,
            "max": max(b_vals) if b_vals else None,
            "unique_values": len(set(b_vals)),
            "distribution": b_distribution,
        },
    }

    return {
        "encoding_bitfield_lookup": {
            "source_va": f"0x{BITFIELD_LOOKUP_VA:X}",
            "end_va": f"0x{BITFIELD_LOOKUP_END:X}",
            "size_bytes": BITFIELD_LOOKUP_COUNT * BITFIELD_LOOKUP_ENTRY_SIZE,
            "entry_format": "(u32 field_a, u32 field_b)",
            "sentinel": f"0x{sentinel:X}",
            "stats": stats,
            "entries": entries,
        }
    }


# ─── Table 15: SASS Handler Dispatch Table 1 ─────────────────────────

# The SASS encoder uses two handler dispatch tables stored in .rodata.
# Each table is a sequence of sub-tables (one per SM generation or
# encoding context), where each sub-table is a list of 24-byte entries
# mapping opcode IDs to handler functions in .text.
#
# Entry format (two variants observed):
#   Format A: {handler_ptr:u64, zero:u64, opcode_id:u64}  -- first sub-table
#   Format B: {opcode_id:u64, handler_ptr:u64, zero:u64}  -- subsequent sub-tables
#
# Sub-tables are terminated by an entry with opcode_id=0, followed by
# zero-padding (8-byte aligned) before the next sub-table begins.
#
# Table 1 contains ~6900 entries.  Handler pointers in the first
# sub-table point to simple validation stubs (mov eax,1; ret) in the
# 0xC69xxx range, while later sub-tables point to full encoding
# functions spread across a wider .text range.
#
# opcode_id encoding: high byte = category, low byte = variant.

SASS_HANDLER_TABLE_1_START = 0x22C0E00
SASS_HANDLER_TABLE_1_END   = 0x22F1E00  # conservative upper bound


def _parse_sass_handler_entries(br: BinaryReader, region_start: int, region_end: int):
    """Parse a SASS handler dispatch table region into a flat list of entries.

    Handles both entry formats (A and B) and the inter-sub-table zero
    padding.  Returns (entries, sub_table_sizes) where entries is a list
    of dicts and sub_table_sizes records how many entries (including the
    terminator) each sub-table contained."""

    base = br._off(region_start)
    limit = br._off(region_end)
    entries = []
    sub_table_sizes = []
    current_count = 0

    def u64(off):
        return struct.unpack_from('<Q', br.data, off)[0]

    pos = base
    while pos + 24 <= limit:
        v0 = u64(pos)
        v1 = u64(pos + 8)
        v2 = u64(pos + 16)

        # Format A: {handler_ptr, 0, opcode_id}
        if br.is_in_text(v0) and v1 == 0 and v2 < 0x10000:
            entries.append({
                "handler_va": v0,
                "opcode_id": int(v2),
                "entry_va": pos + VA_BASE,
                "format": "A",
            })
            current_count += 1
            if v2 == 0:
                # Terminator: end of sub-table
                sub_table_sizes.append(current_count)
                current_count = 0
                pos += 24
                while pos + 8 <= limit and u64(pos) == 0:
                    pos += 8
                continue
            pos += 24

        # Format B: {opcode_id, handler_ptr, 0}
        elif 0 < v0 < 0x10000 and br.is_in_text(v1) and v2 == 0:
            entries.append({
                "handler_va": v1,
                "opcode_id": int(v0),
                "entry_va": pos + VA_BASE,
                "format": "B",
            })
            current_count += 1
            pos += 24

        # Format A terminator with zero opcode: {handler_ptr, 0, 0}
        elif br.is_in_text(v0) and v1 == 0 and v2 == 0:
            entries.append({
                "handler_va": v0,
                "opcode_id": 0,
                "entry_va": pos + VA_BASE,
                "format": "A",
            })
            current_count += 1
            sub_table_sizes.append(current_count)
            current_count = 0
            pos += 24
            while pos + 8 <= limit and u64(pos) == 0:
                pos += 8

        # Zero padding between sub-tables
        elif v0 == 0:
            if current_count > 0:
                sub_table_sizes.append(current_count)
                current_count = 0
            pos += 8

        else:
            # Non-matching data: skip one qword and try re-aligning
            pos += 8

    if current_count > 0:
        sub_table_sizes.append(current_count)

    return entries, sub_table_sizes


def _build_handler_table_result(entries: list, sub_table_sizes: list,
                                region_start: int, region_end: int,
                                table_label: str) -> dict:
    """Build the JSON-serializable result dict for a handler dispatch table."""
    from collections import Counter

    # Separate terminators (opcode_id == 0) from real entries
    real_entries = [e for e in entries if e["opcode_id"] > 0]
    terminators = [e for e in entries if e["opcode_id"] == 0]

    # Deduplicate: same (handler_va, opcode_id) appearing in multiple sub-tables
    seen = set()
    unique_entries = []
    for e in real_entries:
        key = (e["handler_va"], e["opcode_id"])
        if key not in seen:
            seen.add(key)
            unique_entries.append(e)

    unique_handlers = len(set(e["handler_va"] for e in real_entries))
    unique_opcodes = len(set(e["opcode_id"] for e in real_entries))
    handler_vas = [e["handler_va"] for e in real_entries]

    # Category distribution
    cat_dist = Counter((e["opcode_id"] >> 8) & 0xFF for e in real_entries)

    # Build output entry list (deduplicated, sorted by opcode_id then handler_va)
    output_entries = []
    for i, e in enumerate(sorted(unique_entries,
                                  key=lambda x: (x["opcode_id"], x["handler_va"]))):
        oid = e["opcode_id"]
        output_entries.append({
            "index": i,
            "opcode_id": oid,
            "opcode_id_hex": f"0x{oid:04X}",
            "category": (oid >> 8) & 0xFF,
            "variant": oid & 0xFF,
            "handler_va": f"0x{e['handler_va']:X}",
        })

    return {
        table_label: {
            "region_start": f"0x{region_start:X}",
            "region_end": f"0x{region_end:X}",
            "region_size_bytes": region_end - region_start,
            "total_raw_entries": len(entries),
            "real_entries": len(real_entries),
            "terminator_entries": len(terminators),
            "deduplicated_entries": len(unique_entries),
            "unique_handlers": unique_handlers,
            "unique_opcode_ids": unique_opcodes,
            "handler_va_range": {
                "min": f"0x{min(handler_vas):X}" if handler_vas else None,
                "max": f"0x{max(handler_vas):X}" if handler_vas else None,
            },
            "sub_table_count": len(sub_table_sizes),
            "sub_table_sizes": sub_table_sizes,
            "category_distribution": {
                f"0x{cat:02X}": count
                for cat, count in sorted(cat_dist.items())
            },
            "entries": output_entries,
        }
    }


def extract_sass_handler_dispatch_1(br: BinaryReader) -> dict:
    """Extract SASS Handler Dispatch Table 1 from the .rodata region
    0x22C0E00-0x22F1E00.  Maps opcode IDs to encoding handler functions
    (validation stubs in the first sub-table, full encoders in later ones)."""

    entries, sub_sizes = _parse_sass_handler_entries(
        br, SASS_HANDLER_TABLE_1_START, SASS_HANDLER_TABLE_1_END)

    real = [e for e in entries if e["opcode_id"] > 0]
    print(f"    Parsed {len(entries)} raw entries ({len(real)} with opcode_id > 0)")

    bad_ptrs = [e for e in real if not br.is_in_text(e["handler_va"])]
    if bad_ptrs:
        print(f"    WARNING: {len(bad_ptrs)} entries have handler_va outside .text",
              file=sys.stderr)

    return _build_handler_table_result(
        entries, sub_sizes,
        SASS_HANDLER_TABLE_1_START, SASS_HANDLER_TABLE_1_END,
        "sass_handler_dispatch_1")


# ─── Table 16: SASS Handler Dispatch Table 2 ─────────────────────────

# Table 2 uses the same format as Table 1 but contains different handler
# functions.  Table 2's handlers (0xCBxxxx range) have proper function
# prologues (push registers, call into encoding engine) indicating these
# are the actual SASS instruction encoding functions, as opposed to
# Table 1's validation stubs.

SASS_HANDLER_TABLE_2_START = 0x2379E00
SASS_HANDLER_TABLE_2_END   = 0x2399E00  # conservative upper bound


def extract_sass_handler_dispatch_2(br: BinaryReader) -> dict:
    """Extract SASS Handler Dispatch Table 2 from the .rodata region
    0x2379E00-0x2399E00.  Maps opcode IDs to full SASS encoding handler
    functions."""

    entries, sub_sizes = _parse_sass_handler_entries(
        br, SASS_HANDLER_TABLE_2_START, SASS_HANDLER_TABLE_2_END)

    real = [e for e in entries if e["opcode_id"] > 0]
    print(f"    Parsed {len(entries)} raw entries ({len(real)} with opcode_id > 0)")

    bad_ptrs = [e for e in real if not br.is_in_text(e["handler_va"])]
    if bad_ptrs:
        print(f"    WARNING: {len(bad_ptrs)} entries have handler_va outside .text",
              file=sys.stderr)

    return _build_handler_table_result(
        entries, sub_sizes,
        SASS_HANDLER_TABLE_2_START, SASS_HANDLER_TABLE_2_END,
        "sass_handler_dispatch_2")


# ─── Table 17: OKT Knob Descriptors ───────────────────────────────────

# The Opcode Knob Table (OKT) is a contiguous array of 9-pointer tuples
# starting at VA 0x1CE9E00.  Each tuple is 72 bytes (9 x 8-byte pointers
# into .rodata string constants).  Layout per entry:
#   [0] type_str     — "OKT_INT", "OKT_NONE", "OKT_FLOAT", etc.
#   [1] name_str     — always empty ("") in .rodata; filled at runtime
#   [2] default_val  — default value as string ("0", "1", etc.)
#   [3] param1       — type-specific parameter
#   [4] param2       — type-specific parameter
#   [5] param3       — type-specific parameter
#   [6] flags_hex    — hex flags string ("0x1", "0x2", "0x3", etc.)
#   [7] offset_hex   — hex offset into .bss knob object ("0x5d0", etc.)
#   [8] separator    — always empty ("") in .rodata
#
# The 'name' field is populated at runtime by the knob registration
# constructors (ctor_005, ctor_007) using the ROT13-encoded knob name
# strings already extracted in Table 11.

OKT_TABLE_VA    = 0x1CE9E00
OKT_TABLE_END   = 0x1CFCE00
OKT_FIELD_COUNT = 9
OKT_ENTRY_SIZE  = OKT_FIELD_COUNT * 8  # 72 bytes


def extract_okt_knob_descriptors(br: BinaryReader) -> dict:
    """Extract OKT (Opcode Knob Table) descriptors from .rodata.

    Each entry is a 9-tuple of string pointers.  We dereference every
    pointer and return the resolved strings.  The 'name' field is always
    empty in the static image (populated at runtime)."""

    region_size = OKT_TABLE_END - OKT_TABLE_VA
    max_entries = region_size // OKT_ENTRY_SIZE  # upper bound

    entries = []
    type_counts: dict[str, int] = {}

    for i in range(max_entries):
        base_va = OKT_TABLE_VA + i * OKT_ENTRY_SIZE
        type_ptr = br.ptr(base_va)

        # Termination: a null pointer or a pointer outside .rodata
        if type_ptr == 0 or not br.is_in_rodata(type_ptr):
            break

        # Read all 9 field pointers
        fields: list[str] = []
        for j in range(OKT_FIELD_COUNT):
            fptr = br.ptr(base_va + j * 8)
            if fptr == 0:
                fields.append("")
            elif br.is_in_rodata(fptr):
                fields.append(br.cstring(fptr, max_len=128))
            else:
                fields.append(f"<ptr_0x{fptr:X}>")

        type_str    = fields[0]
        name_str    = fields[1]
        default_val = fields[2]
        param1      = fields[3]
        param2      = fields[4]
        param3      = fields[5]
        flags_hex   = fields[6]
        offset_hex  = fields[7]
        separator   = fields[8]

        type_counts[type_str] = type_counts.get(type_str, 0) + 1

        entry = {
            "index": i,
            "type": type_str,
            "default": default_val,
            "param1": param1,
            "param2": param2,
            "param3": param3,
            "flags": flags_hex,
            "bss_offset": offset_hex,
        }

        # Include name/separator only when non-empty (saves space; they
        # are always "" in practice for the static image)
        if name_str:
            entry["name"] = name_str
        if separator:
            entry["separator"] = separator

        entries.append(entry)

    # Validate: bss_offset values should be monotonically non-decreasing hex strings
    offsets = []
    for e in entries:
        try:
            offsets.append(int(e["bss_offset"], 16))
        except (ValueError, KeyError):
            offsets.append(-1)

    monotonic_violations = 0
    for k in range(1, len(offsets)):
        if offsets[k] != -1 and offsets[k - 1] != -1 and offsets[k] < offsets[k - 1]:
            monotonic_violations += 1

    if monotonic_violations:
        print(f"    INFO: {monotonic_violations} non-monotonic bss_offset transitions in OKT", file=sys.stderr)

    return {
        "okt_knob_descriptors": {
            "source_va": f"0x{OKT_TABLE_VA:X}",
            "end_va": f"0x{OKT_TABLE_END:X}",
            "entry_count": len(entries),
            "entry_size_bytes": OKT_ENTRY_SIZE,
            "type_distribution": dict(sorted(type_counts.items(), key=lambda x: -x[1])),
            "note": "Name field is always empty in static image; populated at runtime "
                    "by ctor_005/ctor_007 using ROT13 knob name strings from Table 11.",
            "entries": entries,
        }
    }


# ─── Table 18: Embedded PTX Intrinsic Source ─────────────────────────

# The ptxas binary embeds ~1080 PTX function declarations (prototypes)
# for built-in intrinsics.  These are NUL-terminated ".weak .func ..."
# strings packed contiguously starting at VA 0x1D1E200.
#
# Preceding this block (VA 0x1D15E00-0x1D1E200) is a string table with
# function names, error messages, and CRC values used by the intrinsic
# lookup machinery.  We extract both regions.

PTX_INTRINSIC_SOURCE_VA  = 0x1D1E200
PTX_INTRINSIC_SOURCE_END = 0x1D4B777  # byte after last NUL terminator
PTX_STRING_TABLE_VA      = 0x1D15E00
PTX_STRING_TABLE_END     = 0x1D1E200


def _extract_func_name(decl: str) -> str:
    """Extract the function name from a .weak .func PTX declaration."""
    m = re.search(r'\.func\s+(?:\([^)]*\)\s+)?(\w+)', decl)
    return m.group(1) if m else ""


def _categorize_intrinsic(name: str) -> str:
    """Assign a category based on the function name prefix."""
    prefixes = [
        ("__cuda_sm20_",        "sm20_math"),
        ("__cuda_sm70_",        "sm70_intrinsics"),
        ("__cuda_sm80_",        "sm80_intrinsics"),
        ("__cuda_sm90_",        "sm90_intrinsics"),
        ("__cuda_sm100_",       "sm100_intrinsics"),
        ("__cuda_wmma_",        "wmma"),
        ("__cuda_mma_",         "mma"),
        ("__cuda_reduxsync_",   "redux_sync"),
        ("__cuda_sanitizer_",   "sanitizer"),
        ("__cuda_",             "cuda_other"),
    ]
    for prefix, cat in prefixes:
        if name.startswith(prefix):
            return cat
    return "other"


def extract_embedded_ptx_intrinsics(br: BinaryReader) -> dict:
    """Extract embedded PTX intrinsic function declarations from .rodata.

    Returns the full text of each declaration along with parsed metadata
    (function name, return type, parameter list, category)."""

    # ── PTX function declarations ──
    start_off = br._off(PTX_INTRINSIC_SOURCE_VA)
    end_off = br._off(PTX_INTRINSIC_SOURCE_END)
    ptx_data = br.data[start_off:end_off]

    entries = []
    categories: dict[str, int] = {}
    pos = 0
    while pos < len(ptx_data):
        if ptx_data[pos] == 0:
            pos += 1
            continue
        nul = ptx_data.find(b'\x00', pos)
        if nul < 0:
            break
        chunk = ptx_data[pos:nul].strip()
        pos = nul + 1
        if len(chunk) < 10:
            continue
        try:
            text = chunk.decode('ascii')
        except UnicodeDecodeError:
            continue
        if '.func' not in text:
            continue

        func_name = _extract_func_name(text)
        category = _categorize_intrinsic(func_name)
        categories[category] = categories.get(category, 0) + 1

        # Parse return type from (.reg .type %name) pattern
        ret_match = re.search(r'\.func\s+\(([^)]+)\)', text)
        ret_type = ret_match.group(1).strip() if ret_match else "(void)"

        va = PTX_INTRINSIC_SOURCE_VA + (pos - len(chunk) - 1)
        entries.append({
            "index": len(entries),
            "va": f"0x{va:X}",
            "function_name": func_name,
            "return_type": ret_type,
            "category": category,
            "declaration": text.strip(),
        })

    # ── Preceding string table ──
    strtab_start_off = br._off(PTX_STRING_TABLE_VA)
    strtab_end_off = br._off(PTX_STRING_TABLE_END)
    strtab_data = br.data[strtab_start_off:strtab_end_off]

    string_table = []
    pos = 0
    while pos < len(strtab_data):
        if strtab_data[pos] == 0:
            pos += 1
            continue
        nul = strtab_data.find(b'\x00', pos)
        if nul < 0:
            break
        chunk = strtab_data[pos:nul]
        pos = nul + 1
        if len(chunk) < 3:
            continue
        try:
            text = chunk.decode('ascii')
            if all(c.isprintable() for c in text):
                va = PTX_STRING_TABLE_VA + (pos - len(chunk) - 1)
                string_table.append({"va": f"0x{va:X}", "text": text})
        except UnicodeDecodeError:
            pass

    return {
        "embedded_ptx_intrinsics": {
            "source_va": f"0x{PTX_INTRINSIC_SOURCE_VA:X}",
            "end_va": f"0x{PTX_INTRINSIC_SOURCE_END:X}",
            "size_bytes": PTX_INTRINSIC_SOURCE_END - PTX_INTRINSIC_SOURCE_VA,
            "entry_count": len(entries),
            "categories": dict(sorted(categories.items(), key=lambda x: -x[1])),
            "entries": entries,
        },
        "ptx_intrinsic_string_table": {
            "source_va": f"0x{PTX_STRING_TABLE_VA:X}",
            "end_va": f"0x{PTX_STRING_TABLE_END:X}",
            "size_bytes": PTX_STRING_TABLE_END - PTX_STRING_TABLE_VA,
            "entry_count": len(string_table),
            "entries": string_table,
        },
    }


# ─── Table 19: Supplemental Compiler Pass Names (ROT13) ──────────────

# The scheduling/scoreboard pass names are ROT13-encoded CamelCase strings
# packed in a contiguous region of .rodata.  Unlike the knob name strings
# (Table 11), these are pure pass/feature identifiers used by the instruction
# scheduler, scoreboard allocator, and related backend phases.
#
# A small number of plaintext entries (OptimizeNaNOrZero, HoistInvariants,
# ConvertMemoryToRegisterOrUniform) appear at the tail of the region --
# these are compiler phase names stored without ROT13 encoding.

PASS_NAME_REGION_VA  = 0x21DC308
PASS_NAME_REGION_END = 0x21DD248  # byte after last entry's NUL

# Known plaintext constants in this region (not ROT13-encoded).
_PASS_PLAINTEXT = frozenset({
    "OptimizeNaNOrZero",
    "HoistInvariants",
    "ConvertMemoryToRegisterOrUniform",
})


def extract_supplemental_pass_names(br: BinaryReader) -> dict:
    """Extract ROT13-encoded compiler pass/feature names from .rodata.

    Each entry is a NUL-terminated string.  ROT13-encoded entries are
    decoded to their human-readable form.  A handful of plaintext entries
    at the tail of the region are marked with is_rot13=False."""

    start_off = br._off(PASS_NAME_REGION_VA)
    end_off = br._off(PASS_NAME_REGION_END)
    data = br.data[start_off:end_off]

    entries = []
    pos = 0
    while pos < len(data):
        if data[pos] == 0:
            pos += 1
            continue
        nul = data.find(b'\x00', pos)
        if nul < 0:
            break
        raw_bytes = data[pos:nul]
        pos = nul + 1

        try:
            raw = raw_bytes.decode('ascii')
        except UnicodeDecodeError:
            continue

        # Accept only alphanumeric + underscore, minimum 4 chars, starts with upper
        if len(raw) < 4:
            continue
        if not all(c.isalnum() or c in '_' for c in raw):
            continue

        decoded = rot13(raw)

        # Must look like a CamelCase identifier when decoded
        if not decoded[0].isupper():
            continue
        if not any(c.islower() for c in decoded):
            continue

        is_rot13 = raw not in _PASS_PLAINTEXT
        name = decoded if is_rot13 else raw
        va = PASS_NAME_REGION_VA + (pos - len(raw) - 1)

        entries.append({
            "va": f"0x{va:X}",
            "rot13": raw,
            "name": name,
            "is_rot13": is_rot13,
        })

    rot13_count = sum(1 for e in entries if e["is_rot13"])
    plain_count = len(entries) - rot13_count

    return {
        "supplemental_pass_names": {
            "source_va": f"0x{PASS_NAME_REGION_VA:X}",
            "end_va": f"0x{PASS_NAME_REGION_END:X}",
            "total_count": len(entries),
            "rot13_count": rot13_count,
            "plaintext_count": plain_count,
            "note": "Scheduling/scoreboard pass and feature names. ROT13-encoded in binary; "
                    "decoded to human-readable CamelCase. A few tail entries are plaintext.",
            "entries": entries,
        }
    }


# ─── Table 20: Per-SM Functional Unit Latency Tables ─────────────────

# Each entry is 72 bytes:
#   i32  unit_id           (scheduling class ID, matches dep rule field[0])
#   i32  reserved          (always 0)
#   u8[8] pipe_masks_a     (per-pipeline availability mask; 0xFF = unused pipeline)
#   u8[8] pipe_masks_b     (secondary mask/flags; 0xFF = unused)
#   i32[12] sched_params   (latency, throughput, issue delay, stall counts, etc.)
#
# Three shared tables cover all SM generations:
#   sm_8x_shared:  sm_80/86/89/90/90a  (256 entries)
#   sm_10x_shared: sm_100/103           (430 entries)
#   sm_7x_shared:  sm_60/70/72/75      (619 entries)

LATENCY_TABLES = {
    "sm_8x_shared":  {"start": 0x2297C00, "end": 0x229C400, "sm_list": "sm_80,sm_86,sm_89,sm_90,sm_90a"},
    "sm_10x_shared": {"start": 0x226C880, "end": 0x2274170, "sm_list": "sm_100,sm_103"},
    "sm_7x_shared":  {"start": 0x2245060, "end": 0x224FE78, "sm_list": "sm_60,sm_70,sm_72,sm_75"},
}

LATENCY_ENTRY_SIZE = 72  # bytes


def extract_latency_tables(br: BinaryReader) -> dict:
    """Extract per-SM functional unit latency tables from .rodata.
    Each 72-byte entry encodes pipeline masks and scheduling parameters
    for one scheduling class (functional unit type)."""

    all_tables = {}
    for label, spec in LATENCY_TABLES.items():
        va_start = spec["start"]
        va_end = spec["end"]
        table_size = va_end - va_start
        entry_count = table_size // LATENCY_ENTRY_SIZE
        remainder = table_size % LATENCY_ENTRY_SIZE

        if remainder != 0:
            print(f"    WARNING: {label} size {table_size} not divisible by "
                  f"{LATENCY_ENTRY_SIZE} (remainder={remainder})", file=sys.stderr)

        entries = []
        for i in range(entry_count):
            va = va_start + i * LATENCY_ENTRY_SIZE
            off = br._off(va)
            chunk = br.data[off:off + LATENCY_ENTRY_SIZE]

            unit_id = struct.unpack_from('<i', chunk, 0)[0]
            reserved = struct.unpack_from('<i', chunk, 4)[0]
            pipe_masks_a = list(chunk[8:16])
            pipe_masks_b = list(chunk[16:24])
            sched_params = list(struct.unpack_from('<12i', chunk, 24))

            entries.append({
                "index": i,
                "unit_id": unit_id,
                "reserved": reserved,
                "pipe_masks_a": pipe_masks_a,
                "pipe_masks_b": pipe_masks_b,
                "sched_params": sched_params,
            })

        # Validate: unit_ids should be non-negative small integers
        invalid_uids = [(e["index"], e["unit_id"]) for e in entries
                        if e["unit_id"] < 0 or e["unit_id"] > 10000]
        if invalid_uids:
            for idx, uid in invalid_uids[:5]:
                print(f"    WARNING: {label}[{idx}] unit_id={uid} out of expected range",
                      file=sys.stderr)

        # Validate: reserved field should always be 0
        nonzero_reserved = [(e["index"], e["reserved"]) for e in entries
                            if e["reserved"] != 0]
        if nonzero_reserved:
            for idx, val in nonzero_reserved[:5]:
                print(f"    WARNING: {label}[{idx}] reserved={val} (expected 0)",
                      file=sys.stderr)

        uid_set = sorted(set(e["unit_id"] for e in entries))

        all_tables[label] = {
            "va_range": f"0x{va_start:X}-0x{va_end:X}",
            "size_bytes": table_size,
            "entry_count": entry_count,
            "entry_size": LATENCY_ENTRY_SIZE,
            "sm_list": spec["sm_list"],
            "unique_unit_ids": len(uid_set),
            "unit_id_range": [uid_set[0], uid_set[-1]] if uid_set else [],
            "entries": entries,
        }

    return {"per_sm_latency_tables": all_tables}


# ─── Table 21: Per-SM Dependency Rule Tables ─────────────────────────

# Each entry is 40 bytes = 10 x i32:
#   i32[0]  unit_id           (scheduling class ID)
#   i32[1]  rule_type         (dependency type: 0=special, 1=standard)
#   i32[2]  latency           (minimum cycles before dependent can issue)
#   i32[3]  throughput_inv    (inverse throughput / issue rate)
#   i32[4]  barrier_latency   (scoreboard wait threshold, often 56=0x38)
#   i32[5]  barrier_throughput (scoreboard throughput)
#   i32[6]  read_latency      (read-after-write latency, -1 = N/A)
#   i32[7]  write_latency     (write-after-read latency, -1 = N/A)
#   i32[8]  stall_cycles      (fixed stall count or encoded parameter)
#   i32[9]  issue_slots       (number of issue slots consumed)
#
# Per-SM variant tables (entry count matches the latency table for same generation):

DEP_RULE_TABLES = {
    "sm_100": {"start": 0x2268440, "end": 0x226C770},
    "sm_103": {"start": 0x2262720, "end": 0x2266A50},
    "sm_80":  {"start": 0x2295400, "end": 0x2297C00},
    "sm_86":  {"start": 0x2292140, "end": 0x2294940},
    "sm_89":  {"start": 0x228EE80, "end": 0x2291680},
    "sm_90":  {"start": 0x228BB80, "end": 0x228E380},
    "sm_90a": {"start": 0x22888C0, "end": 0x228B0C0},
    "sm_70":  {"start": 0x2237480, "end": 0x223D538},
    "sm_72":  {"start": 0x223EE60, "end": 0x2244F18},
    "sm_75":  {"start": 0x222F9E0, "end": 0x2235A98},
    "sm_60":  {"start": 0x2227F40, "end": 0x222DFF8},
}

DEP_RULE_ENTRY_SIZE = 40  # bytes
DEP_RULE_FIELDS = [
    "unit_id", "rule_type", "latency", "throughput_inv",
    "barrier_latency", "barrier_throughput", "read_latency",
    "write_latency", "stall_cycles", "issue_slots",
]


def extract_dependency_rules(br: BinaryReader) -> dict:
    """Extract per-SM dependency rule tables from .rodata.
    Each 40-byte entry defines scheduling dependency constraints
    for one functional unit class on a specific SM variant."""

    all_tables = {}
    for sm_name, spec in DEP_RULE_TABLES.items():
        va_start = spec["start"]
        va_end = spec["end"]
        table_size = va_end - va_start
        entry_count = table_size // DEP_RULE_ENTRY_SIZE
        remainder = table_size % DEP_RULE_ENTRY_SIZE

        if remainder != 0:
            print(f"    WARNING: {sm_name} dep rules size {table_size} not divisible by "
                  f"{DEP_RULE_ENTRY_SIZE} (remainder={remainder})", file=sys.stderr)

        entries = []
        for i in range(entry_count):
            va = va_start + i * DEP_RULE_ENTRY_SIZE
            vals = list(struct.unpack_from('<10i', br.data, br._off(va)))
            entry = {"index": i}
            for j, name in enumerate(DEP_RULE_FIELDS):
                entry[name] = vals[j]
            entries.append(entry)

        uid_set = sorted(set(e["unit_id"] for e in entries))
        latencies = [e["latency"] for e in entries if e["latency"] >= 0]

        all_tables[sm_name] = {
            "va_range": f"0x{va_start:X}-0x{va_end:X}",
            "size_bytes": table_size,
            "entry_count": entry_count,
            "entry_size": DEP_RULE_ENTRY_SIZE,
            "unique_unit_ids": len(uid_set),
            "unit_id_range": [uid_set[0], uid_set[-1]] if uid_set else [],
            "latency_range": [min(latencies), max(latencies)] if latencies else [],
            "entries": entries,
        }

    return {"per_sm_dependency_rules": all_tables}


# ─── Table 22: Per-SM Scoreboard Configuration Tables ────────────────

# Each entry is 88 bytes = 22 x i32:
#   i32[0..17]  up to 6 triplets of (scoreboard_id, threshold, mask_or_flag)
#               Unused triplet slots are all-zero.
#   i32[18..20] padding (always 0)
#   i32[21]     triplet_count (number of valid triplets in this entry)
#
# Per-SM variant tables:

SCOREBOARD_TABLES = {
    "sm_100": {"start": 0x2266A60, "end": 0x2268440},
    "sm_103": {"start": 0x2261740, "end": 0x2262720},
    "sm_80":  {"start": 0x2294940, "end": 0x2295400},
    "sm_86":  {"start": 0x2291680, "end": 0x2292140},
    "sm_89":  {"start": 0x228E380, "end": 0x228EE80},
    "sm_90":  {"start": 0x228B0C0, "end": 0x228BB80},
    "sm_90a": {"start": 0x2287DC0, "end": 0x22888C0},
}

SCOREBOARD_ENTRY_SIZE = 88  # bytes
SCOREBOARD_STRIDE = 22  # i32 count per entry
SCOREBOARD_MAX_TRIPLETS = 6


def extract_scoreboard_configs(br: BinaryReader) -> dict:
    """Extract per-SM scoreboard configuration tables from .rodata.
    Each 88-byte entry defines up to 6 scoreboard barrier triplets
    (scoreboard_id, threshold, mask) that the scheduler uses for
    dependency tracking on a given functional unit class."""

    all_tables = {}
    for sm_name, spec in SCOREBOARD_TABLES.items():
        va_start = spec["start"]
        va_end = spec["end"]
        table_size = va_end - va_start
        full_entries = table_size // SCOREBOARD_ENTRY_SIZE
        remainder = table_size % SCOREBOARD_ENTRY_SIZE

        raw = br.read_bytes(va_start, table_size)

        # Verify remainder is all zeros (trailing padding)
        if remainder > 0:
            trail = raw[full_entries * SCOREBOARD_ENTRY_SIZE:]
            if any(b != 0 for b in trail):
                print(f"    WARNING: {sm_name} scoreboard has non-zero trailing "
                      f"{remainder} bytes", file=sys.stderr)

        entries = []
        for i in range(full_entries):
            off = i * SCOREBOARD_ENTRY_SIZE
            vals = list(struct.unpack_from(f'<{SCOREBOARD_STRIDE}i', raw, off))

            triplet_count = vals[21]
            triplets = []
            for t in range(min(triplet_count, SCOREBOARD_MAX_TRIPLETS)):
                base = t * 3
                triplets.append({
                    "scoreboard_id": vals[base],
                    "threshold": vals[base + 1],
                    "mask": vals[base + 2],
                })

            # Validate: padding fields [18..20] should be zero
            padding = vals[18:21]
            if any(p != 0 for p in padding):
                print(f"    WARNING: {sm_name}[{i}] non-zero padding: {padding}",
                      file=sys.stderr)

            entries.append({
                "index": i,
                "triplet_count": triplet_count,
                "triplets": triplets,
                "raw_i32": vals,
            })

        # Statistics
        max_trip = max((e["triplet_count"] for e in entries), default=0)
        trip_dist = {}
        for e in entries:
            tc = e["triplet_count"]
            trip_dist[tc] = trip_dist.get(tc, 0) + 1

        all_tables[sm_name] = {
            "va_range": f"0x{va_start:X}-0x{va_end:X}",
            "size_bytes": table_size,
            "entry_count": full_entries,
            "entry_size": SCOREBOARD_ENTRY_SIZE,
            "trailing_padding": remainder,
            "max_triplets_used": max_trip,
            "triplet_count_distribution": dict(sorted(trip_dist.items())),
            "entries": entries,
        }

    return {"per_sm_scoreboard_configs": all_tables}


# ─── Table 23: Encoding Tree Structures ───────────────────────────────

# Two regions of hierarchical tree nodes used in instruction encoding decision
# trees.  Each region is a flat array of 16-byte entries {u64 ptr, u64 value}.
# Internal nodes have ptr pointing INTO the same tree region (child list),
# leaf/data entries have encoding IDs and .text function pointers.

ENCODING_TREES = [
    {
        "label": "encoding_tree_1",
        "start_va": 0x233BE00,
        "end_va":   0x2353E00,
        "note": "Primary instruction encoding decision tree",
    },
    {
        "label": "encoding_tree_2",
        "start_va": 0x235CE00,
        "end_va":   0x2379E00,
        "note": "Secondary/extended instruction encoding decision tree",
    },
]


def extract_encoding_trees(br: BinaryReader) -> dict:
    """Extract hierarchical encoding decision trees from .rodata.

    Each tree is a flat array of 16-byte slots {u64 ptr, u64 value}.
    Slots where ptr falls within the tree's own VA range are internal nodes
    (ptr = child array base, value = child count).  All other non-zero
    slots are leaf/data entries (encoding IDs, .text function pointers,
    or padding).  Zero slots are empty."""

    trees = []
    for spec in ENCODING_TREES:
        label = spec["label"]
        start_va = spec["start_va"]
        end_va = spec["end_va"]
        size = end_va - start_va
        entry_count = size // 16

        entries = []
        internal_count = 0
        leaf_count = 0
        zero_count = 0

        for i in range(entry_count):
            va = start_va + i * 16
            v0 = br.u64(va)
            v1 = br.u64(va + 8)

            if v0 == 0 and v1 == 0:
                zero_count += 1
                continue  # skip zero padding in output

            is_internal = start_va <= v0 < end_va
            if is_internal:
                internal_count += 1
                entries.append({
                    "slot": i,
                    "va": f"0x{va:X}",
                    "type": "internal",
                    "child_ptr": f"0x{v0:X}",
                    "child_count": v1,
                })
            else:
                leaf_count += 1
                entries.append({
                    "slot": i,
                    "va": f"0x{va:X}",
                    "type": "leaf",
                    "d0": f"0x{v0:X}",
                    "d1": f"0x{v1:X}",
                    "d0_is_text": br.is_in_text(v0) if v0 != 0 else False,
                    "d1_is_text": br.is_in_text(v1) if v1 != 0 else False,
                })

        trees.append({
            "label": label,
            "start_va": f"0x{start_va:X}",
            "end_va": f"0x{end_va:X}",
            "size_bytes": size,
            "total_slots": entry_count,
            "internal_nodes": internal_count,
            "leaf_entries": leaf_count,
            "zero_slots": zero_count,
            "note": spec["note"],
            "entries": entries,
        })

    return {"encoding_trees": trees}


# ─── Table 24: Register Class Auxiliary Tables ────────────────────────

# Per-SM-generation register class descriptor arrays.  Each region is a
# contiguous array of 64-byte records (16 x u32).  Loaded to the sm_backend
# object at offsets +112/+120/+128/+136/+144/+152 (six pointer slots).
#
# Record layout (16 x u32):
#   d[0]  = register class ID (3=GP, 1=predicate, 5=uniform, 8=pair, etc.)
#   d[1]  = sub-variant A
#   d[2]  = sub-variant B (primary variant index)
#   d[3]  = auxiliary class ref
#   d[4]  = range_lo
#   d[5]  = range_hi
#   d[6..14] = reserved (zero in current binary)
#   d[15] = flag: 1=simple, 2=extended (record uses d[3..5])

REGCLASS_AUX_REGIONS = [
    ("sm_10x", 0x224FE80, 0x22516C0),
    ("sm_8x",  0x2274180, 0x2274780),
    ("sm_7x",  0x21FB680, 0x21FDC00),
]


def extract_register_class_aux(br: BinaryReader) -> dict:
    """Extract per-SM register class auxiliary tables from .rodata.
    Each region is a flat array of 64-byte records."""

    regions = []
    for sm_label, start_va, end_va in REGCLASS_AUX_REGIONS:
        size = end_va - start_va
        record_count = size // 64

        if size % 64 != 0:
            print(f"    WARNING: {sm_label} region size {size} not divisible by 64",
                  file=sys.stderr)

        records = []
        flag_counts = {1: 0, 2: 0}
        for i in range(record_count):
            va = start_va + i * 64
            d = br.u32_array(va, 16)
            flag = d[15]
            flag_counts[flag] = flag_counts.get(flag, 0) + 1

            records.append({
                "index": i,
                "va": f"0x{va:X}",
                "class_id": d[0],
                "sub_variant_a": d[1],
                "sub_variant_b": d[2],
                "aux_class_ref": d[3],
                "range_lo": d[4],
                "range_hi": d[5],
                "flag": flag,
                "raw_u32": d,
            })

        regions.append({
            "sm_generation": sm_label,
            "start_va": f"0x{start_va:X}",
            "end_va": f"0x{end_va:X}",
            "size_bytes": size,
            "record_count": record_count,
            "record_stride": 64,
            "flag_distribution": flag_counts,
            "records": records,
        })

    return {"register_class_aux": regions}


# ─── Table 24b: Register Class Constraint Tables ─────────────────────
#
# Per-SM register operand constraint tables stored at struct offset +0x70
# by sub_ABF590.  Each table is 72 rows of 16 x u32 (64 bytes per row,
# 4608 bytes total).  The last field d[15] is the active_group_count
# indicating how many (class_id, sub_a, sub_b) triplets are populated:
#
#   d[0..2]   = group 0: (class_id, sub_a, sub_b)
#   d[3..5]   = group 1: (class_id, sub_a, sub_b)
#   d[6..8]   = group 2: (class_id, sub_a, sub_b)
#   d[9..11]  = group 3: (class_id, sub_a, sub_b)
#   d[12..14] = group 4: (class_id, sub_a, sub_b)
#   d[15]     = active_group_count (1..5 for SM 5x; varies for other SMs)
#
# SM 3x records may contain embedded 32-bit .rodata VA pointers in the
# class_id fields (e.g., d[0], d[4], d[8], d[12] > 0x1000000).
#
# Rows 0-39 typically have d[0]=0 (anonymous / inline constraints).
# Rows 40-71 have d[0]>0 (class-based constraints referencing a
# register class ID from the regclass_aux table).
#
# SM dispatch mapping (from sub_ABF590):
#   SM 30-35 (0x3001-0x3005): 0x2274780
#   SM 40-41 (0x4000-0x4001): 0x22516C0
#   SM 50-55 (0x5000-0x5005): 0x21FDC00

REGCLASS_CONSTRAINT_REGIONS = [
    ("sm_5x", 0x21FDC00, 0x21FEE00),
    ("sm_4x", 0x22516C0, 0x22528C0),
    ("sm_3x", 0x2274780, 0x2275980),
]


def extract_register_class_constraints(br: BinaryReader) -> dict:
    """Extract per-SM register class constraint tables from .rodata.
    Each region is 72 rows of 64-byte records (16 x u32) describing
    register operand constraints with grouped (class, sub_a, sub_b) triplets."""

    regions = []
    for sm_label, start_va, end_va in REGCLASS_CONSTRAINT_REGIONS:
        size = end_va - start_va
        record_count = size // 64

        if size % 64 != 0:
            print(f"    WARNING: {sm_label} constraint region size {size} not "
                  f"divisible by 64", file=sys.stderr)

        records = []
        group_count_hist = {}
        has_embedded_ptrs = False

        for i in range(record_count):
            va = start_va + i * 64
            d = br.u32_array(va, 16)
            group_count = d[15]
            group_count_hist[group_count] = group_count_hist.get(group_count, 0) + 1

            # Detect embedded .rodata pointers (SM 3x uses truncated 32-bit VAs)
            embedded_vas = []
            for gi in range(5):
                cid = d[gi * 3]
                if RODATA_START <= cid <= RODATA_END:
                    has_embedded_ptrs = True
                    embedded_vas.append(f"0x{cid:X}")

            # Parse groups
            groups = []
            for gi in range(5):
                base_idx = gi * 3
                cid, sub_a, sub_b = d[base_idx], d[base_idx + 1], d[base_idx + 2]
                if cid != 0 or sub_a != 0 or sub_b != 0:
                    entry = {"class_id": cid, "sub_a": sub_a, "sub_b": sub_b}
                    if RODATA_START <= cid <= RODATA_END:
                        entry["class_id_is_va"] = True
                        entry["class_id_hex"] = f"0x{cid:X}"
                    groups.append(entry)

            rec = {
                "index": i,
                "va": f"0x{va:X}",
                "active_group_count": group_count,
                "groups": groups,
                "raw_u32": d,
            }
            if embedded_vas:
                rec["embedded_va_refs"] = embedded_vas
            records.append(rec)

        regions.append({
            "sm_generation": sm_label,
            "start_va": f"0x{start_va:X}",
            "end_va": f"0x{end_va:X}",
            "size_bytes": size,
            "record_count": record_count,
            "record_stride": 64,
            "has_embedded_rodata_pointers": has_embedded_ptrs,
            "group_count_distribution": {
                str(k): v for k, v in sorted(group_count_hist.items())
            },
            "records": records,
        })

    return {"register_class_constraints": regions}


# ─── Table 25: Opcode-to-Pipeline Mapping Tables ─────────────────────

# Sorted arrays of (opcode_id:u32, pipeline_flags:u32) pairs that map
# internal opcode IDs to execution pipeline flags for scheduling.
# Pipeline flags: 0=special, 1=ALU, 2=FP64, 3=SFU/transcendental, 4=other.

OPCODE_PIPELINE_TABLES = [
    ("sm_10x", 0x226C780, 0x226C878),
    ("sm_7x",  0x2244F20, 0x2245048),
]


def extract_opcode_pipeline_map(br: BinaryReader) -> dict:
    """Extract opcode-to-pipeline mapping tables.
    Each table is a sorted array of (opcode_id:u32, pipeline_flags:u32) pairs."""

    tables = []
    for sm_label, start_va, end_va in OPCODE_PIPELINE_TABLES:
        size = end_va - start_va
        pair_count = size // 8

        if size % 8 != 0:
            print(f"    WARNING: {sm_label} pipeline map size {size} not divisible by 8",
                  file=sys.stderr)

        pairs = []
        prev_opid = -1
        is_sorted = True
        flag_histogram = {}

        for i in range(pair_count):
            va = start_va + i * 8
            opid = br.u32(va)
            flags = br.u32(va + 4)

            if opid < prev_opid:
                is_sorted = False
            prev_opid = opid

            flag_histogram[flags] = flag_histogram.get(flags, 0) + 1

            pairs.append({
                "index": i,
                "opcode_id": opid,
                "pipeline_flags": flags,
            })

        if not is_sorted:
            print(f"    WARNING: {sm_label} pipeline map opcode IDs are NOT sorted",
                  file=sys.stderr)

        tables.append({
            "sm_generation": sm_label,
            "start_va": f"0x{start_va:X}",
            "end_va": f"0x{end_va:X}",
            "size_bytes": size,
            "pair_count": pair_count,
            "is_sorted": is_sorted,
            "pipeline_flag_histogram": {str(k): v for k, v in sorted(flag_histogram.items())},
            "entries": pairs,
        })

    return {"opcode_pipeline_map": tables}


# ─── Table 26: Scheduling Backend Vtable ──────────────────────────────

# The scheduling backend vtable at 0x21DBC80 contains 77 consecutive
# function pointers for the 656-byte scheduling backend object.
# Structure: 8 core methods + 3x23 per-SM-generation pipeline query methods.
#
# Core methods (indices 0-7):
#   [0-1] = base dispatch (0x8DA690)
#   [2]   = complex method A (0x8DC3F0)
#   [3]   = complex method B (0x8DC620)
#   [4-7] = base accessors (0x8DA6xx)
#
# Pipeline query groups (23 methods each, 3 generations):
#   Group A [8-30]:  0x8E0Exx-0x8E10xx
#   Group B [31-53]: 0x8E14xx-0x8E15xx
#   Group C [54-76]: 0x8E22xx-0x8E24xx

SCHED_VTABLE_VA    = 0x21DBC80
SCHED_VTABLE_COUNT = 77


def extract_scheduling_vtable(br: BinaryReader) -> dict:
    """Extract the scheduling backend virtual function table."""

    ptrs = br.ptr_array(SCHED_VTABLE_VA, SCHED_VTABLE_COUNT)
    unique_vas = sorted(set(ptrs))

    # Validate all pointers are in .text
    invalid = [(i, p) for i, p in enumerate(ptrs) if not br.is_in_text(p)]
    if invalid:
        for idx, p in invalid[:5]:
            print(f"    WARNING: sched_vtable[{idx}] = 0x{p:X} not in .text",
                  file=sys.stderr)

    # Classify into structural groups
    core_methods = []
    pipeline_groups = [[], [], []]  # A, B, C

    for i, va in enumerate(ptrs):
        entry = {"index": i, "va": f"0x{va:X}"}
        if i < 8:
            entry["group"] = "core"
            core_methods.append(entry)
        elif i < 31:
            entry["group"] = "pipeline_A"
            entry["pipeline_index"] = i - 8
            pipeline_groups[0].append(entry)
        elif i < 54:
            entry["group"] = "pipeline_B"
            entry["pipeline_index"] = i - 31
            pipeline_groups[1].append(entry)
        else:
            entry["group"] = "pipeline_C"
            entry["pipeline_index"] = i - 54
            pipeline_groups[2].append(entry)

    return {
        "scheduling_vtable": {
            "source_va": f"0x{SCHED_VTABLE_VA:X}",
            "end_va": f"0x{SCHED_VTABLE_VA + SCHED_VTABLE_COUNT * 8:X}",
            "total_entries": SCHED_VTABLE_COUNT,
            "unique_functions": len(unique_vas),
            "unique_function_vas": [f"0x{v:X}" for v in unique_vas],
            "invalid_pointers": len(invalid),
            "structure": {
                "core_methods": core_methods,
                "pipeline_group_A": {
                    "va_range": "0x8E0Exx-0x8E10xx",
                    "count": len(pipeline_groups[0]),
                    "entries": pipeline_groups[0],
                },
                "pipeline_group_B": {
                    "va_range": "0x8E14xx-0x8E15xx",
                    "count": len(pipeline_groups[1]),
                    "entries": pipeline_groups[1],
                },
                "pipeline_group_C": {
                    "va_range": "0x8E22xx-0x8E24xx",
                    "count": len(pipeline_groups[2]),
                    "entries": pipeline_groups[2],
                },
            },
            "all_entries": [
                {"index": i, "va": f"0x{ptrs[i]:X}"}
                for i in range(SCHED_VTABLE_COUNT)
            ],
        }
    }


# ─── Table 27: Register File Configuration Constants ─────────────────

# Per-SM resource limits: GPR banks, predicate regs, uniform regs, barriers,
# warp sizing.  Flat u32 array at VA 0x21CEE00.  The numeric data runs for
# 2784 entries (11,136 bytes); the remaining 1,152 bytes to 0x21D1E00 are
# ASCII diagnostic strings that bleed into the page-aligned region boundary.

REGFILE_CONFIG_VA    = 0x21CEE00
REGFILE_CONFIG_END   = 0x21D1E00   # page-aligned boundary
REGFILE_CONFIG_BYTES = REGFILE_CONFIG_END - REGFILE_CONFIG_VA  # 12288

_REGFILE_FIRST8 = [120, 120, 64, 256, 32, 8, 32, 4]


def extract_register_file_config(br: BinaryReader) -> dict:
    """Extract per-SM register file configuration constants.

    The region is a flat array of u32 resource limit values.  After the
    numeric data (max value 256), the remainder of the page-aligned region
    contains ASCII diagnostic strings which we exclude."""

    total_u32 = REGFILE_CONFIG_BYTES // 4  # 3072
    raw = br.u32_array(REGFILE_CONFIG_VA, total_u32)

    # Find the boundary between numeric config data and trailing ASCII.
    numeric_count = total_u32
    for i, v in enumerate(raw):
        if v > 4096:
            numeric_count = i
            break
    entries = raw[:numeric_count]

    spot_ok = entries[:8] == _REGFILE_FIRST8
    if not spot_ok:
        print(f"    WARNING: First 8 values {entries[:8]} != expected {_REGFILE_FIRST8}",
              file=sys.stderr)

    val_max = max(entries) if entries else 0
    val_counts = {}
    for v in entries:
        val_counts[v] = val_counts.get(v, 0) + 1
    top_values = sorted(val_counts.items(), key=lambda x: -x[1])[:10]

    return {
        "register_file_config": {
            "source_va": f"0x{REGFILE_CONFIG_VA:X}",
            "end_va": f"0x{REGFILE_CONFIG_END:X}",
            "region_bytes": REGFILE_CONFIG_BYTES,
            "numeric_entry_count": numeric_count,
            "trailing_ascii_bytes": (total_u32 - numeric_count) * 4,
            "max_value": val_max,
            "first8_spot_check": "PASS" if spot_ok else "FAIL",
            "value_frequency_top10": [
                {"value": v, "count": c} for v, c in top_values
            ],
            "entries": entries,
        }
    }


# ─── Table 28: SM Version Code Lookup ─────────────────────────────────

# 128-entry u16 table mapping internal ptxas arch indices to SM version
# codes.  Encoding: bits [15:12] = major tens digit, [11:8] = minor ones
# digit, [7:0] = variant (0=base, 1=a, 2=b, 3=c, 4=a-alt, 5=f).

SM_VERSION_CODE_VA    = 0x2020620
SM_VERSION_CODE_COUNT = 128


def extract_sm_version_codes(br: BinaryReader) -> dict:
    """Extract the 128-entry u16 SM version code lookup table."""

    entries = br.u16_array(SM_VERSION_CODE_VA, SM_VERSION_CODE_COUNT)

    decoded = []
    for i, v in enumerate(entries):
        if v == 0:
            decoded.append({"index": i, "code": 0, "sm": None})
            continue
        major10 = (v >> 12) & 0xF
        minor1 = (v >> 8) & 0xF
        sm_num = major10 * 10 + minor1
        variant = v & 0xFF
        suffix = ""
        if variant == 1:
            suffix = "a"
        elif variant == 2:
            suffix = "b"
        elif variant == 3:
            suffix = "c"
        elif variant == 4:
            suffix = "a"  # alternate 'a' encoding (sm_90a)
        elif variant == 5:
            suffix = "f"
        elif variant > 5:
            suffix = f"v{variant}"
        sm_str = f"sm_{sm_num}{suffix}"
        if sm_num < 10 or sm_num > 200 or variant > 10:
            print(f"    WARNING: Index {i} code 0x{v:04X} decodes to implausible {sm_str}",
                  file=sys.stderr)
            decoded.append({"index": i, "code": v, "code_hex": f"0x{v:04X}",
                            "sm": None, "note": "implausible"})
        else:
            decoded.append({"index": i, "code": v, "code_hex": f"0x{v:04X}",
                            "sm": sm_str, "sm_num": sm_num, "variant": variant})

    non_zero = [d for d in decoded if d["code"] != 0]
    valid_sm = [d for d in decoded if d.get("sm") is not None]

    return {
        "sm_version_codes": {
            "source_va": f"0x{SM_VERSION_CODE_VA:X}",
            "entry_count": SM_VERSION_CODE_COUNT,
            "entry_width": "u16",
            "encoding": "bits[15:12]=major*10, [11:8]=minor, [7:0]=variant",
            "non_zero_count": len(non_zero),
            "valid_sm_count": len(valid_sm),
            "entries": decoded,
        }
    }


# ─── Table 29: SM Scheduling Parameter Seeds ─────────────────────────

# Triplet array {sm_id, gen_code, variant} of u32 at VA 0x1D16148.
# 50 entries (including null-padding slots) terminated by sentinel
# {0xFF, 0x00, 0xFF00}.  Three logical segments:
#   [0..7]   : Blackwell/Thor subset with padding
#   [8..17]  : Hopper/Blackwell subset with padding
#   [18..49] : Full architecture list (Fermi through Blackwell)

SM_SCHED_SEEDS_VA    = 0x1D16148
SM_SCHED_SEEDS_COUNT = 50

_GEN_NAMES = {
    0: "none", 1: "Fermi", 2: "Kepler", 3: "Maxwell", 4: "Pascal",
    5: "Volta", 6: "Turing", 7: "Ampere", 8: "Hopper", 9: "Thor",
}


def extract_sm_scheduling_seeds(br: BinaryReader) -> dict:
    """Extract SM scheduling parameter seed triplets."""

    entries = []
    for i in range(SM_SCHED_SEEDS_COUNT):
        va = SM_SCHED_SEEDS_VA + i * 12
        sm_id = br.u32(va)
        gen_code = br.u32(va + 4)
        variant = br.u32(va + 8)
        gen_name = _GEN_NAMES.get(gen_code, f"gen{gen_code}")
        entries.append({
            "index": i,
            "sm_id": sm_id,
            "sm": f"sm_{sm_id}" if 0 < sm_id < 200 else None,
            "gen_code": gen_code,
            "gen_name": gen_name,
            "variant": variant,
        })

    # Verify sentinel follows the 50th entry
    sentinel_va = SM_SCHED_SEEDS_VA + SM_SCHED_SEEDS_COUNT * 12
    sent_sm = br.u32(sentinel_va)
    sent_gen = br.u32(sentinel_va + 4)
    sent_var = br.u32(sentinel_va + 8)
    sentinel_ok = (sent_sm == 0xFF and sent_gen == 0 and sent_var == 0xFF00)
    if not sentinel_ok:
        print(f"    WARNING: Expected sentinel {{0xFF,0,0xFF00}}, got "
              f"{{0x{sent_sm:X},0x{sent_gen:X},0x{sent_var:X}}}",
              file=sys.stderr)

    active = [e for e in entries if e["sm_id"] > 0 and e["sm_id"] < 200]
    null_padding = [e for e in entries if e["sm_id"] == 0]

    return {
        "sm_scheduling_seeds": {
            "source_va": f"0x{SM_SCHED_SEEDS_VA:X}",
            "entry_count": SM_SCHED_SEEDS_COUNT,
            "entry_stride": 12,
            "entry_format": "{u32 sm_id, u32 gen_code, u32 variant}",
            "active_entries": len(active),
            "null_padding_entries": len(null_padding),
            "sentinel_check": "PASS" if sentinel_ok else "FAIL",
            "gen_code_legend": _GEN_NAMES,
            "segments": [
                {"name": "blackwell_thor_subset", "indices": "0-7"},
                {"name": "hopper_blackwell_subset", "indices": "8-17"},
                {"name": "full_architecture_list", "indices": "18-49"},
            ],
            "entries": entries,
        }
    }


# ─── Table 30: SM ID Enumeration ─────────────────────────────────────

# Canonical list of SM compute capability numbers at VA 0x1CE7F80.
# 28 u32 entries with 2 null-separator slots (indices 15 and 24).

SM_ID_ENUM_VA    = 0x1CE7F80
SM_ID_ENUM_COUNT = 28


def extract_sm_id_enumeration(br: BinaryReader) -> dict:
    """Extract the canonical SM ID enumeration table."""

    raw = br.u32_array(SM_ID_ENUM_VA, SM_ID_ENUM_COUNT)

    entries = []
    sm_ids = []
    for i, v in enumerate(raw):
        if v == 0:
            entries.append({"index": i, "sm_id": 0, "sm": None,
                            "note": "null separator"})
        elif v < 200:
            entries.append({"index": i, "sm_id": v, "sm": f"sm_{v}"})
            sm_ids.append(v)
        else:
            print(f"    WARNING: SM ID enum[{i}] = {v} (out of range)",
                  file=sys.stderr)
            entries.append({"index": i, "sm_id": v,
                            "note": "out of range"})

    expected_sms = {30, 32, 35, 37, 50, 52, 53, 60, 61, 62, 70, 72, 73,
                    75, 80, 86, 87, 88, 89, 90, 100, 101, 103, 110, 120, 121}
    found_sms = set(sm_ids)
    missing = expected_sms - found_sms
    extra = found_sms - expected_sms
    if missing:
        print(f"    WARNING: Missing expected SM IDs: {sorted(missing)}",
              file=sys.stderr)
    if extra:
        print(f"    INFO: Extra SM IDs not in expected set: {sorted(extra)}",
              file=sys.stderr)

    return {
        "sm_id_enumeration": {
            "source_va": f"0x{SM_ID_ENUM_VA:X}",
            "entry_count": SM_ID_ENUM_COUNT,
            "active_sm_count": len(sm_ids),
            "null_separator_indices": [i for i, e in enumerate(entries)
                                       if e.get("note") == "null separator"],
            "sm_ids": sm_ids,
            "coverage_check": {
                "expected": sorted(expected_sms),
                "found": sorted(found_sms),
                "missing": sorted(missing),
                "extra": sorted(extra),
            },
            "entries": entries,
        }
    }


# ─── Table 31: Extended ROT13 SASS Names ─────────────────────────────

# VA 0x21CAE00-0x21CEE00 (16,384 bytes): ROT13-encoded SASS instruction
# mnemonics and related strings (FFMA2, FENCE.T, FCHK, ...).

EXTENDED_SASS_NAMES_VA  = 0x21CAE00
EXTENDED_SASS_NAMES_END = 0x21CEE00


def extract_extended_sass_names(br: BinaryReader) -> dict:
    """Extract ROT13-encoded SASS instruction names from the extended region."""

    region_size = EXTENDED_SASS_NAMES_END - EXTENDED_SASS_NAMES_VA
    start_off = br._off(EXTENDED_SASS_NAMES_VA)
    raw = br.data[start_off:start_off + region_size]

    all_strings = []
    clean_mnemonics = []
    pos = 0
    while pos < len(raw):
        if raw[pos] == 0:
            pos += 1
            continue
        nul = raw.find(b'\x00', pos)
        if nul < 0:
            break
        try:
            s = raw[pos:nul].decode('ascii')
            d = rot13(s)
            va = EXTENDED_SASS_NAMES_VA + pos
            entry = {"va": f"0x{va:X}", "rot13": s, "decoded": d,
                     "length": len(s)}
            if len(s) >= 2:
                all_strings.append(entry)
            if (len(s) >= 2 and
                    any(c.isupper() for c in d) and
                    all(c.isalnum() or c in '._() *{}|' for c in d)):
                clean_mnemonics.append(entry)
        except UnicodeDecodeError:
            pass
        pos = nul + 1

    return {
        "extended_sass_names": {
            "source_va": f"0x{EXTENDED_SASS_NAMES_VA:X}",
            "end_va": f"0x{EXTENDED_SASS_NAMES_END:X}",
            "region_bytes": region_size,
            "total_strings_ge2": len(all_strings),
            "clean_mnemonic_count": len(clean_mnemonics),
            "clean_mnemonics": clean_mnemonics,
            "all_strings": all_strings,
        }
    }


# ─── Table 32: ROT13 Modifier Format Strings ─────────────────────────

# VA 0x21C5E00-0x21CAE00 (20,480 bytes): ROT13-encoded instruction modifier
# format templates (e.g., "SYNCS.ARRIVE.A1TR.ART0.A0TR.A0TX").

MODIFIER_FMTSTR_VA  = 0x21C5E00
MODIFIER_FMTSTR_END = 0x21CAE00


def extract_modifier_format_strings(br: BinaryReader) -> dict:
    """Extract ROT13-encoded modifier format strings."""

    region_size = MODIFIER_FMTSTR_END - MODIFIER_FMTSTR_VA
    start_off = br._off(MODIFIER_FMTSTR_VA)
    raw = br.data[start_off:start_off + region_size]

    all_strings = []
    format_templates = []
    pos = 0
    while pos < len(raw):
        if raw[pos] == 0:
            pos += 1
            continue
        nul = raw.find(b'\x00', pos)
        if nul < 0:
            break
        try:
            s = raw[pos:nul].decode('ascii')
            d = rot13(s)
            va = MODIFIER_FMTSTR_VA + pos
            entry = {"va": f"0x{va:X}", "rot13": s, "decoded": d,
                     "length": len(s)}
            if len(s) >= 2:
                all_strings.append(entry)
            if len(s) >= 3 and '.' in d and any(c.isupper() for c in d):
                format_templates.append(entry)
        except UnicodeDecodeError:
            pass
        pos = nul + 1

    return {
        "modifier_format_strings": {
            "source_va": f"0x{MODIFIER_FMTSTR_VA:X}",
            "end_va": f"0x{MODIFIER_FMTSTR_END:X}",
            "region_bytes": region_size,
            "total_strings_ge2": len(all_strings),
            "format_template_count": len(format_templates),
            "format_templates": format_templates,
            "all_strings": all_strings,
        }
    }


# ─── Table: Modifier Value Lookup Tables ──────────────────────────────

# ~43 small arrays at VA 0x22FCD20-0x22FD580 that map Ori IR modifier
# enum values to SASS binary encoding values.  Each table is indexed by
# (ir_modifier_value - enum_base) and returns the corresponding encoding
# integer (or 0xFFFFFFFF for "invalid").
#
# Three element types coexist in the region:
#   - DWORD arrays: small integer values (modifier mappings), most tables
#   - BYTE arrays: identity or stride-pattern byte LUTs (64-256 entries)
#
# The two most-shared tables are:
#   dword_22FD498[3] = [0,1,2]   (tristate, 63 helper funcs)
#   dword_22FD570[4] = [0,1,2,3] (quaternary, 44 helper funcs)

MOD_VALUE_REGION_START = 0x22FCD20
MOD_VALUE_REGION_END   = 0x22FD580

# Known sub-tables (VA -> (count, label, element_type, usage_count))
_MOD_VALUE_KNOWN_TABLES = {
    0x22FCD20: (3,  "rounding_mode_023",     "dword", None),
    0x22FCD30: (5,  "gap_table_neg1",        "dword", None),
    0x22FCD50: (6,  "gap_table_dual_neg1",   "dword", None),
    0x22FCD70: (4,  "swap_01",               "dword", None),
    0x22FCD80: (4,  "gap_no_3",              "dword", None),
    0x22FCD90: (4,  "fold_last_two",         "dword", None),
    0x22FCDA0: (19, "large_jump_9_to_12",    "dword", None),
    0x22FCDF4: (4,  "fold_3_3",              "dword", None),
    0x22FCE1C: (15, "complex_remapping_15",  "dword", None),
    0x22FCE60: (11, "alt_remapping_11",      "dword", None),
    0x22FCEA4: (18, "pair_fold_group",       "dword", None),
    0x22FCEF4: (4,  "identity_4a",           "dword", None),
    0x22FCF10: (13, "compound_remap_13",     "dword", None),
    0x22FCF50: (6,  "offset_2_to_6",         "dword", None),
    0x22FCF70: (5,  "rotation",              "dword", None),
    0x22FCF90: (5,  "fold_0_1_1_2_2",        "dword", None),
    0x22FCFAC: (2,  "pair_1_3",              "dword", None),
    0x22FCFC4: (9,  "fold_2_dup",            "dword", None),
    0x22FCFF0: (9,  "split_block",           "dword", None),
    0x22FD020: (64, "identity_byte_64",      "byte",  None),
    0x22FD060: (36, "stride_skip_byte_36",   "byte",  None),
    0x22FD0A0: (96, "identity_byte_96",      "byte",  None),
    0x22FD100: (8,  "small_identity_8",      "dword", None),
    0x22FD150: (6,  "skip_4",                "dword", None),
    0x22FD170: (5,  "skip_1_3",              "dword", None),
    0x22FD18C: (9,  "stride_9_then_1_4",     "dword", None),
    0x22FD1B8: (5,  "fold_1_2_2_3_4",        "dword", None),
    0x22FD1E0: (13, "jump_5_to_9",           "dword", None),
    0x22FD220: (256, "identity_byte_256",    "byte",  None),
    0x22FD320: (3,  "power_encoding_1_3_15", "dword", None),
    0x22FD354: (10, "identity_7_plus_3",     "dword", 10),
    0x22FD384: (22, "identity_22",           "dword", None),
    0x22FD3E4: (17, "identity_17_rotated",   "dword", None),
    0x22FD440: (13, "identity_8_dup_tail",   "dword", None),
    0x22FD480: (5,  "identity_5",            "dword", 13),
    0x22FD498: (3,  "tristate",              "dword", 63),
    0x22FD4C0: (11, "identity_11",           "dword", None),
    0x22FD500: (9,  "identity_9",            "dword", None),
    0x22FD540: (10, "identity_10",           "dword", None),
    0x22FD570: (4,  "quaternary",            "dword", 44),
}


def extract_modifier_value_tables(br: BinaryReader) -> dict:
    """Extract modifier value lookup tables from 0x22FCD20-0x22FD580.

    Each sub-table maps IR modifier enum values to SASS encoding values.
    Tables are identified by their start VA and classified as DWORD or BYTE
    type depending on element size.  DWORD tables contain individual u32
    mapping values; BYTE tables contain per-byte identity or stride LUTs."""

    region_off = br._off(MOD_VALUE_REGION_START)
    region_size = MOD_VALUE_REGION_END - MOD_VALUE_REGION_START
    raw = br.data[region_off:region_off + region_size]

    tables = []

    for va, (count, label, elem_type, usage) in sorted(_MOD_VALUE_KNOWN_TABLES.items()):
        off = va - MOD_VALUE_REGION_START
        if off < 0 or off >= region_size:
            print(f"    WARNING: modifier table {label} VA 0x{va:X} outside region", file=sys.stderr)
            continue

        if elem_type == "byte":
            byte_vals = list(raw[off:off + count])
            is_identity = (byte_vals == list(range(count)))
            tables.append({
                "va": f"0x{va:X}",
                "label": label,
                "element_type": "byte",
                "count": count,
                "is_identity": is_identity,
                "values": byte_vals,
                "usage_count": usage,
            })
        else:
            dword_vals = list(struct.unpack_from(f'<{count}I', raw, off))
            clean = [v if v != 0xFFFFFFFF else -1 for v in dword_vals]
            is_identity = (clean == list(range(count)))
            has_gaps = any(v == -1 for v in clean)
            tables.append({
                "va": f"0x{va:X}",
                "label": label,
                "element_type": "dword",
                "count": count,
                "is_identity": is_identity,
                "has_invalid_gaps": has_gaps,
                "values": clean,
                "usage_count": usage,
            })

    # Spot-check the two most important tables
    spot_checks = []
    tri_off = br._off(0x22FD498)
    tri = list(struct.unpack_from('<3I', br.data, tri_off))
    if tri != [0, 1, 2]:
        spot_checks.append(f"tristate expected [0,1,2] got {tri}")

    quad_off = br._off(0x22FD570)
    quad = list(struct.unpack_from('<4I', br.data, quad_off))
    if quad != [0, 1, 2, 3]:
        spot_checks.append(f"quaternary expected [0,1,2,3] got {quad}")

    if spot_checks:
        for msg in spot_checks:
            print(f"    SPOT CHECK FAIL: {msg}", file=sys.stderr)

    identity_count = sum(1 for t in tables if t["is_identity"])
    dword_count = sum(1 for t in tables if t["element_type"] == "dword")
    byte_count = sum(1 for t in tables if t["element_type"] == "byte")

    return {
        "modifier_value_tables": {
            "region_start": f"0x{MOD_VALUE_REGION_START:X}",
            "region_end": f"0x{MOD_VALUE_REGION_END:X}",
            "region_size_bytes": region_size,
            "total_tables": len(tables),
            "dword_tables": dword_count,
            "byte_tables": byte_count,
            "identity_tables": identity_count,
            "spot_check_failures": spot_checks,
            "tables": tables,
        }
    }


# ─── Table: Instruction Legality Table ────────────────────────────────

# Sparse int32 array at VA 0x22FEE00-0x2339E00 (241,664 bytes = 60,416 DWORDs).
# Indexed by (opcode, modifier_combination) tuples; non-zero values encode
# legality flags per SM generation.  ~68.4% of entries are zero (unused combo).
#
# Non-zero value categories:
#   - Small values (< 0x10000): packed modifier/flag encoding
#   - 0x08000000: standalone flag-only marker
#   - value | 0x08000000: value with flag bit set
#   - Large values (0x01xxxxxx range): context-dependent

LEGALITY_TABLE_START = 0x22FEE00
LEGALITY_TABLE_END   = 0x2339E00
LEGALITY_TABLE_SIZE  = LEGALITY_TABLE_END - LEGALITY_TABLE_START  # 241,664 bytes


def extract_instruction_legality(br: BinaryReader) -> dict:
    """Extract the instruction legality table as a sparse representation.

    Only non-zero entries are stored, each with its DWORD index and value.
    The full table has 60,416 entries but only ~19,000 are non-zero."""

    off = br._off(LEGALITY_TABLE_START)
    count = LEGALITY_TABLE_SIZE // 4
    raw = struct.unpack_from(f'<{count}I', br.data, off)

    sparse = []
    for i, v in enumerate(raw):
        if v != 0:
            sparse.append({"index": i, "value": v})

    nz_count = len(sparse)
    zero_pct = 100.0 * (1 - nz_count / count)

    flag_only = 0
    with_flag = 0
    small_vals = 0
    large_vals = 0
    unique_vals = set()

    for entry in sparse:
        v = entry["value"]
        unique_vals.add(v)
        if v == 0x08000000:
            flag_only += 1
        elif v & 0x08000000:
            with_flag += 1
        elif v < 0x10000:
            small_vals += 1
        else:
            large_vals += 1

    for entry in sparse:
        v = entry["value"]
        if v >= 0x10000:
            entry["value_hex"] = f"0x{v:X}"

    first_nz_idx = sparse[0]["index"] if sparse else -1
    last_nz_idx = sparse[-1]["index"] if sparse else -1
    first_nz_va = LEGALITY_TABLE_START + first_nz_idx * 4 if first_nz_idx >= 0 else 0
    last_nz_va = LEGALITY_TABLE_START + last_nz_idx * 4 if last_nz_idx >= 0 else 0

    return {
        "instruction_legality": {
            "source_va": f"0x{LEGALITY_TABLE_START:X}",
            "end_va": f"0x{LEGALITY_TABLE_END:X}",
            "size_bytes": LEGALITY_TABLE_SIZE,
            "total_entries": count,
            "nonzero_entries": nz_count,
            "zero_entries": count - nz_count,
            "zero_pct": round(zero_pct, 1),
            "first_nonzero": {
                "index": first_nz_idx,
                "va": f"0x{first_nz_va:X}",
            },
            "last_nonzero": {
                "index": last_nz_idx,
                "va": f"0x{last_nz_va:X}",
            },
            "value_categories": {
                "flag_0x08000000_only": flag_only,
                "value_with_flag": with_flag,
                "small_values_lt_0x10000": small_vals,
                "large_values_ge_0x10000": large_vals,
            },
            "unique_nonzero_values": len(unique_vals),
            "sparse_entries": sparse,
        }
    }



# ─── Table: Operand Resource Strategy Tables ─────────────────────────
#
# VA 0x21FAE00 - 0x21FB640 (2,112 bytes).  Between the message/dispatch
# tables and shared-memory config.  Three sub-regions:
#   A: Six 7-slot C++ vtables (resource-strategy class hierarchy)
#   B: Nine switch/jump tables (.text pointers, resource-cost eval)
#   C: Register-count lookup tables (flag bytes, index map, matrices)

_RESRC_STRATEGY_VA    = 0x21FAE00
_RESRC_STRATEGY_END   = 0x21FB640
_RESRC_STRATEGY_BYTES = _RESRC_STRATEGY_END - _RESRC_STRATEGY_VA  # 2112

_RESRC_VTABLE_VAS = [
    0x21FAE00, 0x21FAE48, 0x21FAE90,
    0x21FAED8, 0x21FAF20, 0x21FAF68,
]
_RESRC_VTABLE_SLOTS  = 7
_RESRC_VTABLE_STRIDE = 0x48

_RESRC_JUMP_TABLES = [
    (0x21FAFA0,  7, "operand_type_dispatch"),
    (0x21FAFD8,  6, "source_field_dispatch"),
    (0x21FB008, 22, "modifier_case_dispatch"),
    (0x21FB0B8, 40, "operand_pair_dispatch"),
    (0x21FB1F8,  7, "dual_operand_dispatch"),
    (0x21FB230,  6, "cost_eval_A"),
    (0x21FB260,  6, "cost_eval_B"),
    (0x21FB290,  6, "cost_eval_C"),
    (0x21FB2C0,  6, "cost_eval_D"),
]

_RESRC_FLAGS_VA      = 0x21FB2F0
_RESRC_IDXMAP_VA     = 0x21FB300
_RESRC_BASESZ_VA     = 0x21FB320
_RESRC_MATRIX_A_VA   = 0x21FB340   # u32[42]
_RESRC_MATRIX_B_VA   = 0x21FB3E8   # u32[150]

_RESRC_FIRST8_A  = [18, 18, 18, 18, 18, 18, 18, 18]
_RESRC_IDXMAP_EX = [0, 1, 2, 3, 4, 5, 1, 0]
_RESRC_BASESZ_EX = [48, 50, 38, 41, 46, 46, 0, 0]


def extract_operand_resource_strategy(br: BinaryReader) -> dict:
    """Extract per-SM operand resource strategy tables.

    Region VA 0x21FAE00 - 0x21FB640 (2,112 bytes): six C++ vtables,
    nine switch/jump tables, and register-count lookup matrices."""

    vtables = []
    for idx, va in enumerate(_RESRC_VTABLE_VAS):
        slots = br.ptr_array(va, _RESRC_VTABLE_SLOTS)
        vtables.append({
            "index": idx, "va": f"0x{va:X}",
            "slot_count": _RESRC_VTABLE_SLOTS,
            "text_pointer_count": sum(1 for p in slots if br.is_in_text(p)),
            "slots": [f"0x{s:X}" for s in slots],
        })
    cs2 = set(vt["slots"][2] for vt in vtables)
    s2_uniform = len(cs2) == 1

    jump_tables = []
    jt_total = 0
    for jva, jcnt, jlbl in _RESRC_JUMP_TABLES:
        raw = br.ptr_array(jva, jcnt)
        freq = {}
        for p in raw:
            freq[p] = freq.get(p, 0) + 1
        dft, dfc = max(freq.items(), key=lambda x: x[1])
        jump_tables.append({
            "label": jlbl, "va": f"0x{jva:X}", "entry_count": jcnt,
            "text_pointer_count": sum(1 for p in raw if br.is_in_text(p)),
            "unique_targets": len(set(raw)),
            "default_target": f"0x{dft:X}", "default_count": dfc,
            "entries": [f"0x{p:X}" for p in raw],
        })
        jt_total += jcnt

    fb = br.read_bytes(_RESRC_FLAGS_VA, 16)
    idxmap = br.u32_array(_RESRC_IDXMAP_VA, 8)
    bsz = br.u32_array(_RESRC_BASESZ_VA, 8)
    ma = br.u32_array(_RESRC_MATRIX_A_VA, 42)
    mb = br.u32_array(_RESRC_MATRIX_B_VA, 150)

    def _aruns(v):
        r, i = [], 0
        while i < len(v):
            if i + 2 < len(v):
                s = v[i + 1] - v[i]
                if s > 0:
                    j = i + 1
                    while j < len(v) and v[j] - v[j - 1] == s:
                        j += 1
                    if j - i >= 3:
                        r.append({"start_index": i, "length": j - i,
                                  "first": v[i], "last": v[j - 1], "step": s})
                        i = j
                        continue
            i += 1
        return r

    allv = ma + mb
    return {
        "operand_resource_strategy": {
            "source_va": f"0x{_RESRC_STRATEGY_VA:X}",
            "end_va": f"0x{_RESRC_STRATEGY_END:X}",
            "size_bytes": _RESRC_STRATEGY_BYTES,
            "part_a_vtables": {
                "count": len(vtables), "slots_per_vtable": _RESRC_VTABLE_SLOTS,
                "stride_bytes": _RESRC_VTABLE_STRIDE,
                "slot2_uniform": s2_uniform,
                "common_slot2": list(cs2)[0] if s2_uniform else None,
                "vtables": vtables,
            },
            "part_b_jump_tables": {
                "count": len(jump_tables), "total_entries": jt_total,
                "tables": jump_tables,
            },
            "part_c_register_counts": {
                "flag_bytes": {
                    "va": f"0x{_RESRC_FLAGS_VA:X}",
                    "array_a": list(fb[:6]), "array_b": list(fb[5:11]),
                    "note": "Two parallel u8[6] at base+0 and base+5, indexed by operand type",
                },
                "index_map": {
                    "va": f"0x{_RESRC_IDXMAP_VA:X}", "values": idxmap,
                    "spot_check": "PASS" if idxmap == _RESRC_IDXMAP_EX else "FAIL",
                    "note": "Maps operand type (0-7) to register-file array selector (0-5)",
                },
                "reg_base_sizes": {
                    "va": f"0x{_RESRC_BASESZ_VA:X}", "values": bsz,
                    "spot_check": "PASS" if bsz == _RESRC_BASESZ_EX else "FAIL",
                    "note": "Base register counts per 3-bit type configuration",
                },
                "matrix_a": {
                    "va": f"0x{_RESRC_MATRIX_A_VA:X}", "element_count": 42,
                    "first8_spot_check": "PASS" if ma[:8] == _RESRC_FIRST8_A else "FAIL",
                    "arithmetic_runs": _aruns(ma), "entries": ma,
                },
                "matrix_b": {
                    "va": f"0x{_RESRC_MATRIX_B_VA:X}", "element_count": 150,
                    "arithmetic_runs": _aruns(mb), "entries": mb,
                },
                "value_range": {
                    "min": min(allv) if allv else 0,
                    "max": max(allv) if allv else 0,
                },
            },
        }
    }


# ─── Table: Per-SM Encoding Handler Dispatch Tables ───────────────────

# Five dispatch tables in .rodata, one per SM generation group.
# Each table is an array of 24-byte entries (3 x u64):
#   {u64 dispatch_opcode, u64 handler_ptr, u64 padding=0}
#
# dispatch_opcode = (format_id << 8) | minor_opcode
# handler_ptr = VA into .text of the encoding handler function
#
# Tables are densely packed with some empty (all-zero) slots.
# Each table has 3000 24-byte slots (72,000 bytes).
#
# SM generation mapping (from MERCURY_TABLES.txt):
#   Table 0: SM 50-7x  (Maxwell/Pascal/Volta) -- base instruction set
#   Table 1: SM 75     (Turing)
#   Table 2: SM 100+   (Blackwell)
#   Table 3: SM 80-8x  (Ampere)
#   Table 4: SM 86-89  (Ampere GA10x variant)

_MERCURY_DISPATCH_TABLES = [
    (0x22E7AD0, "sm50_7x",  "SM 50-7x (Maxwell/Pascal/Volta)"),
    (0x2348FB0, "sm75",     "SM 75 (Turing)"),
    (0x236E160, "sm100",    "SM 100+ (Blackwell)"),
    (0x238C9B0, "sm80_8x",  "SM 80-8x (Ampere)"),
    (0x23A8090, "sm86_89",  "SM 86-89 (Ampere GA10x)"),
]

_MERCURY_DISPATCH_ENTRY_SIZE = 24
_MERCURY_DISPATCH_MAX_ENTRIES = 3000


def extract_per_sm_handler_dispatch(br: BinaryReader) -> dict:
    """Extract the 5 per-SM encoding handler dispatch tables.

    Each table maps (format_id << 8 | minor_opcode) to a .text handler
    function pointer.  Two entry formats coexist:
      Format A (dominant): {u64 handler, u64 pad=0, u64 opcode}  -- 24 bytes
      Format B (initial):  {u64 opcode, u64 handler, u64 pad=0}  -- 24 bytes
    The first few entries use format B; the bulk uses format A.
    We scan at 8-byte granularity to detect both formats reliably."""
    from collections import Counter

    table_region_bytes = _MERCURY_DISPATCH_MAX_ENTRIES * _MERCURY_DISPATCH_ENTRY_SIZE
    all_tables = []

    for table_start, table_id, description in _MERCURY_DISPATCH_TABLES:
        base_off = br._off(table_start)
        raw = br.data[base_off:base_off + table_region_bytes]

        seen_pairs = set()
        entries = []

        def _add(opc: int, hdl: int, byte_off: int, fmt: str):
            pair = (opc, hdl)
            if pair in seen_pairs:
                return
            seen_pairs.add(pair)
            fmt_id = (opc >> 8) & 0xFF
            minor = opc & 0xFF
            entries.append({
                "dispatch_opcode": int(opc),
                "dispatch_opcode_hex": f"0x{opc:04X}",
                "format_id": fmt_id,
                "minor_opcode": minor,
                "handler_va": f"0x{hdl:X}",
                "entry_format": fmt,
            })

        # Format A: {handler, 0, opcode} at 8-byte aligned positions
        for byte_off in range(0, len(raw) - 24, 8):
            hdl = struct.unpack_from('<Q', raw, byte_off)[0]
            pad = struct.unpack_from('<Q', raw, byte_off + 8)[0]
            opc = struct.unpack_from('<Q', raw, byte_off + 16)[0]
            if br.is_in_text(hdl) and pad == 0 and 0 < opc < 0x10000:
                _add(opc, hdl, byte_off, "A")

        # Format B: {opcode, handler, 0} -- typically only at the table start
        for byte_off in range(0, min(512, len(raw) - 16), 8):
            opc = struct.unpack_from('<Q', raw, byte_off)[0]
            hdl = struct.unpack_from('<Q', raw, byte_off + 8)[0]
            pad = struct.unpack_from('<Q', raw, byte_off + 16)[0] if byte_off + 16 < len(raw) else 1
            if 0 < opc < 0x10000 and br.is_in_text(hdl) and pad == 0:
                _add(opc, hdl, byte_off, "B")

        # Sort entries by dispatch_opcode for stable output
        entries.sort(key=lambda e: (e["dispatch_opcode"], e["handler_va"]))

        unique_opcodes = len(set(e["dispatch_opcode"] for e in entries))
        unique_handlers = len(set(e["handler_va"] for e in entries))

        handler_vas = [int(e["handler_va"], 16) for e in entries]
        opc_vals = [e["dispatch_opcode"] for e in entries]

        fmt_dist = Counter(e["format_id"] for e in entries)
        fmt_a = sum(1 for e in entries if e["entry_format"] == "A")
        fmt_b = sum(1 for e in entries if e["entry_format"] == "B")

        table_end = table_start + table_region_bytes

        all_tables.append({
            "table_id": table_id,
            "description": description,
            "start_va": f"0x{table_start:X}",
            "end_va": f"0x{table_end:X}",
            "region_size_bytes": table_region_bytes,
            "entry_size_bytes": _MERCURY_DISPATCH_ENTRY_SIZE,
            "total_entries": len(entries),
            "format_a_entries": fmt_a,
            "format_b_entries": fmt_b,
            "unique_dispatch_opcodes": unique_opcodes,
            "unique_handlers": unique_handlers,
            "opcode_range": {
                "min": f"0x{min(opc_vals):04X}" if opc_vals else None,
                "max": f"0x{max(opc_vals):04X}" if opc_vals else None,
            },
            "handler_va_range": {
                "min": f"0x{min(handler_vas):X}" if handler_vas else None,
                "max": f"0x{max(handler_vas):X}" if handler_vas else None,
            },
            "format_id_distribution": {
                str(fid): cnt for fid, cnt in sorted(fmt_dist.items())
            },
            "entries": entries,
        })

    # Cross-table analysis
    opcode_sets = {}
    for tbl in all_tables:
        opcode_sets[tbl["table_id"]] = set(e["dispatch_opcode"] for e in tbl["entries"])

    all_opcs = set.intersection(*opcode_sets.values()) if opcode_sets else set()
    sm_specific = {}
    for tid, opcs in opcode_sets.items():
        others = set.union(*(s for t, s in opcode_sets.items() if t != tid))
        unique_to_this = opcs - others
        if unique_to_this:
            sm_specific[tid] = sorted(unique_to_this)

    return {
        "per_sm_handler_dispatch": {
            "table_count": len(all_tables),
            "entry_format": "{u64 dispatch_opcode, u64 handler_ptr, u64 padding}",
            "opcode_encoding": "(format_id << 8) | minor_opcode",
            "common_opcodes_all_tables": len(all_opcs),
            "sm_specific_opcodes": {
                tid: [f"0x{o:04X}" for o in opcs]
                for tid, opcs in sm_specific.items()
            },
            "tables": all_tables,
        }
    }


# ─── Table 36: WGMMA/Intrinsic Infrastructure ────────────────────────────
#
# The gap at 0x229C480-0x22A1500 (20,608 bytes) sits between the occupancy
# constants and encoding constants.  It contains interconnected data structures
# supporting wgmma pipeline scheduling, PTX intrinsic lowering, operand-type
# polymorphism, and Mercury instruction modifier-to-opcode mapping.
#
# Sub-regions (verified via xrefs from .text):
#
#   A. 0x229C480-0x229C670: WGMMA pipeline configuration
#      - u32[12] pipeline parameters (depths, latencies)
#      - ptr64[22] handler vtable (assigned via mov [rbx], 0x229C4C0 at 0x66344B)
#      - u32[7] opcode indices + u32[11] enumeration config
#
#   B. 0x229C670-0x229CB20: ISel handler table
#      - ptr64[150] function pointers for instruction selection handlers
#      - Referenced from sub_9F3340 at 0x9F3D83
#
#   C. 0x229CB20-0x229D1C8: Embedded wgmma warning strings
#      - NUL-terminated ASCII diagnostic messages about wgmma pipeline
#        serialization (insufficient registers, divergent paths, etc.)
#
#   D. 0x229D1C8-0x229D418: Warning string pointers + wgmma config
#      - ptr64[] pointers into the warning strings above
#      - Small u32 config values (wgmma group sizes, error codes 0x3001-0x3004)
#
#   E. 0x229D418-0x229E2C0: PTX intrinsic handler table
#      - ptr64[469] function pointers for PTX intrinsic lowering
#      - Functions in 0x85a, 0x868, 0x7d7, 0xa8e address ranges
#      - Referenced from sub_ADF4xx and sub_AE04xx
#
#   F. 0x229E2C0-0x229E8C0: Operand type vtables
#      - 16 C++ vtables for polymorphic operand type classes
#      - Each vtable has 9-11 function pointer entries with NULL separators
#      - Assigned as *(_QWORD*)result = off_229Exxx in constructor functions
#
#   G. 0x229E8C0-0x229E9F8: Intrinsic type name table
#      - ptr64[39] all pointing to "???" at 0x21CDA50
#      - Used as char* by sub_6BDB60: off_229E8C0[type_index]
#
#   H. 0x229E9F8-0x22A0110: Intrinsic lowering switch jump tables
#      - ptr64[~736] indirect jump targets for switch dispatch
#      - Default target 0xAEA750 appears 269 times
#      - Referenced by sub_AEBE50, sub_AECB60, sub_AEA420 etc.
#
#   I. 0x22A0110-0x22A1500: Mercury opcode enum tables
#      - Mixed u32[] and u16[] lookup arrays
#      - Map Mercury instruction modifier enum values to SASS opcode IDs
#      - Used by 80+ sub_AF5xxx functions: return word_22Axxxx[modifier]
#      - u16 arrays contain sequential values 0x060A-0x0669 (register IDs)
#      - u32 arrays contain SASS opcode IDs in 652-2855 range

# Precise boundaries for each sub-region
_WGMMA_INFRA_GAP_START      = 0x229C480
_WGMMA_INFRA_GAP_END        = 0x22A1500

_WGMMA_PIPELINE_PARAMS_VA   = 0x229C480
_WGMMA_PIPELINE_PARAMS_N    = 12       # u32 entries
_WGMMA_HANDLER_VTABLE_VA    = 0x229C4C0
_WGMMA_HANDLER_VTABLE_N     = 22       # ptr64 entries
_WGMMA_OPCODE_INDICES_VA    = 0x229C5E0
_WGMMA_OPCODE_INDICES_N     = 7        # u32 entries
_WGMMA_ENUM_CONFIG_VA       = 0x229C600
_WGMMA_ENUM_CONFIG_N        = 18       # u32 entries (includes zeros)

_ISEL_HANDLER_TABLE_VA      = 0x229C670
_ISEL_HANDLER_TABLE_N       = 150      # ptr64 entries (150 .text ptrs, ends at 0x229CB20)

_WGMMA_WARNINGS_START       = 0x229CB20  # "Potential Performance Loss: wgmma..." starts here
_WGMMA_WARNINGS_END         = 0x229D1C8

_WGMMA_CONFIG_VA            = 0x229D1C8
_WGMMA_CONFIG_END           = 0x229D418

_INTRINSIC_HANDLER_TABLE_VA = 0x229D418
_INTRINSIC_HANDLER_TABLE_N  = 469      # ptr64 entries

_OPERAND_VTABLES_START      = 0x229E2C0
_OPERAND_VTABLES_END        = 0x229E8C0

_TYPE_NAME_TABLE_VA         = 0x229E8C0
_TYPE_NAME_TABLE_N          = 39       # ptr64 entries (all -> "???")

_SWITCH_JUMP_TABLE_VA       = 0x229E9F8
_SWITCH_JUMP_TABLE_END      = 0x22A0110

_MERCURY_ENUM_TABLES_START  = 0x22A0110
_MERCURY_ENUM_TABLES_END    = 0x22A1500


def _extract_wgmma_warning_strings(br: BinaryReader) -> list:
    """Extract NUL-terminated warning strings from the wgmma warning region."""
    start_off = br._off(_WGMMA_WARNINGS_START)
    end_off = br._off(_WGMMA_WARNINGS_END)
    raw = br.data[start_off:end_off]

    strings = []
    pos = 0
    while pos < len(raw):
        if raw[pos] == 0:
            pos += 1
            continue
        nul = raw.find(b'\x00', pos)
        if nul < 0:
            break
        try:
            s = raw[pos:nul].decode('ascii')
            va = _WGMMA_WARNINGS_START + pos
            strings.append({"va": f"0x{va:X}", "text": s, "length": len(s)})
        except UnicodeDecodeError:
            pass
        pos = nul + 1
    return strings


def _extract_operand_vtables(br: BinaryReader) -> list:
    """Extract C++ vtables for operand type classes.

    Each vtable is a run of .text pointers terminated by NULL entries.
    Returns a list of vtable descriptors with function pointer VAs."""
    start_off = br._off(_OPERAND_VTABLES_START)
    end_off = br._off(_OPERAND_VTABLES_END)

    vtables = []
    pos = start_off
    while pos < end_off:
        # Skip leading zeros
        while pos < end_off:
            val = struct.unpack_from('<Q', br.data, pos)[0]
            if val != 0:
                break
            pos += 8

        if pos >= end_off:
            break

        # Collect consecutive non-zero code pointers
        vt_start_va = pos + VA_BASE
        entries = []
        while pos < end_off:
            val = struct.unpack_from('<Q', br.data, pos)[0]
            if val == 0:
                break
            entries.append(f"0x{val:X}")
            pos += 8

        if entries:
            text_count = sum(1 for e in entries if br.is_in_text(int(e, 16)))
            vtables.append({
                "va": f"0x{vt_start_va:X}",
                "entry_count": len(entries),
                "text_pointers": text_count,
                "entries": entries,
            })

    return vtables


def _extract_mercury_enum_tables(br: BinaryReader) -> list:
    """Extract Mercury modifier-to-opcode lookup tables.

    These are u32[] and u16[] arrays referenced by sub_AF5xxx functions.
    Each function checks bounds, indexes an array, and returns an opcode ID.
    We scan for zero-separated runs of small values."""
    start_off = br._off(_MERCURY_ENUM_TABLES_START)
    end_off = br._off(_MERCURY_ENUM_TABLES_END)

    tables = []
    pos = start_off

    while pos < end_off:
        # Skip zeros
        while pos < end_off:
            val = struct.unpack_from('<H', br.data, pos)[0]
            if val != 0:
                break
            pos += 2

        if pos >= end_off:
            break

        # Detect element width: u16 pairs vs u32
        # u16 pairs have both halves in 0x0500-0x06FF range
        va = pos + VA_BASE
        val16a = struct.unpack_from('<H', br.data, pos)[0]
        val16b = struct.unpack_from('<H', br.data, pos + 2)[0]
        val32 = struct.unpack_from('<I', br.data, pos)[0]

        is_u16_array = (0x0500 <= val16a <= 0x06FF and 0x0500 <= val16b <= 0x06FF)

        if is_u16_array:
            # Scan u16 entries
            entries = []
            while pos < end_off:
                v = struct.unpack_from('<H', br.data, pos)[0]
                if v == 0:
                    break
                entries.append(v)
                pos += 2
            tables.append({
                "va": f"0x{va:X}",
                "element_type": "u16",
                "count": len(entries),
                "entries": entries,
                "value_range": [min(entries), max(entries)] if entries else [],
            })
        else:
            # Scan u32 entries
            entries = []
            while pos < end_off:
                v = struct.unpack_from('<I', br.data, pos)[0]
                if v == 0:
                    break
                entries.append(v)
                pos += 4
            if entries:
                tables.append({
                    "va": f"0x{va:X}",
                    "element_type": "u32",
                    "count": len(entries),
                    "entries": entries,
                    "value_range": [min(entries), max(entries)] if entries else [],
                })

    return tables


def extract_wgmma_intrinsic_infra(br: BinaryReader) -> dict:
    """Extract the wgmma/intrinsic infrastructure gap (0x229C480-0x22A1500).

    This 20,608-byte region between the occupancy constants and encoding
    constants contains interconnected data structures for:
    - WGMMA pipeline configuration and scheduling
    - Instruction selection handler dispatch
    - PTX intrinsic lowering (469 handler functions)
    - Operand type C++ vtables (16 vtables)
    - Mercury modifier-to-opcode enum lookup tables
    """

    # A. WGMMA pipeline parameters
    pipeline_params = br.u32_array(_WGMMA_PIPELINE_PARAMS_VA, _WGMMA_PIPELINE_PARAMS_N)
    pipeline_labels = [
        "pipeline_depth", "max_pending",
        "stage_latency_0", "stage_latency_1", "stage_latency_2",
        "stage_latency_3", "stage_latency_4", "stage_latency_5",
        "special_latency", "stage_latency_6", "stage_latency_7",
        "max_pipeline_length",
    ]

    # A. WGMMA handler vtable
    wgmma_vtable = br.ptr_array(_WGMMA_HANDLER_VTABLE_VA, _WGMMA_HANDLER_VTABLE_N)

    # A. Opcode indices
    opcode_indices = br.u32_array(_WGMMA_OPCODE_INDICES_VA, _WGMMA_OPCODE_INDICES_N)

    # A. Enum config
    enum_config = br.u32_array(_WGMMA_ENUM_CONFIG_VA, _WGMMA_ENUM_CONFIG_N)

    # B. ISel handler table
    isel_handlers = br.ptr_array(_ISEL_HANDLER_TABLE_VA, _ISEL_HANDLER_TABLE_N)
    isel_text_count = sum(1 for p in isel_handlers if br.is_in_text(p))

    # C. Warning strings
    warning_strings = _extract_wgmma_warning_strings(br)

    # D. Config block -- mix of pointers and small values
    config_size = (_WGMMA_CONFIG_END - _WGMMA_CONFIG_VA) // 8
    config_raw = []
    for i in range(config_size):
        va = _WGMMA_CONFIG_VA + i * 8
        val = br.u64(va)
        if br.is_in_text(val) or br.is_in_rodata(val):
            config_raw.append({"offset": i * 8, "type": "ptr", "value": f"0x{val:X}"})
        elif val == 0:
            config_raw.append({"offset": i * 8, "type": "zero", "value": 0})
        else:
            config_raw.append({"offset": i * 8, "type": "data", "value": int(val)})

    # E. PTX intrinsic handler table
    intrinsic_handlers = br.ptr_array(_INTRINSIC_HANDLER_TABLE_VA, _INTRINSIC_HANDLER_TABLE_N)
    intrinsic_text_count = sum(1 for p in intrinsic_handlers if br.is_in_text(p))

    # Group intrinsic handlers by address prefix to show function clusters
    from collections import Counter
    prefix_dist = Counter(f"0x{(p >> 12) << 12:X}" for p in intrinsic_handlers if p != 0)

    # F. Operand type vtables
    operand_vtables = _extract_operand_vtables(br)

    # G. Type name table
    type_names = []
    for i in range(_TYPE_NAME_TABLE_N):
        ptr = br.ptr(_TYPE_NAME_TABLE_VA + i * 8)
        try:
            name = br.cstring(ptr, max_len=32)
        except Exception:
            name = "<unreadable>"
        type_names.append({"index": i, "ptr_va": f"0x{ptr:X}", "name": name})

    # H. Switch jump tables
    jt_start_off = br._off(_SWITCH_JUMP_TABLE_VA)
    jt_end_off = br._off(_SWITCH_JUMP_TABLE_END)
    jt_entries = []
    for off in range(jt_start_off, jt_end_off, 8):
        val = struct.unpack_from('<Q', br.data, off)[0]
        jt_entries.append(val)

    jt_nonzero = [v for v in jt_entries if v != 0]
    jt_unique = len(set(jt_nonzero))
    jt_default = Counter(jt_nonzero).most_common(1)[0] if jt_nonzero else (0, 0)

    # I. Mercury opcode enum tables
    mercury_tables = _extract_mercury_enum_tables(br)

    total_size = _WGMMA_INFRA_GAP_END - _WGMMA_INFRA_GAP_START

    return {
        "wgmma_intrinsic_infra": {
            "region": {
                "start_va": f"0x{_WGMMA_INFRA_GAP_START:X}",
                "end_va": f"0x{_WGMMA_INFRA_GAP_END:X}",
                "size_bytes": total_size,
            },
            "wgmma_pipeline": {
                "params_va": f"0x{_WGMMA_PIPELINE_PARAMS_VA:X}",
                "params": {label: val for label, val in zip(pipeline_labels, pipeline_params)},
                "handler_vtable_va": f"0x{_WGMMA_HANDLER_VTABLE_VA:X}",
                "handler_vtable": [f"0x{p:X}" for p in wgmma_vtable],
                "handler_vtable_text_count": sum(1 for p in wgmma_vtable if br.is_in_text(p)),
                "opcode_indices": opcode_indices,
                "enum_config": enum_config,
            },
            "isel_handler_table": {
                "va": f"0x{_ISEL_HANDLER_TABLE_VA:X}",
                "count": _ISEL_HANDLER_TABLE_N,
                "text_pointer_count": isel_text_count,
                "entries": [f"0x{p:X}" for p in isel_handlers],
            },
            "wgmma_warning_strings": {
                "region": f"0x{_WGMMA_WARNINGS_START:X}-0x{_WGMMA_WARNINGS_END:X}",
                "count": len(warning_strings),
                "entries": warning_strings,
            },
            "wgmma_config_block": {
                "va": f"0x{_WGMMA_CONFIG_VA:X}",
                "entries": config_raw,
            },
            "intrinsic_handler_table": {
                "va": f"0x{_INTRINSIC_HANDLER_TABLE_VA:X}",
                "count": _INTRINSIC_HANDLER_TABLE_N,
                "text_pointer_count": intrinsic_text_count,
                "handler_prefix_distribution": {
                    k: v for k, v in prefix_dist.most_common(15)
                },
                "entries": [f"0x{p:X}" for p in intrinsic_handlers],
            },
            "operand_type_vtables": {
                "region": f"0x{_OPERAND_VTABLES_START:X}-0x{_OPERAND_VTABLES_END:X}",
                "count": len(operand_vtables),
                "vtables": operand_vtables,
            },
            "intrinsic_type_names": {
                "va": f"0x{_TYPE_NAME_TABLE_VA:X}",
                "count": _TYPE_NAME_TABLE_N,
                "note": "All entries point to '???' placeholder — types not yet named in binary",
                "entries": type_names,
            },
            "switch_jump_tables": {
                "region": f"0x{_SWITCH_JUMP_TABLE_VA:X}-0x{_SWITCH_JUMP_TABLE_END:X}",
                "total_entries": len(jt_entries),
                "nonzero_entries": len(jt_nonzero),
                "unique_targets": jt_unique,
                "default_target": f"0x{jt_default[0]:X}" if jt_default[0] else "none",
                "default_count": jt_default[1],
            },
            "mercury_opcode_enum_tables": {
                "region": f"0x{_MERCURY_ENUM_TABLES_START:X}-0x{_MERCURY_ENUM_TABLES_END:X}",
                "table_count": len(mercury_tables),
                "tables": mercury_tables,
            },
        }
    }


# ─── Table 37: Register Allocator Initialization Data ──────────────────
#
# VA 0x21EDE00-0x21EFE00 (8,192 bytes).  Sits between dispatch tables
# (ending at 0x21EDE00) and messages+dispatch (starting at 0x21EFE00).
#
# This block is the static initialization data for ptxas's register
# allocator object (~1024 bytes).  Constructor functions at 0xA3AF80
# and 0xA46CE0-0xA53B90 load from this region via MOVDQA/MOV.
#
# Layout (4 sub-regions):
#
#   A. 0x21EDE00-0x21EE41F: Function pointer vtable (196 x ptr64 = 1568 bytes)
#      - [0..132]   Main per-opcode register-class query dispatch (133 entries).
#                    122 are "rep ret" stubs; 11 are real handlers.
#      - [133..136]  4 NULLs (separator)
#      - [137..141]  SM-variant group 1 (5 entries): reduced method set
#      - [142..195]  SM-variant groups 2-7 (6 groups of [2-NULL + 7-ptr]):
#                    Each 7-entry group has layout:
#                      [0,1] init (same function, duplicated)
#                      [2]   scan  (shared: 0xA46CE0 across all groups)
#                      [3]   setup (shared: 0xA3A7E0 across all groups)
#                      [4]   rewrite (unique per group)
#                      [5]   verify  (unique per group)
#                      [6]   alloc   (unique per group)
#
#   B. 0x21EE420-0x21EE45F: Sentinel metadata (64 bytes)
#      - 3 x {lo_u32, hi_u32=0xFFFFFFFF} boundary markers
#      - 1 x 0xFFFFFFFFFFFFFFFF end-of-list
#      - u64 count=3 (number of register banks/classes)
#      - 3 x u64 zero padding
#
#   C. 0x21EE460-0x21EFAE0: Register ID arrays (5,760 bytes)
#      - 10 contiguous u32 arrays, each a sequential run of register IDs.
#      - Register IDs encoded as (bank << 16 | reg_number):
#          bank=1 (0x0001xxxx): general-purpose registers, step=1
#          bank=2 (0x0002xxxx): paired/wide registers, step=2
#      - Arrays separated by 8-32 byte zero padding.
#      - Concrete arrays:
#          240 IDs: bank=1, regs 640..879
#           34 IDs: bank=1, regs 576..609
#          --- 824 bytes zero padding ---
#          240 IDs: bank=1, regs 400..639
#           32 IDs: bank=1, regs 160..191
#          136 IDs: bank=2, regs 192..462 (step=2)
#          --- 32 bytes zero padding ---
#           16 IDs: bank=1, regs 464..479
#           48 IDs: bank=2, regs 480..574 (step=2)
#          240 IDs: bank=1, regs 160..399
#          168 IDs: bank=1, regs 640..807
#          --- 32 bytes zero padding ---
#           64 IDs: bank=1, regs 808..871
#
#   D. 0x21EFAE0-0x21EFE00: Terminator sentinel + debug strings (800 bytes)
#      - xmmword sentinel at 0x21EFAE0: {0, 0xFFFFFFFF, 0, 0}
#        Loaded via MOVDQA into allocator objects at 0xA3B352 et al.
#      - u32 0xFFFFFFFF end marker at 0x21EFAE4
#      - 10 NUL-terminated diagnostic strings for register allocation debugging:
#        "This def [%d] represents uninitialized value..."
#        "Please use -knob DUMPIR=AllocateRegisters for debugging"
#        "REMATERIALIZATION PROBLEM..."
#        etc.

_REGALLOC_INIT_VA        = 0x21EDE00
_REGALLOC_INIT_END       = 0x21EFE00
_REGALLOC_INIT_SIZE      = _REGALLOC_INIT_END - _REGALLOC_INIT_VA  # 8192

_REGALLOC_VTABLE_VA      = 0x21EDE00
_REGALLOC_VTABLE_COUNT   = 196   # ptr64 entries

_REGALLOC_SENTINEL_VA    = 0x21EE420  # 64 bytes of boundary metadata
_REGALLOC_REGID_VA       = 0x21EE460  # start of register ID arrays
_REGALLOC_REGID_END      = 0x21EFAE0  # end of register ID arrays
_REGALLOC_TERM_VA        = 0x21EFAE0  # xmmword sentinel + debug strings
_REGALLOC_STRINGS_VA     = 0x21EFAF0  # first debug string

# Number of main per-opcode dispatch entries (before SM-variant groups)
_REGALLOC_MAIN_DISPATCH  = 133
# SM-variant sub-groups: group 1 has 5 entries, groups 2-7 have 7 each
_REGALLOC_VARIANT_GROUPS = 7


def extract_regalloc_init(br: BinaryReader) -> dict:
    """Extract the register allocator initialization data block at 0x21EDE00.

    Contains the allocator vtable, sentinel metadata, register ID arrays,
    and diagnostic format strings used by the register allocation pass."""

    # ── A. Function pointer vtable ──
    vtable_ptrs = br.ptr_array(_REGALLOC_VTABLE_VA, _REGALLOC_VTABLE_COUNT)

    # Classify main dispatch entries
    stub_bytes = b'\xf3\xc3'  # rep ret
    main_entries = []
    for i in range(_REGALLOC_MAIN_DISPATCH):
        va = vtable_ptrs[i]
        is_text = br.is_in_text(va)
        is_stub = False
        if is_text:
            off = br._off(va)
            is_stub = br.data[off:off + 2] == stub_bytes
        main_entries.append({
            "index": i,
            "va": f"0x{va:X}" if is_text else "0x0",
            "is_stub": is_stub,
            "is_real_handler": is_text and not is_stub,
        })

    real_handler_indices = [e["index"] for e in main_entries if e["is_real_handler"]]
    stub_count = sum(1 for e in main_entries if e["is_stub"])

    # Parse SM-variant sub-groups
    variant_groups = []
    method_labels = ["init_a", "init_b", "scan", "setup", "rewrite", "verify", "alloc"]

    # Group 1: indices 137..141 (5 entries, reduced method set)
    g1_ptrs = vtable_ptrs[137:142]
    g1_labels = ["scan_or_init", "common_init", "variant_scan", "variant_verify", "stub_or_alloc"]
    g1_entries = []
    for j, va in enumerate(g1_ptrs):
        is_text = br.is_in_text(va)
        g1_entries.append({
            "slot": j,
            "label": g1_labels[j] if j < len(g1_labels) else f"slot_{j}",
            "va": f"0x{va:X}",
            "is_stub": br.data[br._off(va):br._off(va) + 2] == stub_bytes if is_text else False,
        })
    variant_groups.append({
        "group_index": 1,
        "vtable_indices": "137..141",
        "entry_count": len(g1_entries),
        "entries": g1_entries,
    })

    # Groups 2-7: indices [144+g*9 .. 144+g*9+6] with 2-NULL separators
    for g in range(6):
        base = 144 + g * 9
        grp_ptrs = vtable_ptrs[base:base + 7]
        entries = []
        for j, va in enumerate(grp_ptrs):
            entries.append({
                "slot": j,
                "label": method_labels[j],
                "va": f"0x{va:X}",
                "is_shared": (j == 2 and va == 0xA46CE0) or (j == 3 and va == 0xA3A7E0),
            })
        variant_groups.append({
            "group_index": g + 2,
            "vtable_indices": f"{base}..{base + 6}",
            "entry_count": len(entries),
            "entries": entries,
        })

    # ── B. Sentinel metadata ──
    sentinel_raw = []
    for i in range(8):
        va = _REGALLOC_SENTINEL_VA + i * 8
        val = br.u64(va)
        sentinel_raw.append({
            "offset": i,
            "value": f"0x{val:016X}",
            "hi32": f"0x{val >> 32:X}",
            "lo32": f"0x{val & 0xFFFFFFFF:X}",
        })

    # ── C. Register ID arrays ──
    reg_arrays = []
    regid_bytes = _REGALLOC_REGID_END - _REGALLOC_REGID_VA
    off = br._off(_REGALLOC_REGID_VA)
    u32s = list(struct.unpack_from(f'<{regid_bytes // 4}I', br.data, off))

    i = 0
    while i < len(u32s):
        if u32s[i] == 0:
            i += 1
            continue
        start = i
        bank = u32s[i] >> 16
        # Detect step (bank=2 uses step=2, bank=1 uses step=1)
        step = 1
        if start + 1 < len(u32s) and u32s[start + 1] != 0:
            step = u32s[start + 1] - u32s[start]
        # Collect contiguous sequential run with this step
        while i < len(u32s) and u32s[i] != 0 and (u32s[i] >> 16) == bank:
            if i > start and u32s[i] - u32s[i - 1] != step:
                break
            i += 1
        count = i - start
        first_reg = u32s[start] & 0xFFFF
        last_reg = u32s[i - 1] & 0xFFFF
        arr_va = _REGALLOC_REGID_VA + start * 4
        reg_arrays.append({
            "va": f"0x{arr_va:X}",
            "count": count,
            "bank": bank,
            "reg_lo": first_reg,
            "reg_hi": last_reg,
            "step": step,
            "size_bytes": count * 4,
        })

    total_reg_ids = sum(a["count"] for a in reg_arrays)

    # ── D. Terminator sentinel + debug strings ──
    term_xmm = br.xmm_dwords(_REGALLOC_TERM_VA)

    debug_strings = []
    str_start_off = br._off(_REGALLOC_STRINGS_VA)
    # Read 256 bytes past nominal end to capture the last string whose NUL
    # terminator falls at 0x21EFE45 (69 bytes beyond the 8 KB gap boundary).
    str_end_off = br._off(_REGALLOC_INIT_END) + 256
    str_end_off = min(str_end_off, len(br.data))
    raw = br.data[str_start_off:str_end_off]
    pos = 0
    while pos < len(raw):
        nul = raw.find(b'\x00', pos)
        if nul < 0:
            break
        if nul > pos:
            try:
                txt = raw[pos:nul].decode('ascii')
                if len(txt) > 2:
                    debug_strings.append({
                        "va": f"0x{_REGALLOC_STRINGS_VA + pos:X}",
                        "text": txt,
                    })
            except UnicodeDecodeError:
                pass
        pos = nul + 1
        # Stop once we hit non-string data past the nominal boundary
        if pos > (_REGALLOC_INIT_END - _REGALLOC_STRINGS_VA) + 128:
            break

    # Collect unique function VAs across entire vtable
    all_text_ptrs = sorted(set(
        p for p in vtable_ptrs if br.is_in_text(p)
    ))

    return {
        "regalloc_init": {
            "region": {
                "start_va": f"0x{_REGALLOC_INIT_VA:X}",
                "end_va": f"0x{_REGALLOC_INIT_END:X}",
                "size_bytes": _REGALLOC_INIT_SIZE,
            },
            "vtable": {
                "va": f"0x{_REGALLOC_VTABLE_VA:X}",
                "total_entries": _REGALLOC_VTABLE_COUNT,
                "main_dispatch": {
                    "count": _REGALLOC_MAIN_DISPATCH,
                    "stub_count": stub_count,
                    "real_handler_count": len(real_handler_indices),
                    "real_handler_indices": real_handler_indices,
                    "entries": main_entries,
                },
                "variant_groups": {
                    "count": len(variant_groups),
                    "note": "Per-SM register allocation strategy overrides",
                    "shared_scan_va": "0xA46CE0",
                    "shared_setup_va": "0xA3A7E0",
                    "groups": variant_groups,
                },
                "unique_function_vas": [f"0x{v:X}" for v in all_text_ptrs],
                "unique_function_count": len(all_text_ptrs),
            },
            "sentinel_metadata": {
                "va": f"0x{_REGALLOC_SENTINEL_VA:X}",
                "size_bytes": 64,
                "note": "Boundary markers for register bank classes; count=3",
                "entries": sentinel_raw,
            },
            "register_id_arrays": {
                "region": f"0x{_REGALLOC_REGID_VA:X}-0x{_REGALLOC_REGID_END:X}",
                "size_bytes": regid_bytes,
                "array_count": len(reg_arrays),
                "total_register_ids": total_reg_ids,
                "encoding_note": "Each u32 = (bank << 16 | reg_number); bank=1 is GP, bank=2 is paired/wide",
                "arrays": reg_arrays,
            },
            "terminator_and_strings": {
                "sentinel_va": f"0x{_REGALLOC_TERM_VA:X}",
                "sentinel_xmmword": [f"0x{v:X}" for v in term_xmm],
                "sentinel_note": "Loaded by MOVDQA into allocator objects as 'no valid register' marker",
                "debug_string_count": len(debug_strings),
                "debug_strings": debug_strings,
            },
        }
    }


# ─── Table 38: ISel Node Descriptor Tables ───────────────────────────────
#
# VA 0x22A5A58-0x22AD9D0 (~32,120 bytes).  Sits between the encoding
# constants and the ISel dispatch sub-tables.
#
# This region contains ISel (instruction selection) node descriptors:
# C++ vtables for pattern-matching ISel node types, operand sub-type
# vtables, and sentinel-delimited operand-field-offset blocks that
# map instruction operands to byte offsets within ISel node structs.
#
# Layout:
#
#   A. 0x22A5A58-0x22AAE00: ISel vtable pool (21,416 bytes, 13 sub-vtables)
#      - 4 x 455-entry primary handler tables (per-SM ISel dispatch)
#      - 4 x ~159-entry secondary handler tables
#      - 4 x 15-entry operand sub-type vtables (back-referenced by blocks)
#      - 1 x 129-entry tail vtable
#
#   B. 0x22AAE00-0x22ABE20: ISel descriptor object 1 (inline vtable form)
#      - 30-entry primary method vtable: opcode predicates, pattern matchers
#      - 15-entry sub-vtable A (operand type ID 0x5004)
#      - 15-entry sub-vtable B (operand type ID 0x5005)
#      - 2 self-referencing .rodata pointers to sub-vtables A and B
#      - 4 x 0x380-byte operand field offset blocks (per-SM variant)
#
#   C. 0x22ABE00-0x22AC030: ISel descriptor object 2 (remote vtable form)
#      - .rodata pointer to 15-entry sub-vtable at 0x22A6DE8
#      - 1 x 0x210-byte operand field offset block
#
#   D. 0x22AC030-0x22AC1A0: Trailing operand field offset data (368 bytes)
#
#   E. 0x22AC1A0-0x22AD230: ISel handler dispatch tables
#      - 146 + 316 .text pointers with mixed metadata gaps
#
#   F. 0x22AD230-0x22AD9D0: Final ISel dispatch table (244 ptrs + header)
#
# Operand block format (size varies: 0x210, 0x380):
#   +0x00: u32(0), u32(block_size)
#   +0x08: u32(2), u32(0), u32(3), u32(0), u32(-1), u32(-1)  -- metadata
#   then:  sentinel-separated groups of (u32(0), u32(byte_offset)) pairs
#   last:  .rodata backref pointer + zero padding
#
# The (0, offset) pairs are byte offsets into ISel node structs (stride 4).

_ISEL_DESC_VTABLE_POOL_VA    = 0x22A5A58
_ISEL_DESC_VTABLE_POOL_END   = 0x22AAE00

_ISEL_DESC_OBJ1_VA           = 0x22AAE00
_ISEL_DESC_OBJ1_HEADER_SIZE  = 0x220
_ISEL_DESC_OBJ1_BLOCK_COUNT  = 4

_ISEL_DESC_OBJ2_VA           = 0x22ABE00
_ISEL_DESC_OBJ2_HEADER_SIZE  = 0x20

_ISEL_DESC_TRAILING_VA       = 0x22AC030
_ISEL_DESC_TRAILING_END      = 0x22AC1A0

_ISEL_DESC_HANDLER_VA        = 0x22AC1A0
_ISEL_DESC_HANDLER_END       = 0x22AD230

_ISEL_DESC_FINAL_VA          = 0x22AD230
_ISEL_DESC_FINAL_END         = 0x22AD9D0


def _parse_operand_block(br: BinaryReader, block_va: int, block_size: int) -> dict:
    """Parse one sentinel-delimited operand-field-offset block.

    Returns the block header metadata, sentinel-separated groups of
    struct byte offsets, and any back-reference pointer at the end.
    """
    off = br._off(block_va)
    vals = list(struct.unpack_from(f'<{block_size // 4}I', br.data, off))

    # Extract small enum values from the header region (indices 2..~16)
    enum_vals = []
    for i in range(2, min(len(vals), 20)):
        v = vals[i]
        if 0 < v < 100:
            enum_vals.append(v)

    # Parse sentinel-separated groups of (0, offset) pairs
    groups = []
    cur = []
    backref_ptr = None

    for i in range(0, len(vals) - 1, 2):
        lo, hi = vals[i], vals[i + 1]
        if lo == 0xFFFFFFFF and hi == 0xFFFFFFFF:
            if cur:
                groups.append(cur)
                cur = []
        elif lo == 0 and hi == 0:
            if cur:
                groups.append(cur)
                cur = []
        elif lo == 0 and 0 < hi < 0x1000:
            cur.append(hi)
        elif lo > 0x400000 or hi > 0x400000:
            full = struct.unpack_from('<Q', br.data, off + i * 4)[0]
            if br.is_in_rodata(full):
                backref_ptr = full
                if cur:
                    groups.append(cur)
                    cur = []
    if cur:
        groups.append(cur)

    # Drop the size-echo group (single element equal to block_size)
    groups = [g for g in groups if g != [block_size]]

    annotated = []
    for g in groups:
        is_consec = len(g) > 1 and all(g[j + 1] - g[j] == 4 for j in range(len(g) - 1))
        annotated.append({
            "field_offsets": [f"0x{o:X}" for o in g],
            "count": len(g),
            "consecutive_stride4": is_consec,
            "range": f"0x{min(g):X}-0x{max(g):X}" if g else None,
        })

    return {
        "va": f"0x{block_va:X}",
        "size_bytes": block_size,
        "enum_metadata": enum_vals,
        "offset_group_count": len(annotated),
        "offset_groups": annotated,
        "backref_vtable_va": f"0x{backref_ptr:X}" if backref_ptr else None,
        "total_field_offsets": sum(len(g) for g in groups),
    }


def _extract_isel_vtable_pool(br: BinaryReader) -> list:
    """Extract sub-vtables from the ISel vtable pool region."""
    start_off = br._off(_ISEL_DESC_VTABLE_POOL_VA)
    end_off = br._off(_ISEL_DESC_VTABLE_POOL_END)

    vtables = []
    pos = start_off
    while pos < end_off:
        while pos < end_off:
            val = struct.unpack_from('<Q', br.data, pos)[0]
            if val != 0:
                break
            pos += 8
        if pos >= end_off:
            break

        vt_va = pos + VA_BASE
        entries = []
        while pos < end_off:
            val = struct.unpack_from('<Q', br.data, pos)[0]
            if not br.is_in_text(val):
                pos += 8  # skip non-.text value to avoid infinite loop
                break
            entries.append(val)
            pos += 8

        if entries:
            vtables.append({
                "va": f"0x{vt_va:X}",
                "entry_count": len(entries),
                "entries": [f"0x{p:X}" for p in entries],
            })

    return vtables


def _extract_text_ptr_tables(br: BinaryReader, start_va: int, end_va: int) -> list:
    """Extract runs of .text pointers from a dispatch region."""
    start_off = br._off(start_va)
    end_off = br._off(end_va)

    tables = []
    pos = start_off
    while pos < end_off:
        val = struct.unpack_from('<Q', br.data, pos)[0]
        if br.is_in_text(val):
            tbl_va = pos + VA_BASE
            entries = []
            while pos < end_off:
                v = struct.unpack_from('<Q', br.data, pos)[0]
                if not br.is_in_text(v):
                    break
                entries.append(v)
                pos += 8
            tables.append({
                "va": f"0x{tbl_va:X}",
                "entry_count": len(entries),
                "entries": [f"0x{p:X}" for p in entries],
            })
        else:
            pos += 8
    return tables


def extract_isel_node_descriptors(br: BinaryReader) -> dict:
    """Extract ISel node descriptor tables from 0x22A5A58-0x22AD9D0.

    Covers the vtable pool, inline/remote descriptor objects with operand
    field offset blocks, and the trailing ISel handler dispatch arrays.
    """

    # A. Vtable pool
    pool_vtables = _extract_isel_vtable_pool(br)
    pool_text_count = sum(vt["entry_count"] for vt in pool_vtables)

    # B. Object 1: inline vtable header (0x220 bytes)
    obj1_primary = br.ptr_array(_ISEL_DESC_OBJ1_VA, 30)
    obj1_sub_a = br.ptr_array(_ISEL_DESC_OBJ1_VA + 0x100, 15)
    obj1_sub_b = br.ptr_array(_ISEL_DESC_OBJ1_VA + 0x188, 15)
    obj1_self_refs = br.ptr_array(_ISEL_DESC_OBJ1_VA + 0x200, 2)

    def _type_id(func_va: int) -> int | None:
        off = br._off(func_va)
        if br.data[off] == 0xB8 and br.data[off + 5] == 0xC3:
            return struct.unpack_from('<I', br.data, off + 1)[0]
        return None

    sub_a_tid = _type_id(obj1_sub_a[0])
    sub_b_tid = _type_id(obj1_sub_b[0])

    obj1_blocks = []
    bva = _ISEL_DESC_OBJ1_VA + _ISEL_DESC_OBJ1_HEADER_SIZE
    for _ in range(_ISEL_DESC_OBJ1_BLOCK_COUNT):
        bsize = br.u32(bva + 4)
        obj1_blocks.append(_parse_operand_block(br, bva, bsize))
        bva += bsize

    # C. Object 2: remote vtable header (0x20 bytes)
    obj2_remote = br.ptr(_ISEL_DESC_OBJ2_VA)
    obj2_bva = _ISEL_DESC_OBJ2_VA + _ISEL_DESC_OBJ2_HEADER_SIZE
    obj2_bsize = br.u32(obj2_bva + 4)
    obj2_block = _parse_operand_block(br, obj2_bva, obj2_bsize)

    # D. Trailing operand data
    trail_off = br._off(_ISEL_DESC_TRAILING_VA)
    trail_end = br._off(_ISEL_DESC_TRAILING_END)
    trail_bytes = trail_end - trail_off
    trail_vals = list(struct.unpack_from(f'<{trail_bytes // 4}I', br.data, trail_off))
    trail_sents = sum(1 for v in trail_vals if v == 0xFFFFFFFF)
    trail_offsets = sum(
        1 for i in range(0, len(trail_vals) - 1, 2)
        if trail_vals[i] == 0 and 0 < trail_vals[i + 1] < 0x1000
    )

    # E. Handler dispatch tables (two runs of .text ptrs)
    handler_tables = _extract_text_ptr_tables(br, _ISEL_DESC_HANDLER_VA, _ISEL_DESC_HANDLER_END)

    # F. Final dispatch table
    final_tables = _extract_text_ptr_tables(br, _ISEL_DESC_FINAL_VA, _ISEL_DESC_FINAL_END)

    return {
        "isel_node_descriptors": {
            "region": {
                "pool_start_va": f"0x{_ISEL_DESC_VTABLE_POOL_VA:X}",
                "descriptors_end_va": f"0x{_ISEL_DESC_FINAL_END:X}",
                "total_size_bytes": _ISEL_DESC_FINAL_END - _ISEL_DESC_VTABLE_POOL_VA,
            },
            "vtable_pool": {
                "va": f"0x{_ISEL_DESC_VTABLE_POOL_VA:X}",
                "end_va": f"0x{_ISEL_DESC_VTABLE_POOL_END:X}",
                "size_bytes": _ISEL_DESC_VTABLE_POOL_END - _ISEL_DESC_VTABLE_POOL_VA,
                "sub_vtable_count": len(pool_vtables),
                "total_text_pointers": pool_text_count,
                "sub_vtables": pool_vtables,
            },
            "descriptor_object_1": {
                "va": f"0x{_ISEL_DESC_OBJ1_VA:X}",
                "end_va": f"0x{bva:X}",
                "form": "inline_vtable",
                "primary_vtable": {
                    "entry_count": 30,
                    "entries": [f"0x{p:X}" for p in obj1_primary],
                },
                "sub_vtable_a": {
                    "va": f"0x{_ISEL_DESC_OBJ1_VA + 0x100:X}",
                    "type_id": f"0x{sub_a_tid:X}" if sub_a_tid else None,
                    "entry_count": 15,
                    "entries": [f"0x{p:X}" for p in obj1_sub_a],
                },
                "sub_vtable_b": {
                    "va": f"0x{_ISEL_DESC_OBJ1_VA + 0x188:X}",
                    "type_id": f"0x{sub_b_tid:X}" if sub_b_tid else None,
                    "entry_count": 15,
                    "entries": [f"0x{p:X}" for p in obj1_sub_b],
                },
                "self_ref_ptrs": [f"0x{r:X}" for r in obj1_self_refs],
                "operand_block_count": len(obj1_blocks),
                "operand_blocks": obj1_blocks,
            },
            "descriptor_object_2": {
                "va": f"0x{_ISEL_DESC_OBJ2_VA:X}",
                "end_va": f"0x{obj2_bva + obj2_bsize:X}",
                "form": "remote_vtable",
                "remote_vtable_va": f"0x{obj2_remote:X}",
                "operand_block": obj2_block,
            },
            "trailing_operand_data": {
                "va": f"0x{_ISEL_DESC_TRAILING_VA:X}",
                "end_va": f"0x{_ISEL_DESC_TRAILING_END:X}",
                "size_bytes": trail_bytes,
                "sentinel_count": trail_sents,
                "field_offset_pairs": trail_offsets,
            },
            "handler_dispatch_tables": {
                "va": f"0x{_ISEL_DESC_HANDLER_VA:X}",
                "end_va": f"0x{_ISEL_DESC_HANDLER_END:X}",
                "table_count": len(handler_tables),
                "tables": handler_tables,
            },
            "final_dispatch_table": {
                "va": f"0x{_ISEL_DESC_FINAL_VA:X}",
                "end_va": f"0x{_ISEL_DESC_FINAL_END:X}",
                "table_count": len(final_tables),
                "tables": final_tables,
            },
        }
    }


# ─── Table 39: Scheduling Encoder Dispatch Tables ────────────────────

# The 7,680-byte gap at 0x21D9200-0x21DB000 sits between the shared memory
# sm_75 table (ending ~0x21D9168) and the DAG knob strings (starting 0x21DB000).
# It contains the core scheduling encoder's dispatch infrastructure:
#
# The mega-function at 0x89FBA0 implements per-opcode control word encoding
# for instruction scheduling.  It uses:
#
# 1. A function pointer table for initialization methods (5 code ptrs)
# 2. SSE bitmask constants and a double 1000000.0 (clock() conversion)
# 3. A 14-entry resource class dispatch vtable (maps resource class -> handler)
# 4. A 773-element identity permutation (uint32 array: 0..772)
# 5. A 330-entry opcode dispatch jump table (main switch on instruction opcode)
# 6. Two sub-opcode jump tables (6 and 7 entries) for secondary dispatch
# 7. Scheduling metadata (9 qwords of small integer parameters)
# 8. Two polymorphic C++ vtables (126 and 65 code ptrs) installed at [object+0]
#    by mov [rbx], 0x21DA9F8 and mov [rbx], 0x21DADF8

_SCHED_ENC_GAP_START      = 0x21D9200
_SCHED_ENC_GAP_END        = 0x21DB000

_SCHED_INIT_FPTR_VA       = 0x21D9200   # 8 qwords (5 code ptrs + 3 NULL)
_SCHED_INIT_FPTR_N        = 8

_SCHED_SSE_CONST_VA       = 0x21D9240   # 16-byte SSE mask + 8-byte double
_SCHED_DOUBLE_VA          = 0x21D9250   # double 1000000.0

_SCHED_RESOURCE_VTABLE_VA = 0x21D9258   # 14 code ptrs
_SCHED_RESOURCE_VTABLE_N  = 14

_SCHED_IDENTITY_VA        = 0x21D92E0   # 773 uint32 elements: identity[i] = i
_SCHED_IDENTITY_N         = 773

_SCHED_OPCODE_JT_VA       = 0x21D9EF8   # 330 qword code ptrs (jmp [rax*8+0x21D9EF8])
_SCHED_OPCODE_JT_N        = 330
_SCHED_OPCODE_JT_DEFAULT  = 0x8A2119    # generic handler: call a2d340

_SCHED_SUBOP_JT_A_VA      = 0x21DA948   # 6 qword code ptrs (jmp [rax*8+0x21DA948])
_SCHED_SUBOP_JT_A_N       = 6

_SCHED_SUBOP_JT_B_VA      = 0x21DA978   # 7 qword code ptrs (jmp [rax*8+0x21DA978])
_SCHED_SUBOP_JT_B_N       = 7

_SCHED_METADATA_VA        = 0x21DA9B0   # 9 qwords of small-int parameters
_SCHED_METADATA_N         = 9

_SCHED_POLY_VTABLE_A_VA   = 0x21DA9F8   # 126 code ptrs (installed by mov [rbx], imm)
_SCHED_POLY_VTABLE_A_N    = 126

_SCHED_POLY_VTABLE_B_VA   = 0x21DADF8   # 65 code ptrs (variant, differs only in [0],[1])
_SCHED_POLY_VTABLE_B_N    = 65


def extract_sched_encoder_dispatch(br: BinaryReader) -> dict:
    """Extract scheduling encoder dispatch tables from the 0x21D9200 gap.

    This region is the dispatch backbone of the mega-function at 0x89FBA0
    which encodes scheduling control words into SASS instructions.  It
    contains jump tables for opcode dispatch, resource class vtables,
    polymorphic C++ vtables, and numeric constants."""

    from collections import Counter

    # 1. Initialization function pointers
    init_fptrs = br.ptr_array(_SCHED_INIT_FPTR_VA, _SCHED_INIT_FPTR_N)
    init_entries = []
    for i, p in enumerate(init_fptrs):
        entry = {"index": i, "va": f"0x{p:X}" if p else "NULL"}
        if p and br.is_in_text(p):
            entry["valid"] = True
        elif p == 0:
            entry["valid"] = True
        else:
            entry["valid"] = False
            print(f"    WARNING: sched_init_fptr[{i}] = 0x{p:X} not in .text",
                  file=sys.stderr)
        init_entries.append(entry)

    # 2. SSE constant and double
    sse_lo, sse_hi = br.xmm(_SCHED_SSE_CONST_VA)
    sse_dwords = list(br.xmm_dwords(_SCHED_SSE_CONST_VA))
    timing_double = struct.unpack_from('<d', br.data, br._off(_SCHED_DOUBLE_VA))[0]

    # 3. Resource class dispatch vtable
    resource_ptrs = br.ptr_array(_SCHED_RESOURCE_VTABLE_VA, _SCHED_RESOURCE_VTABLE_N)
    resource_invalid = [(i, p) for i, p in enumerate(resource_ptrs) if not br.is_in_text(p)]
    if resource_invalid:
        for idx, p in resource_invalid[:5]:
            print(f"    WARNING: resource_vtable[{idx}] = 0x{p:X} not in .text",
                  file=sys.stderr)
    resource_unique = sorted(set(resource_ptrs))

    # 4. Identity permutation -- validate it is actually [0..N-1]
    identity = br.u32_array(_SCHED_IDENTITY_VA, _SCHED_IDENTITY_N)
    identity_ok = all(identity[i] == i for i in range(_SCHED_IDENTITY_N))
    if not identity_ok:
        bad = [(i, identity[i]) for i in range(_SCHED_IDENTITY_N) if identity[i] != i]
        print(f"    WARNING: identity table has {len(bad)} non-identity entries",
              file=sys.stderr)

    # 5. Main opcode dispatch jump table
    opcode_jt = br.ptr_array(_SCHED_OPCODE_JT_VA, _SCHED_OPCODE_JT_N)
    opcode_invalid = sum(1 for p in opcode_jt if not br.is_in_text(p))
    opcode_counter = Counter(opcode_jt)
    opcode_default_count = opcode_counter.get(_SCHED_OPCODE_JT_DEFAULT, 0)
    opcode_unique = sorted(set(opcode_jt))

    opcode_entries = []
    for i, p in enumerate(opcode_jt):
        entry = {
            "index": i,
            "index_hex": f"0x{i:03X}",
            "handler_va": f"0x{p:X}",
            "is_default": p == _SCHED_OPCODE_JT_DEFAULT,
        }
        opcode_entries.append(entry)

    # 6. Sub-opcode jump tables
    subop_a = br.ptr_array(_SCHED_SUBOP_JT_A_VA, _SCHED_SUBOP_JT_A_N)
    subop_b = br.ptr_array(_SCHED_SUBOP_JT_B_VA, _SCHED_SUBOP_JT_B_N)

    # 7. Scheduling metadata
    metadata_raw = []
    for i in range(_SCHED_METADATA_N):
        va = _SCHED_METADATA_VA + i * 8
        val = br.u64(va)
        lo, hi = br.u32(va), br.u32(va + 4)
        metadata_raw.append({"index": i, "qword": int(val), "lo_u32": lo, "hi_u32": hi})

    # 8. Polymorphic vtables
    poly_a = br.ptr_array(_SCHED_POLY_VTABLE_A_VA, _SCHED_POLY_VTABLE_A_N)
    poly_b = br.ptr_array(_SCHED_POLY_VTABLE_B_VA, _SCHED_POLY_VTABLE_B_N)

    poly_a_invalid = sum(1 for p in poly_a if not br.is_in_text(p))
    poly_b_invalid = sum(1 for p in poly_b if not br.is_in_text(p))

    # Compare A vs B: they differ only in [0] and [1]
    poly_diffs = []
    for i in range(min(len(poly_a), len(poly_b))):
        if poly_a[i] != poly_b[i]:
            poly_diffs.append({
                "index": i,
                "vtable_A": f"0x{poly_a[i]:X}",
                "vtable_B": f"0x{poly_b[i]:X}",
            })

    total_size = _SCHED_ENC_GAP_END - _SCHED_ENC_GAP_START

    return {
        "sched_encoder_dispatch": {
            "region": {
                "start_va": f"0x{_SCHED_ENC_GAP_START:X}",
                "end_va": f"0x{_SCHED_ENC_GAP_END:X}",
                "size_bytes": total_size,
                "description": "Scheduling encoder dispatch infrastructure for sub_89FBA0",
            },
            "init_function_ptrs": {
                "va": f"0x{_SCHED_INIT_FPTR_VA:X}",
                "count": _SCHED_INIT_FPTR_N,
                "entries": init_entries,
            },
            "sse_constants": {
                "va": f"0x{_SCHED_SSE_CONST_VA:X}",
                "xmm_lo": f"0x{sse_lo:016X}",
                "xmm_hi": f"0x{sse_hi:016X}",
                "xmm_dwords": [f"0x{d:08X}" for d in sse_dwords],
                "note": "Loaded by movdqa at 0x891060 and 0x986F85",
            },
            "timing_constant": {
                "va": f"0x{_SCHED_DOUBLE_VA:X}",
                "value": timing_double,
                "note": "double 1000000.0 -- microseconds/second for clock() conversion",
                "xref": "mov rax, [rip+...] at 0x894362, stored to [obj+0x3f0]",
            },
            "resource_class_dispatch": {
                "va": f"0x{_SCHED_RESOURCE_VTABLE_VA:X}",
                "count": _SCHED_RESOURCE_VTABLE_N,
                "unique_functions": len(resource_unique),
                "unique_function_vas": [f"0x{v:X}" for v in resource_unique],
                "invalid_pointers": len(resource_invalid),
                "note": "Maps resource class index to handler; targets compare edx vs 0xB7",
                "entries": [
                    {"index": i, "va": f"0x{resource_ptrs[i]:X}"}
                    for i in range(_SCHED_RESOURCE_VTABLE_N)
                ],
            },
            "identity_permutation": {
                "va": f"0x{_SCHED_IDENTITY_VA:X}",
                "count": _SCHED_IDENTITY_N,
                "element_type": "uint32",
                "size_bytes": _SCHED_IDENTITY_N * 4,
                "is_valid_identity": identity_ok,
                "note": "identity[i] = i for i in 0..772; used as default index mapping",
            },
            "opcode_dispatch_jmptable": {
                "va": f"0x{_SCHED_OPCODE_JT_VA:X}",
                "end_va": f"0x{_SCHED_OPCODE_JT_VA + _SCHED_OPCODE_JT_N * 8:X}",
                "count": _SCHED_OPCODE_JT_N,
                "dispatch_site": "jmp [rax*8 + 0x21D9EF8] at 0x89FC3E",
                "guard": "cmp r15d, 0x149; ja 0x8A2119 (default handler)",
                "default_handler": f"0x{_SCHED_OPCODE_JT_DEFAULT:X}",
                "default_count": opcode_default_count,
                "specialized_count": _SCHED_OPCODE_JT_N - opcode_default_count,
                "unique_targets": len(opcode_unique),
                "invalid_pointers": opcode_invalid,
                "entries": opcode_entries,
            },
            "subop_jmptable_A": {
                "va": f"0x{_SCHED_SUBOP_JT_A_VA:X}",
                "count": _SCHED_SUBOP_JT_A_N,
                "dispatch_site": "jmp [rax*8 + 0x21DA948] at 0x8A0731",
                "guard": "cmp eax, 5; extracts 3-bit field via sar+and",
                "entries": [
                    {"index": i, "va": f"0x{subop_a[i]:X}"}
                    for i in range(_SCHED_SUBOP_JT_A_N)
                ],
            },
            "subop_jmptable_B": {
                "va": f"0x{_SCHED_SUBOP_JT_B_VA:X}",
                "count": _SCHED_SUBOP_JT_B_N,
                "dispatch_site": "jmp [rax*8 + 0x21DA978] at 0x8A0BD8",
                "guard": "cmp eax, 6; extracts 4-bit field via and 0xF",
                "entries": [
                    {"index": i, "va": f"0x{subop_b[i]:X}"}
                    for i in range(_SCHED_SUBOP_JT_B_N)
                ],
            },
            "scheduling_metadata": {
                "va": f"0x{_SCHED_METADATA_VA:X}",
                "count": _SCHED_METADATA_N,
                "entries": metadata_raw,
            },
            "polymorphic_vtable_A": {
                "va": f"0x{_SCHED_POLY_VTABLE_A_VA:X}",
                "end_va": f"0x{_SCHED_POLY_VTABLE_A_VA + _SCHED_POLY_VTABLE_A_N * 8:X}",
                "count": _SCHED_POLY_VTABLE_A_N,
                "invalid_pointers": poly_a_invalid,
                "xref": "mov [rbx], 0x21DA9F8 at 0x89BA32",
                "note": "Installed as C++ vtable at object+0; 126 virtual method ptrs",
                "entries": [f"0x{p:X}" for p in poly_a],
            },
            "polymorphic_vtable_B": {
                "va": f"0x{_SCHED_POLY_VTABLE_B_VA:X}",
                "end_va": f"0x{_SCHED_POLY_VTABLE_B_VA + _SCHED_POLY_VTABLE_B_N * 8:X}",
                "count": _SCHED_POLY_VTABLE_B_N,
                "invalid_pointers": poly_b_invalid,
                "xrefs": [
                    "mov [rbx], 0x21DADF8 at 0x7CB579",
                    "mov [rdi], 0x21DADF8 at 0x895E22",
                    "mov [rdi], 0x21DADF8 at 0x895F92",
                    "mov [rdi], 0x21DADF8 at 0x896102",
                ],
                "note": "Derived-class vtable variant; differs from A only in first 2 entries",
                "entries": [f"0x{p:X}" for p in poly_b],
            },
            "vtable_A_vs_B_diff": {
                "shared_entries": min(len(poly_a), len(poly_b)) - len(poly_diffs),
                "differing_entries": len(poly_diffs),
                "diffs": poly_diffs,
            },
        }
    }


# ─── Derived: Opcode Master Record ─────────────────────────────────────

def build_opcode_master(names: dict, cat_map: dict, enc_table: dict) -> dict:
    """Cross-reference opcode names, encoding categories, and ISel encoding slots."""
    name_entries = names.get("opcode_names", {}).get("entries", [])
    cat_entries = cat_map.get("encoding_category_map", {}).get("entries", [])
    enc_entries = enc_table.get("opcode_to_encoding", {}).get("entries", [])
    cat_entries = cat_map.get("encoding_category_map", {}).get("entries", [])
    enc_entries = enc_table.get("opcode_to_encoding", {}).get("entries", [])

    # Build encoding slot lookup
    enc_lookup = {}
    for e in enc_entries:
        enc_lookup[e["opcode"]] = e["encoding_slot"]

    # Determine SM generation per opcode using a proper interval check.
    # Boundaries define half-open intervals: [0, SM70_LAST] = sm_70,
    # [SM73_FIRST, SM73_LAST] = sm_73, etc.
    SM_RANGES = [
        (0,   136, "sm_70"),
        (137, 171, "sm_73"),
        (172, 193, "sm_82"),
        (194, 199, "sm_86"),
        (200, 205, "sm_89"),
        (206, 252, "sm_90"),
        (253, 280, "sm_100"),
        (281, 320, "sm_104"),
        (321, 321, "sentinel"),
    ]

    def sm_gen(idx: int) -> str:
        for lo, hi, gen in SM_RANGES:
            if lo <= idx <= hi:
                return gen
        return "unknown"

    records = []
    for i in range(min(OPCODE_NAME_COUNT, len(name_entries))):
        entry = name_entries[i] if i < len(name_entries) else {}
        records.append({
            "index": i,
            "mnemonic": entry.get("mnemonic", f"OPCODE_{i}"),
            "rot13": entry.get("rot13", ""),
            "sm_gen": sm_gen(i),
            "encoding_category": cat_entries[i] if i < len(cat_entries) else None,
            "isel_encoding_slot": enc_lookup.get(i),
        })

    return {"opcode_master": {"count": len(records), "entries": records}}


# ─── Derived: Encoding Geometry ─────────────────────────────────────────

def build_encoding_geometry(fmt_desc: dict, tier2: dict) -> dict:
    """Combine format descriptors with tier-2 modifiers."""
    formats = fmt_desc.get("format_descriptors", [])
    groups = tier2.get("tier2_modifier_tables", {}).get("groups", [])

    # Build modifier lookup by SM range
    modifier_lookup = {}
    for g in groups:
        modifier_lookup[g["label"]] = {
            "sm_range": g["sm_range"],
            "entries": g["entries"],
        }

    combined = []
    for f in formats:
        combined.append({
            "format_label": f["label"],
            "va": f["va"],
            "width_bits": f["instruction_width"],
            "xmmword": {"lo": f["xmmword_lo"], "hi": f["xmmword_hi"]},
            "slot_layout": {
                "sizes": f["slot_sizes"],
                "types": f["slot_types"],
                "flags": f["slot_flags"],
                "active_count": f["active_slots"],
            },
            "encoder_count": f["wiki_encoder_count"],
            "tier2_modifiers": modifier_lookup,
        })

    return {"encoding_geometry": {"format_count": len(combined), "formats": combined}}



# ─── Table 38: ISel Operand Constraints + Instruction VTable ────────────
#
# Gap 0x22B8E00-0x22BD0C0 (17,088 bytes): ISel secondary -> phase name table.
#   A. 0x22B8E00-0x22BB560: 39 operand constraint records (0x100 stride)
#   B. 0x22BB560-0x22BB6D8: 47-entry operand handler vtable
#   C. 0x22BB6D8-0x22BB738: Operand count mapping (96 bytes)
#   D. 0x22BB738-0x22BC3B0: 399-entry instruction operation vtable
#   E. 0x22BC3B0-0x22BD0C0: Phase name string pool (3,344 bytes)

_OPERAND_CONSTRAINT_RECORDS_VA     = 0x22B8E00
_OPERAND_CONSTRAINT_RECORDS_END    = 0x22BB560
_OPERAND_CONSTRAINT_RECORD_STRIDE  = 0x100
_OPERAND_CONSTRAINT_RECORD_COUNT   = 39
_OPERAND_HANDLER_VTABLE_VA         = 0x22BB560
_OPERAND_HANDLER_VTABLE_COUNT      = 47
_OPERAND_COUNT_MAPPING_VA          = 0x22BB6D8
_OPERAND_COUNT_MAPPING_END         = 0x22BB738
_OPERAND_COUNT_MAPPING_SIZE        = _OPERAND_COUNT_MAPPING_END - _OPERAND_COUNT_MAPPING_VA
_INSTR_OPERATION_VTABLE_VA         = 0x22BB738
_INSTR_OPERATION_VTABLE_COUNT      = 399
_PHASE_NAME_STRPOOL_VA             = 0x22BC3B0
_PHASE_NAME_STRPOOL_END            = 0x22BD0C0

_OPERAND_CONSTRAINT_DISPATCH = {
    "JMX": 70, "UTMAPF": 243, "UTMALST": 245, "VHMNMX": 246,
    "VIADD": 247, "CREDUX": 254, "FMNMX3": 257,
    "UTCBAR_1CTA": 261, "UTCBAR_2CTA": 262,
}


def extract_isel_operand_constraints(br: BinaryReader) -> dict:
    """Extract ISel operand constraint records, handler vtables, instruction
    operation vtable, and phase name string pool."""

    records = []
    for i in range(_OPERAND_CONSTRAINT_RECORD_COUNT):
        rec_va = _OPERAND_CONSTRAINT_RECORDS_VA + i * _OPERAND_CONSTRAINT_RECORD_STRIDE
        header = br.u32_array(rec_va, 4)
        tc = br.u32(rec_va + 0x60)
        type_ids = br.u32_array(rec_va + 0x68, tc) if 0 < tc < 30 else []
        flags = br.u32_array(rec_va + 0xDC, 5)
        records.append({"index": i, "va": f"0x{rec_va:X}", "header": header,
                        "type_count": tc, "type_ids": type_ids, "constraint_flags": flags})

    all_type_ids = set()
    for r in records:
        all_type_ids.update(r["type_ids"])

    handler_ptrs = br.ptr_array(_OPERAND_HANDLER_VTABLE_VA, _OPERAND_HANDLER_VTABLE_COUNT)
    mapping_u32s = br.u32_array(_OPERAND_COUNT_MAPPING_VA, _OPERAND_COUNT_MAPPING_SIZE // 4)
    instr_ptrs = br.ptr_array(_INSTR_OPERATION_VTABLE_VA, _INSTR_OPERATION_VTABLE_COUNT)

    pool_data = br.read_bytes(_PHASE_NAME_STRPOOL_VA,
                              _PHASE_NAME_STRPOOL_END - _PHASE_NAME_STRPOOL_VA)
    pool_strings, pos = [], 0
    while pos < len(pool_data):
        if pool_data[pos] == 0:
            pos += 1
            continue
        nul = pool_data.find(b'\x00', pos)
        if nul < 0:
            break
        try:
            s = pool_data[pos:nul].decode('ascii')
        except UnicodeDecodeError:
            pos = nul + 1
            continue
        pool_strings.append({"va": f"0x{_PHASE_NAME_STRPOOL_VA + pos:X}", "string": s})
        pos = nul + 1

    phase_ptrs = set(br.ptr_array(PHASE_NAME_TABLE_VA, PHASE_NAME_COUNT))
    for entry in pool_strings:
        entry["is_phase_name"] = int(entry["va"], 16) in phase_ptrs
    phase_n = sum(1 for e in pool_strings if e["is_phase_name"])

    return {"isel_operand_constraints": {
        "region": {"start_va": f"0x{_OPERAND_CONSTRAINT_RECORDS_VA:X}",
                   "end_va": f"0x{_PHASE_NAME_STRPOOL_END:X}",
                   "total_bytes": _PHASE_NAME_STRPOOL_END - _OPERAND_CONSTRAINT_RECORDS_VA},
        "operand_constraint_records": {
            "source_va": f"0x{_OPERAND_CONSTRAINT_RECORDS_VA:X}",
            "end_va": f"0x{_OPERAND_CONSTRAINT_RECORDS_END:X}",
            "record_count": _OPERAND_CONSTRAINT_RECORD_COUNT,
            "record_stride": _OPERAND_CONSTRAINT_RECORD_STRIDE,
            "nonempty_records": sum(1 for r in records if r["type_count"] > 0),
            "unique_type_ids": sorted(all_type_ids),
            "dispatch_opcodes": _OPERAND_CONSTRAINT_DISPATCH,
            "accessor_function": "sub_C3F490",
            "callers": ["sub_C40420", "sub_C40B90", "sub_C41100",
                        "sub_C42330", "sub_6CB8A0"],
            "records": records},
        "operand_handler_vtable": {
            "source_va": f"0x{_OPERAND_HANDLER_VTABLE_VA:X}",
            "entry_count": _OPERAND_HANDLER_VTABLE_COUNT,
            "valid_text_pointers": sum(1 for p in handler_ptrs if br.is_in_text(p)),
            "unique_targets": len(set(handler_ptrs)),
            "accessors": ["sub_C47430", "sub_C3F730", "sub_C3F970"],
            "entries": [f"0x{p:X}" for p in handler_ptrs]},
        "operand_count_mapping": {
            "source_va": f"0x{_OPERAND_COUNT_MAPPING_VA:X}",
            "end_va": f"0x{_OPERAND_COUNT_MAPPING_END:X}",
            "size_bytes": _OPERAND_COUNT_MAPPING_SIZE,
            "accessors": ["sub_C47D50", "sub_C48510", "sub_C49450",
                          "sub_C49700", "sub_C499D0"],
            "values": mapping_u32s},
        "instruction_operation_vtable": {
            "source_va": f"0x{_INSTR_OPERATION_VTABLE_VA:X}",
            "entry_count": _INSTR_OPERATION_VTABLE_COUNT,
            "valid_text_pointers": sum(1 for p in instr_ptrs if br.is_in_text(p)),
            "null_entries": sum(1 for p in instr_ptrs if p == 0),
            "unique_targets": len(set(instr_ptrs)),
            "constructor": "sub_9CE030",
            "note": "C++ vtable assigned via *obj = off_22BB738",
            "entries": [f"0x{p:X}" for p in instr_ptrs]},
        "phase_name_string_pool": {
            "source_va": f"0x{_PHASE_NAME_STRPOOL_VA:X}",
            "end_va": f"0x{_PHASE_NAME_STRPOOL_END:X}",
            "size_bytes": _PHASE_NAME_STRPOOL_END - _PHASE_NAME_STRPOOL_VA,
            "total_strings": len(pool_strings),
            "phase_name_strings": phase_n,
            "format_strings": len(pool_strings) - phase_n,
            "note": "String storage for phase name pointer table at 0x22BD0C0",
            "strings": pool_strings},
    }}


# ─── Main ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract .rodata tables from ptxas v13.0.88 for SMT-based SASS code generation")
    parser.add_argument("--binary", default=str(Path(__file__).parent.parent / "ptxas"),
                        help="Path to ptxas binary (default: ../ptxas)")
    parser.add_argument("--output", default=str(Path(__file__).parent.parent / "extracted"),
                        help="Output directory (default: ../extracted/)")
    args = parser.parse_args()

    binary_path = Path(args.binary)
    output_dir = Path(args.output)

    if not binary_path.exists():
        print(f"ERROR: Binary not found: {binary_path}", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading {binary_path} ({binary_path.stat().st_size / 1e6:.1f} MB)...")
    br = BinaryReader(str(binary_path))

    # Sanity check: verify known string at known address
    try:
        test_str = br.cstring(0x21C1DD7, max_len=10)
        test_decoded = rot13(test_str)
        print(f"  Sanity check: VA 0x21C1DD7 = '{test_str}' -> '{test_decoded}'")
        if test_decoded != "ERRBAR":
            print(f"  WARNING: Expected 'ERRBAR', got '{test_decoded}' — VA mapping may be wrong")
    except Exception as e:
        print(f"  WARNING: Sanity check failed: {e}")

    binary_hash = br.sha256()
    print(f"  SHA-256: {binary_hash[:16]}...")

    # Extract all tables
    tables = {}
    extractors = [
        ("opcode_names",       "opcode_names.json",       extract_opcode_names),
        ("encoding_cat_map",   "encoding_category_map.json", extract_encoding_category_map),
        ("format_descriptors", "format_descriptors.json",  extract_format_descriptors),
        ("opcode_to_encoding", "opcode_to_encoding.json",  extract_opcode_to_encoding),
        ("occupancy_consts",   "occupancy_constants.json",  extract_occupancy_constants),
        ("smem_configs",       "shared_memory_configs.json", extract_shared_memory_configs),
        ("isel_dispatch",      "isel_dispatch_tables.json", extract_isel_dispatch_tables),
        ("encoding_consts",    "encoding_constants.json",   extract_encoding_constants),
        ("phase_names",        "phase_names.json",          extract_phase_names),
        ("tier2_modifiers",    "tier2_modifiers.json",      extract_tier2_modifiers),
        ("knob_strings",       "knob_strings.json",         extract_knob_strings),
        ("blob_metadata",      "high_entropy_blob.json",    extract_high_entropy_blob_metadata),
        ("slot_template",      "universal_slot_template.json", extract_universal_slot_template),
        ("bitfield_lookup",    "encoding_bitfield_lookup.json", extract_encoding_bitfield_lookup),
        ("handler_dispatch_1", "sass_handler_dispatch_1.json",  extract_sass_handler_dispatch_1),
        ("handler_dispatch_2", "sass_handler_dispatch_2.json",  extract_sass_handler_dispatch_2),
        ("okt_knobs",          "okt_knob_descriptors.json",     extract_okt_knob_descriptors),
        ("ptx_intrinsics",     "embedded_ptx_intrinsics.json",  extract_embedded_ptx_intrinsics),
        ("supp_pass_names",    "supplemental_pass_names.json",  extract_supplemental_pass_names),
        ("latency_tables",     "per_sm_latency_tables.json",    extract_latency_tables),
        ("dep_rules",          "per_sm_dependency_rules.json",  extract_dependency_rules),
        ("scoreboard_configs", "per_sm_scoreboard_configs.json", extract_scoreboard_configs),
        ("encoding_trees",     "encoding_trees.json",           extract_encoding_trees),
        ("regclass_aux",       "register_class_aux.json",       extract_register_class_aux),
        ("regclass_constr",    "register_class_constraints.json", extract_register_class_constraints),
        ("pipeline_map",       "opcode_pipeline_map.json",      extract_opcode_pipeline_map),
        ("sched_vtable",       "scheduling_vtable.json",        extract_scheduling_vtable),
        ("regfile_config",     "register_file_config.json",     extract_register_file_config),
        ("sm_version_codes",   "sm_version_codes.json",         extract_sm_version_codes),
        ("sm_sched_seeds",     "sm_scheduling_seeds.json",      extract_sm_scheduling_seeds),
        ("sm_id_enum",         "sm_id_enumeration.json",        extract_sm_id_enumeration),
        ("extended_sass",      "extended_sass_names.json",      extract_extended_sass_names),
        ("modifier_fmtstrs",   "modifier_format_strings.json",  extract_modifier_format_strings),
        ("mod_value_tables",   "modifier_value_tables.json",    extract_modifier_value_tables),
        ("instr_legality",     "instruction_legality.json",     extract_instruction_legality),
        ("resrc_strategy",     "operand_resource_strategy.json", extract_operand_resource_strategy),
        ("mercury_dispatch",   "per_sm_handler_dispatch.json",  extract_per_sm_handler_dispatch),
        ("wgmma_intrinsic",    "wgmma_intrinsic_infra.json",    extract_wgmma_intrinsic_infra),
        ("regalloc_init",      "regalloc_init_data.json",       extract_regalloc_init),
        ("sched_enc_dispatch", "sched_encoder_dispatch.json",   extract_sched_encoder_dispatch),
        ("isel_op_constr",     "isel_operand_constraints.json", extract_isel_operand_constraints),
        ("isel_node_desc",     "isel_node_descriptors.json",    extract_isel_node_descriptors),
    ]

    for key, filename, func in extractors:
        print(f"  Extracting {key}...")
        try:
            tables[key] = func(br)
            out_path = output_dir / filename
            with open(out_path, 'w') as f:
                json.dump(tables[key], f, indent=2)
            print(f"    -> {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")
        except Exception as e:
            print(f"    ERROR: {e}", file=sys.stderr)
            tables[key] = {"error": str(e)}

    # Build cross-references
    print("  Building cross-references...")

    opcode_master = build_opcode_master(
        tables.get("opcode_names", {}),
        tables.get("encoding_cat_map", {}),
        tables.get("opcode_to_encoding", {}),
    )
    with open(output_dir / "opcode_master.json", 'w') as f:
        json.dump(opcode_master, f, indent=2)
    print(f"    -> opcode_master.json")

    encoding_geometry = build_encoding_geometry(
        tables.get("format_descriptors", {}),
        tables.get("tier2_modifiers", {}),
    )
    with open(output_dir / "encoding_geometry.json", 'w') as f:
        json.dump(encoding_geometry, f, indent=2)
    print(f"    -> encoding_geometry.json")

    # Write manifest
    manifest = {
        "binary": str(binary_path.resolve()),
        "binary_sha256": binary_hash,
        "binary_size": br.size,
        "ptxas_version": "v13.0.88",
        "cuda_toolkit": "13.0",
        "va_base": f"0x{VA_BASE:X}",
        "rodata_range": f"0x{RODATA_START:X}-0x{RODATA_END:X}",
        "rodata_size": RODATA_END - RODATA_START,
        "extraction_tables": {key: filename for key, filename, _ in extractors},
        "derived_tables": {
            "opcode_master": "opcode_master.json",
            "encoding_geometry": "encoding_geometry.json",
        },
        "files": {},
    }

    for f in sorted(output_dir.glob("*.json")):
        if f.name != "manifest.json":
            manifest["files"][f.name] = {
                "size": f.stat().st_size,
                "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
            }

    with open(output_dir / "manifest.json", 'w') as f:
        json.dump(manifest, f, indent=2)

    print(f"\n  manifest.json written to {output_dir / 'manifest.json'}")

    # Summary
    total_size = sum(f.stat().st_size for f in output_dir.glob("*.json"))
    file_count = len(list(output_dir.glob("*.json")))
    print(f"\nExtraction complete: {file_count} files, {total_size / 1024:.1f} KB total")


if __name__ == "__main__":
    main()
