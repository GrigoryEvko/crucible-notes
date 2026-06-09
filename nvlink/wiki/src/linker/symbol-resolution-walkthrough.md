# Symbol Resolution Walkthrough

This page traces a complete multi-input symbol-resolution scenario through the decompiled code, complementing the algorithm reference in [Symbol Resolution](symbol-resolution.md) and the per-phase decomposition in [Symbol Addition](symbol-addition.md). Every hash value is the actual `sub_44E000` MurmurHash3\_x86\_32 output for the given name, every line reference is to `decompiled/sub_440BE0_0x440be0.c` (which matches `elfw_add_symbol` at `0x440BE0`) or `decompiled/sub_448E70_0x448e70.c` (`hash_map_insert` at `0x448E70`), and every offset into the elfw context and symbol record matches the values listed in [Symbol Resolution](symbol-resolution.md#elf-writer-symbol-storage). The page closes with a resolution-rules matrix that summarises every existing/incoming binding combination at the level of which decompiled lines and helper functions fire.

## Scenario

Three inputs drive the linker:

| Input | Symbol | Binding | Type | Section | Notes |
|---|---|---|---|---|---|
| `input1.o` | `main_kernel` | `STB_GLOBAL` | `STT_FUNC` | `.text` (idx 1) | Strong definition |
| `input2.o` | `main_kernel` | `STB_GLOBAL` | `STT_NOTYPE` | `SHN_UNDEF` (0) | Undefined reference |
| `input2.o` | `helper_fn` | `STB_WEAK` | `STT_FUNC` | `.text` (idx 1) | Weak definition |
| `input2.o` | `__nv_sqrt` | `STB_GLOBAL` | `STT_NOTYPE` | `SHN_UNDEF` (0) | Undefined reference |
| `libdevice.a(sqrt.o)` | `__nv_sqrt` | `STB_GLOBAL` | `STT_FUNC` | `.text` | Strong (whole-archive loaded) |
| `libdevice.a(helper.o)` | `helper_fn` | `STB_GLOBAL` | `STT_FUNC` | `.text` | Strong (whole-archive loaded) |

The linker processes `input1.o`, then `input2.o`, then iterates **every** member of `libdevice.a` unconditionally and merges each one's symbols into the same elfw. nvlink does not perform symbol-directed (on-demand) archive extraction; see [Archives — Whole-Archive vs On-Demand Loading](../input/archives.md#whole-archive-vs-on-demand-loading) for the verified behavior in `main_0x409800.c` lines 756-777. Throughout the walkthrough the elfw context has a hash map at `ctx+288` initially sized to 64 buckets (mask `0x3F` at `map+40`), empty positive and negative symbol arrays at `ctx+344` and `ctx+352`, and `ctx+304` (name counter) initialized to zero.

> **QUIRK vs GNU ld**: A traditional Unix linker would scan `libdevice.a`'s armap, identify only members that satisfy currently-undefined symbols, and load just those (single-pass; `ld` does not iterate to a fixed point unless `--start-group`/`--end-group` is used). nvlink does the opposite: it ignores the armap (the `/` member is structurally detected and skipped without parsing), loads every member, and lets the [dead code elimination](dead-code-elimination.md) pass at `sub_44AD40` sweep unreachable functions. There is no fixed-point convergence loop because there is no symbol-directed extraction to converge.

## Step 1: Compute MurmurHash3 Values

The string hash function `sub_44E000` (documented in [Hash Tables](hash-tables.md#string-hash-murmurhash3_x86_32)) produces the following `uint32_t` hash values with seed 0. These were verified against a reference Python implementation of MurmurHash3\_x86\_32 to confirm they match Austin Appleby's published algorithm exactly:

| Name | `murmur3(name)` | Hex | Bucket (mask `0x3F`) |
|---|---|---|---|
| `main_kernel` | `3,328,480,444` | `0xC65D92BC` | `0xC65D92BC & 0x3F = 60` |
| `helper_fn` | `1,000,959,075` | `0x3BA6AA63` | `0x3BA6AA63 & 0x3F = 35` |
| `__nv_sqrt` | `3,170,487,566` | `0xBCFBED0E` | `0xBCFBED0E & 0x3F = 14` |

The bucket index is computed in `sub_448E70` at line 225 of the decompiled source for hashing mode 0 (string keys): `v85 = *((_DWORD *)v3 + 10) & v84;` — this reads the mask at `map+40` (dword index 10) and ANDs it with the hash output. The bucket array is at `map+104` (qword index 13: `*((_QWORD *)v3 + 13) + 8 * v85`).

## Step 2: Process `input1.o` - Add `main_kernel`

The merge loop calls `elfw_add_function_symbol` (`sub_442CA0`) for the strong global function. Trace through the decompiled code:

**Line 66**: `sub_449A80(ctx+288, "main_kernel")` probes the hash map. The map is empty, so the lookup returns NULL and `v7 = NULL`. Control falls through to the insertion path.

**Lines 73-118**: Section index resolution via `sub_464DB0` on `ctx+344` slot 0 returns the `.text` section record; its `st_shndx` is `1` (not `0xFFFF`), so `v14 = 1` directly.

**Lines 121-125**: A second hash map probe (`sub_449A80(ctx+288, "main_kernel")`) confirms `v16 = 0` (not found).

**Lines 126-132**: Arena allocation of a 48-byte symbol record via `sub_4307C0(arena, 48)`. The three 128-bit zero stores at lines 130-132 (`*(_OWORD *)v23 = 0; *((_OWORD *)v23 + 1) = 0; *((_OWORD *)v23 + 2) = 0;`) clear the record.

**Lines 184-200**: Because `v16 == 0`, control skips the duplicate path and jumps to `LABEL_39`. The 12-byte hash map entry is allocated at line 186: `sub_4307C0(v46, 12)`. The name counter at `ctx+304` is incremented to `1` (line 191). A name buffer of length 12 (`strlen("main_kernel") + 1`) is allocated at line 194, and the name is copied with `strcpy` (line 200). The symbol record's `name_str` (qword at record+32, i.e., `v23[4]`) is set to the copied name pointer at line 202. Finally `sub_448E70(name_map, name_copy, entry)` (line 203) inserts the entry into the hash map.

Inside `sub_448E70`, line 218 computes `v84 = (*(hash_fn))(name_copy)` — for mode 0 with no context, this calls the function pointer at `map+0`, which is `sub_44E000` (MurmurHash3). The returned value is `0xC65D92BC`. Line 225 computes `v85 = 0xC65D92BC & 0x3F = 60`, and line 226 reads `*(_QWORD *)(map+104 + 8*60)`, which is NULL (empty bucket). Control proceeds to the bucket allocation path at line 456-470: a 12-byte bucket header is allocated (`sub_4307C0(arena, 12)` at line 460), initialized with `*v43 = 1` (one entry), `v43[1] = 29` (the new entry's index in the entry array at `map+88`), and `v43[2] = 0xFFFFFFFF` (the `-1` sentinel terminating the chain). The entry slot itself (line 475-477) stores the name pointer and the `entry_ptr` value at `map+88 + 16*29`.

**Lines 205-208 of `sub_442CA0`**: The symbol record is populated — `st_other = visibility`, `st_value = 0`, `st_size = 0`, `st_info = 0x12` (`STB_GLOBAL << 4 | STT_FUNC`, computed in `v66 = 16 * a3 + 2` at line 144).

**Lines 209-221**: Because `a3 == 1` (STB_GLOBAL), the symbol goes into the negative array. `sub_464BB0(ctx+352)` returns `0` (empty array), so `v45 = 0` and `*((_DWORD *)v23 + 6) = -0 = 0`. The symbol is pushed into `ctx+352` via `sub_464C30(v23, *(ctx+352))`. **Note**: because `-v45` is also `0`, the first negative-array slot is indexed by `0`, which is also the indexing convention for `SHN_UNDEF`. In practice `elfw_add_symbol` inserts a sentinel into position 0 of the negative array during elfw initialization so that real globals start at index `-1`. For the walkthrough we assume position 0 is already occupied, so `main_kernel` gets `sym_index = -1`.

**Lines 223-224**: `v14 = 1` (the resolved `.text` index), which is `<= 0xFEFF`, so `*((_WORD *)v23 + 3) = 1`. No extended section index path.

**Lines 289-296**: `sub_42F850` checks the STO_CUDA_OBSCURE bit; `v24` (the entry pointer) is non-NULL, so `*(_DWORD *)v24 = v34 = -1`, writing the assigned `sym_index` into the hash map entry at `entry+8`. The function ordinal counter at `ctx+416` is bumped to `1` and written into the symbol record at offset `+28`. `sub_44B940(a1, -1)` registers the function in the callgraph.

**Line 301**: `sub_442820(a1, "main_kernel", visibility, -1)` is the UFT/merge-symbols hook; for a first-time strong global it is a no-op because there is nothing to merge.

**Line 302**: Returns `-1`.

**State after Step 2**:

```text
pos_symbols (ctx+344):  [sentinel, ...]
neg_symbols (ctx+352):  [sentinel, main_kernel]        // index -1 = main_kernel
name_map (ctx+288) buckets [mask 0x3F]:
    bucket 60 -> [count=1, entries=[29], -1]           // 29 = entry slot in map+88
    (all other buckets NULL)
entries (map+88):
    slot 29: key="main_kernel" (ptr), value=entry_ptr_29
entry_ptr_29 at arena offset:
    padding (8 bytes zero) | sym_index = -1
name_counter (ctx+304): 1
func_ordinal_counter (ctx+416): 1
```

## Step 3: Process `input2.o` - `main_kernel` Undefined Reference

`input2.o` has an undefined reference to `main_kernel`. The merge loop calls `elfw_add_symbol` (`sub_440BE0`) with `section_index = 0` (`SHN_UNDEF`), `binding = STB_GLOBAL`, `sym_type = STT_NOTYPE`, `value = 0`, `size = 0`.

**Line 78**: Because `a6 == 0` (not negative), `sub_464DB0(ctx+344, 0)` returns the positive-array sentinel. Its `st_shndx` is `0`, not `0xFFFF`, so `v17 = 0` and control falls through to `LABEL_3`.

**Lines 125-129**: `sub_449A80(ctx+288, "main_kernel")` now hits the existing entry. The hash goes to bucket 60 (`0xC65D92BC & 0x3F`), the bucket chain walker reads entry slot 29, compares the key pointer against the new name string. Mode 0 string comparison uses `map+8`/`map+16` which is `strcmp`. The comparison succeeds, the slot's value pointer is returned, and `*v20 = -1` is loaded into `v19`. So `v19 = -1` (the existing `main_kernel` index).

**Lines 137-147**: Callgraph-completed check; callgraph not yet finalized, so skip.

**Lines 148-184**: `v19 != 0`, so enter the duplicate path. `v19 < 0`, so `v28 = sub_464DB0(ctx+352, 1) = existing_main_kernel_record`.

**Line 162**: `a4 == 1` (STB_GLOBAL). **Line 164**: `*((_BYTE *)v28 + 4) >> 4` reads `st_info >> 4` of the existing record, which is `(0x12 >> 4) = 1`. The condition `== 1` is true.

At this point the binary would normally trigger `sub_467460` with `"adding global symbols of same name"` — **but this is only invoked for actual strong-on-strong duplicate definitions**. For an undefined reference, the merge layer above this function (`sub_45E7D0`, the per-input-object merge loop) short-circuits the call entirely: when `section_index == 0` (undefined) and an existing entry is found, the merge loop does not re-add the symbol, it just records the reference in its own relocation bookkeeping. So in practice control never reaches `sub_440BE0` for this particular symbol. The hash map and symbol arrays are unchanged.

This is one of the key insights of the resolution design: `sub_440BE0` is the low-level insertion function, and the higher-level merge loop in `sub_45E7D0` handles the filtering of undefined-vs-defined cases before calling it. See [Merge Phase](../pipeline/merge.md) for the per-object dispatch logic.

## Step 4: Process `input2.o` - `helper_fn` Weak Definition

Next `input2.o` provides `helper_fn` as `STB_WEAK`, `STT_FUNC`, section `.text` (idx 1). The merge loop calls `elfw_add_function_symbol` (`sub_442CA0`) with `a3 = 2` (STB_WEAK).

**Line 66**: Hash map probe for `"helper_fn"`. `murmur3("helper_fn") = 0x3BA6AA63`, bucket = `35`. Bucket 35 is empty; `sub_449A80` returns NULL, `v7 = NULL`. Fall through.

**Lines 73-118**: Section index resolution returns `v14 = 1` (the `.text` idx from the current input's section table).

**Lines 121-125**: Second probe still returns NULL, `v16 = 0`.

**Lines 126-132**: 48-byte record allocation, zero-fill.

**Line 144**: `v66 = 16 * 2 + 2 = 0x22` (STB_WEAK << 4 | STT_FUNC).

**Line 145 onwards**: `v16 == 0`, branch to `LABEL_39`.

**Line 184 (LABEL_39)**: 12-byte entry allocation, name_counter++ (now `2`), strdup of `"helper_fn"`, `sub_448E70(map, "helper_fn", entry)` inserts into the hash map.

Inside `sub_448E70`: hash function returns `0x3BA6AA63`, bucket = `35`. Bucket 35 is NULL, so the bucket allocation path runs. A 12-byte bucket header is allocated, initialized with `*v43 = 1`, `v43[1] = 42` (the new entry slot in `map+88`), `v43[2] = -1`.

**Lines 205-208**: Populate record: `st_other`, `st_info = 0x22`, `st_value = 0`, `st_size = 0`.

**Line 209**: `a3 == 2` (STB_WEAK, not STB_GLOBAL), so fall through to `LABEL_14`. The symbol goes into the **positive** array at `ctx+344` (weak symbols share the positive array with locals in this implementation; only `STB_GLOBAL` gets the negative array). `sub_464BB0(ctx+344)` returns the current positive count, call it `17` (an arbitrary value after section symbols have been added earlier in merge). `*((_DWORD *)v23 + 6) = 17`. Push into positive array.

**Lines 223-225**: `v14 = 1`, direct `st_shndx` store.

**Lines 289-296**: Entry pointer updated to `17`. Callgraph registered with ordinal `2`.

**Line 301**: `sub_442820(a1, "helper_fn", visibility, 17)` is the weak-merge hook. Since this is the first definition of `helper_fn`, it is a no-op at this level.

**State after Step 4**:

```text
pos_symbols (ctx+344): [sentinel, ..., helper_fn@17]
neg_symbols (ctx+352): [sentinel, main_kernel]
name_map buckets:
    bucket 35 -> [count=1, entries=[42], -1]
    bucket 60 -> [count=1, entries=[29], -1]
entries:
    slot 29: "main_kernel" -> entry_ptr_29 (sym_index = -1)
    slot 42: "helper_fn"   -> entry_ptr_42 (sym_index = 17)
name_counter: 2
```

## Step 5: Process `input2.o` - `__nv_sqrt` Undefined Reference

`input2.o` references `__nv_sqrt` as `STB_GLOBAL`, `STT_NOTYPE`, `SHN_UNDEF`. Same as Step 3, the higher-level merge loop detects that this is an undefined reference with no matching definition yet in the output, but instead of dropping the reference entirely it **adds a placeholder entry** to the negative symbol array and marks it as "needs resolution". This placeholder lives in the hash map so subsequent lookups from archive members can find it.

**Line 125-129 in `sub_440BE0`**: `sub_449A80(ctx+288, "__nv_sqrt")` returns NULL (first encounter), so `v19 = 0`.

**Lines 194-213**: Fresh insertion path. Entry allocation, name_counter++ (now `3`), strdup, `sub_448E70(map, "__nv_sqrt", entry)`.

Inside `sub_448E70`: hash = `0xBCFBED0E`, bucket = `14`. Empty, bucket allocation runs, entry slot (say `55`) is assigned.

**Line 215**: `st_info = (0 & 0xF) + 16 * 1 = 0x10` (STB_GLOBAL | STT_NOTYPE).

**Lines 219-223**: STB_GLOBAL -> negative array. `sub_464BB0(ctx+352)` returns `2` (one sentinel + main_kernel pushed earlier). So `-v41 = -2`, `*((_DWORD *)v26 + 6) = -2`. Push into negative array.

**Line 232**: `v17 = 0` (SHN_UNDEF), `0 <= 0xFEFF`, store directly. No extended path.

**Line 295-297**: Entry pointer gets `*(_DWORD *)v28 = -2`.

**State after Step 5**:

```text
pos_symbols (ctx+344): [sentinel, ..., helper_fn@17]
neg_symbols (ctx+352): [sentinel, main_kernel, __nv_sqrt(UND)]
name_map buckets:
    bucket 14 -> [count=1, entries=[55], -1]
    bucket 35 -> [count=1, entries=[42], -1]
    bucket 60 -> [count=1, entries=[29], -1]
entries:
    slot 29: "main_kernel" -> entry_ptr_29 (sym_index = -1)
    slot 42: "helper_fn"   -> entry_ptr_42 (sym_index = 17)
    slot 55: "__nv_sqrt"   -> entry_ptr_55 (sym_index = -2, SHN_UNDEF)
name_counter: 3
```

## Step 6: Whole-Archive Member Merge — `libdevice.a(sqrt.o)`

When the input-loop dispatch reaches `libdevice.a` (see [Input Loop](../pipeline/input-loop.md#archive-handling) and [Archives](../input/archives.md)), `main()` opens the archive via `sub_4BDAC0` and runs an unconditional `while(1) { archive_next; if (!s1) break; sub_42AF40(..., from_archive=1, ...); }` loop over every member. The archive's `/` armap is never consulted. Each extracted member is classified by `sub_4BDB70` and merged through the same `sub_42AF40 -> sub_442CA0 / sub_440BE0` path that ordinary command-line objects use.

`sub_42A2D0` is sometimes named "archive_validate_callback" in the function map — note that this name refers to **CPU architecture** validation (matching `e_machine` against `qword_2A5F2A0` for `X86_64`/`AARCH64`/`ARMv7`/`PPC64LE`), not symbol-table validation. It runs during library *search* (pass 2 of `-l` resolution) and does not feed back into the symbol resolver.

For this scenario, `libdevice.a(sqrt.o)` is loaded regardless of whether `__nv_sqrt` was previously undefined. The interesting case is what happens when its `__nv_sqrt` definition meets the pre-existing `SHN_UNDEF` placeholder at negative slot `-2`. Focus on the `__nv_sqrt` addition:

**Entry to `sub_442CA0`** (because `STT_FUNC`): `a3 = 1` (STB_GLOBAL), `a4 = visibility`.

**Line 66**: `sub_449A80(ctx+288, "__nv_sqrt")` -> hash `0xBCFBED0E`, bucket `14`, chain walk finds entry slot 55, returns pointer to `entry_ptr_55`. `v7 != NULL`, `v8 = *v7 = -2` (the existing UND slot).

**Line 70**: `*v7 != 0`, so goto `LABEL_26` at line 300 — **the function short-circuits**. Instead of creating a new record, it calls `sub_442820(a1, "__nv_sqrt", visibility, -2)` with the existing index. `sub_442820` (`elfw_merge_symbols`) is the weak/UFT resolution helper; when the existing slot is an UND placeholder and the incoming symbol is a strong definition, it updates the existing negative-array record in place: sets `st_shndx` to the new section index, `st_info = 0x12`, `st_value` to the function's offset in the new section, etc.

So `__nv_sqrt` at negative index `-2` is **upgraded in place** from UND to a strong definition. The hash map entry pointer already points to slot `-2`, so no rebucketing is needed. The entry at slot 55 still references index `-2`, which now holds a resolved record.

**State after Step 6**:

```text
pos_symbols (ctx+344): [sentinel, ..., helper_fn@17]
neg_symbols (ctx+352): [sentinel, main_kernel, __nv_sqrt(RESOLVED)]
name_map buckets:
    bucket 14 -> [count=1, entries=[55], -1]
    bucket 35 -> [count=1, entries=[42], -1]
    bucket 60 -> [count=1, entries=[29], -1]
name_counter: 3 (unchanged)
```

## Step 7: Whole-Archive Member Merge — Strong Replaces Weak (`helper_fn`)

Whole-archive iteration means `libdevice.a(helper.o)` is merged into the elfw regardless of whether the output currently has any reference to `helper_fn`. When its strong `helper_fn` definition meets the pre-existing weak definition at positive slot `17`, standard ELF semantics dictate that the strong definition replaces the weak. nvlink implements this in the weak-resolution helper `sub_442820` (`elfw_merge_symbols`) rather than in `sub_440BE0`.

For `libdevice.a(helper.o)` providing a strong `helper_fn`, the sequence is:

1. Archive member extraction pulls in `helper.o` because the `while(1)` loop in `main()` iterates every member; the input-loop archive dispatch does not consult the symbol table to decide which members to load. **There is no `--whole-archive` / `--no-whole-archive` flag in nvlink**; whole-archive is the only loading mode. The traditional ld criterion of "UND symbol in output matches exported symbol in archive member" is **not** implemented — nvlink never reads the GNU armap (the `/` member is skipped structurally without parsing its contents).

2. `helper.o` is fully merged. During merge, `sub_442CA0` is called for `helper_fn` with `a3 = 1` (STB_GLOBAL).

3. **Line 66**: `sub_449A80` hits the existing weak entry at slot 42, returns pointer, `v8 = 17`.

4. **Line 70**: `*v7 != 0`, goto `LABEL_26`. Line 301: `sub_442820(a1, "helper_fn", visibility, 17)`.

5. Inside `sub_442820`, the weak-resolution logic takes over (documented in [Weak Symbol Handling](weak-symbols.md)). It detects that the incoming symbol has `binding == STB_GLOBAL` and the existing has `binding == STB_WEAK`. The unconditional replacement path emits the verbose trace `"replace weak function %s"` and performs the four cleanup passes:
   - Remove relocations pointing to the old weak definition (`"remove weak reloc"`).
   - Remove `.nv.info` entries for the old weak function.
   - Remove OCG constant sections belonging to the old weak function.
   - Remove debug relocations.

6. The old weak record at positive index 17 is zeroed out (its section assignments are cleared). A new symbol record is created for the strong `helper_fn` and pushed into the **negative** array at index `-3` (the new slot after `__nv_sqrt` at `-2`). The hash map entry at slot 42 is updated from `17` to `-3`.

**State after Step 7**:

```text
pos_symbols (ctx+344): [sentinel, ..., helper_fn@17(ZEROED)]
neg_symbols (ctx+352): [sentinel, main_kernel, __nv_sqrt, helper_fn(strong)]
name_map buckets:
    bucket 14 -> [count=1, entries=[55], -1]
    bucket 35 -> [count=1, entries=[42], -1]
    bucket 60 -> [count=1, entries=[29], -1]
entries:
    slot 29: "main_kernel" -> entry_ptr_29 (sym_index = -1)
    slot 42: "helper_fn"   -> entry_ptr_42 (sym_index = -3)  <-- updated
    slot 55: "__nv_sqrt"   -> entry_ptr_55 (sym_index = -2)
name_counter: 3 (unchanged)
```

The zeroed slot at positive index 17 is garbage-collected by dead code elimination (`sub_44AD40`) during the sweep pass — it is unreachable from any output symbol because the hash map no longer points to it, and no relocation targets it after the `"remove weak reloc"` cleanup.

## Step 8: Final Resolved Symbol Table

After merge, dead code elimination, and section layout, the output ELF symbol table (as it would appear in the final `.symtab`) contains:

| Output idx | Name | Binding | Type | Section | Value | Source |
|---|---|---|---|---|---|---|
| 0 | (none) | LOCAL | NOTYPE | UND | 0 | ELF sentinel |
| 1 | `main_kernel` | GLOBAL | FUNC | `.text` | `0x0000` | `input1.o` |
| 2 | `__nv_sqrt` | GLOBAL | FUNC | `.text` | `0x0080` | `libdevice.a(sqrt.o)` |
| 3 | `helper_fn` | GLOBAL | FUNC | `.text` | `0x00C0` | `libdevice.a(helper.o)` |

The internal negative indices `-1`, `-2`, `-3` have been linearized to output indices `1`, `2`, `3` by the symbol table writer in `sub_45EB00`, which iterates `neg_symbols` in order and assigns sequential output indices. The positive array is skipped in this walkthrough because it contains only section symbols and the zeroed weak `helper_fn` slot, which DCE eliminated.

## Hash Table State Summary

All four states of bucket 35 (where the `helper_fn` contention played out) across the walkthrough:

```text
After Step 2:  bucket 35 -> NULL
After Step 4:  bucket 35 -> [count=1, entries=[42(helper_fn weak, idx=17)], -1]
After Step 6:  bucket 35 -> [count=1, entries=[42(helper_fn weak, idx=17)], -1]  (unchanged)
After Step 7:  bucket 35 -> [count=1, entries=[42(helper_fn strong, idx=-3)], -1]
```

Notice that **the bucket structure itself never changes during the weak-to-strong replacement** — only the `sym_index` field inside the entry node at `entry+8` is updated. This is why `sub_440BE0` re-probes the hash map at line 183 (`v28 = sub_449A80(v21, a2)`) after handling the duplicate case: it needs the entry pointer to write the new index.

## Collision Handling Example

The scenario above has three names that map to distinct buckets, so no in-bucket collisions occur. To illustrate collision resolution, consider what would happen if the input also had a symbol `"my_kernel"`. Its MurmurHash3 is `0xB294C63C`, and at mask `0x3F` the bucket is `0xB294C63C & 0x3F = 60` — **the same bucket as `main_kernel`**.

When `sub_448E70` inserts `"my_kernel"` at Step 2.5:

1. Line 218 computes hash `0xB294C63C`.
2. Line 225 computes bucket `60`.
3. Line 226 reads the bucket at `map+104 + 8*60`, which is **non-NULL** (holds the bucket from the earlier `main_kernel` insertion).
4. The collision resolution path (lines 393-455) runs. The bucket header at `v42` holds `*v43 = 1` (current entry count in bucket). The entry index at `v43[1] = 29` is the existing `main_kernel` slot, `v43[2] = -1` is the sentinel.
5. Line 397 checks `if (v43[1] == -1)` — no, it is `29`. Line 404-410 walks the chain counting entries; `j = 0`, so `v47 = 2`, `v46 = 12`, `v48 = 8`.
6. Line 413 checks `if (*v43 < (unsigned int)v47)` — `1 < 2`, yes, so the bucket needs to grow. Line 418-452 allocates a new bucket header of doubled size: `4 * (2 * 1 + 2) = 16` bytes. It copies the existing entries, sets `*(_DWORD *)nd = v140 = 2 * 1 = 2` (new capacity), appends the new entry's index at offset `v48 = 8`, and writes the `-1` sentinel at offset `v46 = 12`. The old bucket header is freed via `sub_431000(v43, v152)`.
7. Line 471 writes the new bucket pointer into `*v42` (the bucket slot at `map+104 + 8*60`).

After the collision-resolving insertion:

```text
bucket 60 -> [capacity=2, entries=[29(main_kernel, -1), N(my_kernel, -2)], -1]
```

Subsequent lookups for either `"main_kernel"` or `"my_kernel"` at bucket 60 walk the entry chain, compare keys via `strcmp` (the function pointer at `map+8` for mode 0 without context), and return the matching slot. Chain walking is implemented at lines 173-187 of `sub_448E70`:

```c
v91 = *(unsigned int **)(map+104 + 8*bucket);   // bucket header
if (v91) {
    while (1) {
        v92 = *++v91;                            // next entry index
        if ((_DWORD)v92 == -1) break;            // end of chain
        v26 = (char **)(map+88 + 16*v92);        // entry at slot v92
        if (name == *v26)                        // pointer-equal keys
            return &v26[1];                      // found: return value ptr
    }
}
```

(The mode 0 path is at lines 218-247 and uses the `strcmp` function pointer rather than pointer equality.)

## Resolution Rules Matrix

The decision table below combines the low-level `sub_440BE0` logic at lines 148-191 with the merge-level weak/strong arbitration in `sub_442820`. Rows indicate the binding of the existing symbol in the output elfw; columns indicate the binding of the incoming symbol. Each cell describes the resulting action and which function implements it.

| Existing \\ Incoming | `STB_LOCAL` | `STB_GLOBAL` | `STB_WEAK` |
|---|---|---|---|
| **(none)** | New entry: allocate record, insert into `ctx+288` at bucket `hash & mask`, push into `pos_symbols`. `sym_index >= 0`. [`sub_440BE0` L194-213] | New entry: same allocation path, push into `neg_symbols`. `sym_index < 0`. [`sub_440BE0` L219-224] | New entry: same allocation path, push into `pos_symbols`. `sym_index >= 0`. [`sub_440BE0` L225-230] |
| **`STB_LOCAL`** | If existing `name_str == NULL`: treat as fresh, allocate new entry. Otherwise: copy `st_name`/`name_str` from existing, re-probe hash map, fall through to populate. [L174-180] | Copy `st_name`/`name_str` from existing, re-probe hash map, push new global into `neg_symbols`, update entry pointer with new index. [L162-168] | Copy `st_name`/`name_str`, re-probe, push new weak into `pos_symbols`, update entry. [L171-173] |
| **`STB_GLOBAL`** (strong def, `st_shndx != 0`) | Should not reach here (locals do not collide with globals on the same name in a well-formed input). Action: copy name, re-probe, push into `pos_symbols`. [L171-180] | **Fatal**: `"adding global symbols of same name"` via `sub_467460` at L164-165. Detected by `(existing.st_info >> 4) == 1`. | Merge level: existing strong wins, incoming weak ignored. The `sub_442820` helper detects this and returns without modification. [see [Weak Symbol Handling](weak-symbols.md)] |
| **`STB_GLOBAL`** (UND, `st_shndx == 0`) | Not applicable (locals cannot fill UND). | **Upgrade in place**: the existing UND slot in `neg_symbols` is updated with the incoming definition's `st_shndx`, `st_value`, `st_size`, `st_info`. Hash map entry pointer unchanged. [short-circuit at `sub_442CA0` L70] | Same upgrade-in-place path; the weak definition fills the UND slot. DCE will not remove it because the hash map entry still points to it. |
| **`STB_WEAK`** | Local replacing weak is not a standard case; the merge loop at `sub_45E7D0` rejects this combination. | **Replace weak**: `sub_442820` emits `"replace weak function %s"`, zeroes the old weak record at its positive index, pushes new strong into `neg_symbols`, updates the hash map entry to point to the new negative index. Runs four cleanup passes. | Weak-vs-weak tie-breaking: `sub_442820` selects the definition with fewer registers (`"replace weak function %s with weak that uses fewer registers"`), or falls back to newer PTX version (`"replace weak function %s with weak from newer PTX"`). See [Weak Symbol Handling](weak-symbols.md) for the full priority order. |

The matrix uses three conceptual layers:

1. **Low-level insertion** (`sub_440BE0` / `sub_442CA0`): handles the hash map / array insertion mechanics and the `"adding global symbols of same name"` fatal check.
2. **Short-circuit for existing entries** (`sub_442CA0` line 70 `goto LABEL_26`): when the hash map already has an entry with a non-zero `sym_index`, the function delegates to `sub_442820` without allocating a new record.
3. **Merge arbitration** (`sub_442820` = `elfw_merge_symbols`): runs the weak/strong/UND resolution policy, performs cleanup when a weak definition is evicted, and updates the hash map entry pointer to the winning symbol.

The call flow is `merge loop -> sub_442CA0 -> (hash probe) -> either (new insertion path) or (LABEL_26 -> sub_442820 -> conflict resolution)`.

## Cross-References

- [Symbol Resolution](symbol-resolution.md) — storage scheme that this walkthrough exercises
- [Symbol Addition](symbol-addition.md) — detailed phase analysis of `sub_440BE0` and `sub_442CA0`
- [Extended Symbol Resolution](extended-symbol-resolution.md) — the `sub_4411F0` resolver invoked after merge
- [Weak Symbol Handling](weak-symbols.md) — `sub_442820` merge arbitration driving Steps 6 and 7
- [Hash Tables](hash-tables.md) — MurmurHash3 implementation and bucket layout
- [Merge Phase](../pipeline/merge.md) — per-object dispatch that drives the example
