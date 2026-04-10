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

# The format descriptor region is a contiguous array of 34 entries at 136-byte
# stride starting at 0x23F1D70.  Each entry contains a 16-byte xmmword header
# followed by 3 x 10 DWORD arrays (slot_sizes, slot_types, slot_flags).
# The first DWORD of the xmmword is the format ID used by the encoder.
# wiki_encoder_count is 0 for descriptors not yet catalogued in the wiki.

FORMAT_DESCRIPTOR_REGION_START = 0x23F1D70
FORMAT_DESCRIPTOR_STRIDE = 136
FORMAT_DESCRIPTOR_COUNT = 34

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

OCCUPANCY_XMMWORDS = [
    (0x229C400, "base_occupancy_params"),
    (0x229C410, "secondary_param_block"),
    (0x229C420, "primary_occupancy_params"),
    (0x229C430, "sm60_sm70_base"),
    (0x229C440, "common_sm30_sm75"),
    (0x229C450, "sm35_sm37_variants"),
    (0x229C460, "sm3x_sm7x_variants"),
    (0x229C470, "sm70plus_granularity2"),
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
ISEL_DISPATCH_END = 0x22B1480
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


# ─── Table 8: Per-Instruction Encoding Constants ───────────────────────

ENC_CONSTS_VA = 0x22A1500
ENC_CONSTS_END = 0x22A1E00


def extract_encoding_constants(br: BinaryReader) -> dict:
    count = (ENC_CONSTS_END - ENC_CONSTS_VA) // 4
    entries = br.u32_array(ENC_CONSTS_VA, count)
    non_zero = sum(1 for e in entries if e != 0)
    max_val = max(entries) if entries else 0

    return {
        "per_instruction_encoding_consts": {
            "source_va": f"0x{ENC_CONSTS_VA:X}",
            "count": count,
            "non_zero_count": non_zero,
            "max_value": max_val,
            "max_value_hex": f"0x{max_val:X}",
            "entries": entries,
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
    ("group_A_maxwell_turing",     0x202A280, 4, "sm_50-sm_75"),
    ("group_B_ampere_ada",         0x22F1B30, 3, "sm_80-sm_89"),
    ("group_D_lovelace_hopper",    0x22F1BA0, 2, "sm_89-sm_90"),
    ("group_E_blackwell_dc",       0x22F1AA0, 4, "sm_100-sm_103"),
    ("group_F_blackwell_consumer", 0x22F1C20, 2, "sm_120-sm_121"),
    ("group_G_cross_arch",         0x23B2DE0, 1, "cross-architecture"),
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


def _is_rot13_encoded(raw_str: str) -> bool:
    """Determine if a binary string is ROT13-encoded (actual knob name)
    or plaintext (non-knob constant that happens to be in the region).

    ROT13 is a self-inverse cipher, so it's mathematically impossible to
    distinguish ROT13 from plaintext without semantic knowledge.  We use
    an explicit allowlist of known plaintext constants (shader stage names,
    function names, config strings) and treat everything else as ROT13,
    which is correct for the vast majority of entries in the knob regions."""
    return raw_str not in _KNOB_PLAINTEXT_CONSTANTS


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


# ─── Derived: Opcode Master Record ─────────────────────────────────────

def build_opcode_master(names: dict, cat_map: dict, enc_table: dict) -> dict:
    """Cross-reference opcode names, encoding categories, and ISel encoding slots."""
    name_entries = names.get("opcode_names", {}).get("entries", [])
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
