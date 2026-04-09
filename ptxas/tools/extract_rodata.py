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
        return TEXT_START <= va <= TEXT_END

    def is_in_rodata(self, va: int) -> bool:
        return RODATA_START <= va <= RODATA_END

    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def rot13(s: str) -> str:
    return codecs.decode(s, 'rot_13')


# ─── Table 1: ROT13 Opcode Name Table ──────────────────────────────────

# The InstructionInfo constructor at sub_7A5D10 stores 322 name entries.
# Each entry is a {char*, uint64} pair at object+4184. We extract by
# scanning the known string region and cross-referencing with the
# encoding category map (Table 2) which has exactly 322 entries.

# Known string cluster for opcode names: 0x21C1D00 - 0x21C2200
OPCODE_NAME_REGION_START = 0x21C1D00
OPCODE_NAME_REGION_END   = 0x21C2200

# InstructionInfo name table: 322 entries of {ptr, len} at object+4184
# The object is constructed at runtime, but we can find the name string
# pointers by scanning the constructor code. Instead, we use the encoding
# category map (Table 2) at unk_21C0E00 as an index anchor — it has
# exactly 322 entries, one per opcode.

# SM generation boundary markers (from sass-opcodes.md)
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


def extract_opcode_names(br: BinaryReader) -> dict:
    """Extract 322-entry ROT13 opcode name table by scanning the constructor's
    string references. We find names by locating the InstructionInfo name
    table pointer array in the constructor sub_BE7390 / sub_7A5D10."""

    # The name table at InstructionInfo+4184 is an array of 322 x {ptr(8), len(8)}
    # = 322 x 16 = 5152 bytes. The pointers reference ROT13 strings in .rodata.
    #
    # Strategy: scan the known .rodata region for all short ROT13-decodable strings
    # that look like SASS mnemonics, then order them by the constructor's
    # initialization sequence.
    #
    # Simpler approach: the constructor sub_7A5D10 writes sequential LEA instructions
    # that load string addresses. We find these by searching for the string pattern
    # in the binary.

    # Scan the opcode name region for NUL-terminated ASCII strings
    names_raw = []
    va = OPCODE_NAME_REGION_START
    while va < OPCODE_NAME_REGION_END:
        try:
            s = br.cstring(va, max_len=64)
            if len(s) >= 2 and s.isascii() and all(c.isalnum() or c in '_.' for c in s):
                decoded = rot13(s)
                names_raw.append({"va": va, "rot13": s, "mnemonic": decoded, "length": len(s)})
                va += len(s) + 1  # skip past NUL
            else:
                va += 1
        except (ValueError, UnicodeDecodeError):
            va += 1

    # Also scan the extended mnemonic region (0x2034000-0x203A000) for Mercury names
    mercury_names = []
    for scan_va in range(0x2034000, 0x203A000):
        try:
            s = br.cstring(scan_va, max_len=80)
            if len(s) >= 4 and s.isascii() and all(c.isalnum() or c in '_.' for c in s):
                decoded = rot13(s)
                if decoded.startswith(('MERCURY_', 'HMMA', 'IMMA', 'BMMA', 'DMMA',
                                       'QMMA', 'OMMA', 'GMMA', 'UTC', 'FENCE',
                                       'SYNCS', 'CCTL', 'ACQBULK')):
                    mercury_names.append({"va": scan_va, "rot13": s, "mnemonic": decoded})
                scan_va += len(s) + 1
            else:
                scan_va += 1
        except (ValueError, UnicodeDecodeError):
            scan_va += 1

    return {
        "opcode_names": {
            "primary_count": len(names_raw),
            "primary_region": f"0x{OPCODE_NAME_REGION_START:X}-0x{OPCODE_NAME_REGION_END:X}",
            "entries": names_raw[:322],  # cap at 322
            "sm_boundaries": SM_BOUNDARIES,
        },
        "mercury_extended_names": {
            "count": len(mercury_names),
            "entries": mercury_names[:800],
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

FORMAT_DESCRIPTORS = [
    # (va, label, width_bits, wiki_encoder_count)
    (0x23F1D70, "64b_B",    64,  70),
    (0x23F1F08, "64b_A",    64, 215),
    (0x23F1F90, "64b_C",    64,  20),
    (0x23F2238, "64b_D",    64,  17),
    (0x23F2C50, "64b_E",    64,   1),
    (0x23F1DF8, "128b_0x03", 128, 202),
    (0x23F2018, "128b_0x07", 128,  26),
    (0x23F2128, "128b_0x09", 128,   2),
    (0x23F21B0, "128b_0x0A", 128, 135),
    (0x23F2348, "128b_0x0D", 128,  11),
    (0x23F25F0, "128b_0x12", 128,  21),
    (0x23F2678, "128b_0x13", 128, 143),
    (0x23F2810, "128b_0x16", 128,   6),
    (0x23F29A8, "128b_0x19", 128, 152),
    (0x23F2DE8, "128b_0x21", 128,   2),
    (0x23F2EF8, "128b_0x23", 128,   9),
]


def extract_format_descriptors(br: BinaryReader) -> dict:
    results = []
    for va, label, width, enc_count in FORMAT_DESCRIPTORS:
        lo, hi = br.xmm(va)
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

        results.append({
            "va": f"0x{va:X}",
            "label": label,
            "instruction_width": width,
            "xmmword_lo": f"0x{lo:016X}",
            "xmmword_hi": f"0x{hi:016X}",
            "slot_sizes": slot_sizes,
            "slot_types": slot_types,
            "slot_flags": slot_flags,
            "active_slots": active,
            "wiki_encoder_count": enc_count,
        })

    return {"format_descriptors": results}


# ─── Table 4: Opcode-to-Encoding Table ─────────────────────────────────

OPCODE_ENC_TABLE_VA = 0x22B4B60
OPCODE_ENC_TABLE_COUNT = 222
OPCODE_ENC_SENTINEL = 355


def extract_opcode_to_encoding(br: BinaryReader) -> dict:
    entries = br.u16_array(OPCODE_ENC_TABLE_VA, OPCODE_ENC_TABLE_COUNT)
    non_zero = sum(1 for e in entries if e != 0)
    sentinel_count = sum(1 for e in entries if e == OPCODE_ENC_SENTINEL)

    return {
        "opcode_to_encoding": {
            "count": OPCODE_ENC_TABLE_COUNT,
            "sentinel_value": OPCODE_ENC_SENTINEL,
            "source_va": f"0x{OPCODE_ENC_TABLE_VA:X}",
            "non_zero_count": non_zero,
            "sentinel_count": sentinel_count,
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
    # Read global table — scan for zero-terminated DWORD array
    global_sizes = []
    va = SMEM_GLOBAL_TABLE_VA
    for i in range(20):
        val = br.u32(va + i * 4)
        global_sizes.append(val)
        if i > 2 and val == 0 and br.u32(va + (i + 1) * 4) == 0:
            break

    # Read sm_75 table (3 entries known)
    sm75_sizes = br.u32_array(SMEM_SM75_TABLE_VA, 3)

    return {
        "shared_memory_configs": {
            "global_table": {
                "va": f"0x{SMEM_GLOBAL_TABLE_VA:X}",
                "sizes_bytes": global_sizes,
                "sizes_kb": [s // 1024 for s in global_sizes if s > 0],
            },
            "sm_75": {
                "va": f"0x{SMEM_SM75_TABLE_VA:X}",
                "count": 3,
                "sizes_bytes": sm75_sizes,
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

    # Validate pointers
    valid = sum(1 for p in ptrs if br.is_in_text(p))
    sentinel = sum(1 for p in ptrs if p == ISEL_SENTINEL_VA)
    unique = len(set(ptrs))

    return {
        "isel_dispatch_tables": {
            "source_va": f"0x{ISEL_DISPATCH_VA:X}",
            "end_va": f"0x{ISEL_DISPATCH_END:X}",
            "total_pointers": count,
            "valid_text_pointers": valid,
            "sentinel_count": sentinel,
            "sentinel_va": f"0x{ISEL_SENTINEL_VA:X}",
            "unique_targets": unique,
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

# Knob names are ROT13 strings in .rodata referenced by ctor_005.
# The knob descriptor table itself is in .bss (runtime-initialized).
# We extract only the name strings from .rodata.

KNOB_STRING_REGIONS = [
    (0x21B6000, 0x21C1000, "OCG knobs (ctor_005)"),
    (0x21DB000, 0x21DE000, "DAG knobs (ctor_007)"),
]


def extract_knob_strings(br: BinaryReader) -> dict:
    all_knobs = []
    for start, end, label in KNOB_STRING_REGIONS:
        va = start
        while va < end:
            try:
                s = br.cstring(va, max_len=128)
                if (len(s) >= 3 and s.isascii() and
                    all(c.isalnum() or c == '_' for c in s) and
                    any(c.isupper() for c in s)):
                    decoded = rot13(s)
                    # Filter: knob names are CamelCase or UPPER_CASE
                    if (decoded[0].isupper() and len(decoded) >= 3):
                        all_knobs.append({
                            "va": f"0x{va:X}",
                            "rot13": s,
                            "name": decoded,
                            "region": label,
                        })
                    va += len(s) + 1
                else:
                    va += 1
            except (ValueError, UnicodeDecodeError):
                va += 1

    return {
        "knob_strings": {
            "total_count": len(all_knobs),
            "regions": [{"start": f"0x{s:X}", "end": f"0x{e:X}", "label": l}
                        for s, e, l in KNOB_STRING_REGIONS],
            "entries": all_knobs,
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

    # Determine SM generation per opcode
    def sm_gen(idx):
        for name, boundary in sorted(SM_BOUNDARIES.items(), key=lambda x: x[1]):
            if idx <= boundary:
                if "LAST" in name:
                    gen = name.replace("_LAST", "").replace("_FIRST", "")
                    return gen.lower()
        return "sm_104+"

    records = []
    for i in range(min(322, len(name_entries))):
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
