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
# (8.00 bits/byte), spanning VA 0x1D4FE00 to 0x201CE00. This is likely
# compressed or encrypted data (e.g., NVVM IR templates, pre-built code
# sequences, or encoded lookup tables). We document its boundaries and
# compute a fingerprint without extracting the raw data.

HIGH_ENTROPY_BLOB_START = 0x1D4FE00
HIGH_ENTROPY_BLOB_END   = 0x201CE00


def extract_high_entropy_blob_metadata(br: BinaryReader) -> dict:
    """Document the high-entropy blob region in .rodata without extracting
    the raw bytes (it would be ~2.8 MB of incompressible data).
    Computes SHA-256 fingerprint and boundary entropy measurements.

    The blob starts with a structured header of 20 x 16-byte entries,
    each containing a single u32 pointer (to .rodata) followed by 12
    zero bytes.  The pointers are descending and point into the rodata
    section.  After this 320-byte header, the data transitions to
    near-maximum entropy."""
    import math

    blob_start_off = br._off(HIGH_ENTROPY_BLOB_START)
    blob_end_off = br._off(HIGH_ENTROPY_BLOB_END)
    blob_size = blob_end_off - blob_start_off
    blob_data = br.data[blob_start_off:blob_end_off]

    # SHA-256 of the blob
    blob_hash = hashlib.sha256(blob_data).hexdigest()

    # Scan header: count 16-byte entries where bytes [4:16] are all zero
    header_entry_count = 0
    header_pointers = []
    for i in range(min(100, blob_size // 16)):
        off = i * 16
        d0 = struct.unpack_from('<I', blob_data, off)[0]
        rest = blob_data[off + 4:off + 16]
        if rest == b'\x00' * 12 and d0 > 0x1000000:
            header_entry_count += 1
            header_pointers.append(f"0x{d0:08X}")
        else:
            break
    header_size = header_entry_count * 16

    # Overall entropy
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
    # Entropy of the data AFTER the structured header
    post_header_ent = page_entropy(blob_data[header_size:header_size + 4096]) if blob_size > header_size + 4096 else 0.0

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
            "post_header_entropy": round(post_header_ent, 4),
            "last_page_entropy": round(last_page_ent, 4),
            "header_hex_16b": header_hex,
            "header": {
                "entry_count": header_entry_count,
                "size_bytes": header_size,
                "pointers": header_pointers,
                "note": "Descending u32 pointers into .rodata, each in a 16-byte slot (12 bytes padding).",
            },
            "note": "320-byte structured header (20 rodata pointers) followed by ~2.8 MB of "
                    "near-maximum entropy data (8.00 bits/byte). Likely compressed/encrypted "
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
        if sm_num < 10 or sm_num > 200:
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
        ("pipeline_map",       "opcode_pipeline_map.json",      extract_opcode_pipeline_map),
        ("sched_vtable",       "scheduling_vtable.json",        extract_scheduling_vtable),
        ("regfile_config",     "register_file_config.json",     extract_register_file_config),
        ("sm_version_codes",   "sm_version_codes.json",         extract_sm_version_codes),
        ("sm_sched_seeds",     "sm_scheduling_seeds.json",      extract_sm_scheduling_seeds),
        ("sm_id_enum",         "sm_id_enumeration.json",        extract_sm_id_enumeration),
        ("extended_sass",      "extended_sass_names.json",      extract_extended_sass_names),
        ("modifier_fmtstrs",   "modifier_format_strings.json",  extract_modifier_format_strings),
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
