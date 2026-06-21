# PTX Lexer Token Table (178 entries)

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base `0x400000` (non-PIE).*

The PTX front-end token identifiers. `token_id` is the lexer's internal token value (decimal and
hex). `kind` distinguishes a single keyword/symbol (`single`) from a `modifier-group(N)` — a token
id that the grammar accepts as any of N spellings (e.g. the integer-literal group). Angle-bracket
entries (`<identifier>`, `<int-literal>`) are token *classes*, not literal keywords.

| token_id (dec) | token_id (hex) | keyword(s) / group members | kind |
|---|---|---|---|
| 258 | 0x102 | `<identifier>` | single |
| 259 | 0x103 | `<dotted-identifier>` | single |
| 260 | 0x104 | `call` | single |
| 261 | 0x105 | `` | single |
| 262 | 0x106 | `<int-literal>\|<hex-int-literal>\|<bin-int-literal>\|<int-literal>` | modifier-group(4) |
| 264 | 0x108 | `WARP_SZ` | single |
| 265 | 0x109 | `<<` | single |
| 266 | 0x10a | `>>` | single |
| 267 | 0x10b | `==` | single |
| 268 | 0x10c | `!=` | single |
| 269 | 0x10d | `<=` | single |
| 270 | 0x10e | `>=` | single |
| 271 | 0x10f | `.extern` | single |
| 272 | 0x110 | `.visible` | single |
| 273 | 0x111 | `.weak` | single |
| 274 | 0x112 | `.common` | single |
| 275 | 0x113 | `.s8\|.s16\|.s32\|.s64\|.s16x2\|.u8\|.u16\|.u32\|.u64\|.u16x2\|.e4m3\|.e5m2\|.e4m3x2\|.e5m2x2\|.e4m3x4\|.e5m2x4\|.e2m1x2\|.e2m1x4\|.e2m3x2\|.e3m2x2\|.e2m3x4\|.e3m2x4\|.ue8m0x2\|.ue8m0x4\|.f16\|.f16x2\|.f32\|.f32x2\|.f64\|.b8\|.b16\|.b32\|.b64\|.b128\|.pred\|.texref\|.samplerref\|.surfref` | modifier-group(38) |
| 276 | 0x114 | `.entry` | single |
| 277 | 0x115 | `.FORCE_INLINE` | single |
| 278 | 0x116 | `.proto` | single |
| 279 | 0x117 | `.maxnreg` | single |
| 280 | 0x118 | `.maxntid` | single |
| 281 | 0x119 | `.maxnctapersm` | single |
| 282 | 0x11a | `.minnctapersm` | single |
| 283 | 0x11b | `.reqntid` | single |
| 284 | 0x11c | `.reqnctapercluster` | single |
| 285 | 0x11d | `.explicitcluster` | single |
| 286 | 0x11e | `.maxclusterrank` | single |
| 287 | 0x11f | `.blocksareclusters` | single |
| 288 | 0x120 | `.rn\|.rna\|.rm\|.rp\|.rz\|.rs\|.rni\|.rmi\|.rpi\|.rzi` | modifier-group(10) |
| 289 | 0x121 | `.finite\|.infinite\|.number\|.notanumber\|.normal\|.subnormal` | modifier-group(6) |
| 290 | 0x122 | `.ca\|.cg\|.cs\|.lu\|.cv\|.wb\|.wt\|.inv\|.invall` | modifier-group(9) |
| 291 | 0x123 | `.L1\|.L2\|.tensormap` | modifier-group(3) |
| 292 | 0x124 | `.clamp\|.wrap\|.trap\|.zero` | modifier-group(4) |
| 293 | 0x125 | `.shr7\|.shr15` | modifier-group(2) |
| 294 | 0x126 | `.po` | single |
| 295 | 0x127 | `.f4e\|.b4e\|.rc8\|.ecl\|.ecr\|.rc16` | modifier-group(6) |
| 296 | 0x128 | `.up\|.bfly\|.idx` | modifier-group(3) |
| 297 | 0x129 | `.en\|.dis` | modifier-group(2) |
| 298 | 0x12a | `.rand` | single |
| 299 | 0x12b | `.footprint` | single |
| 300 | 0x12c | `.coarse` | single |
| 301 | 0x12d | `.reg` | single |
| 302 | 0x12e | `.const\|.const[0]\|.const[1]\|.const[10]` | modifier-group(3) |
| 303 | 0x12f | `.global` | single |
| 304 | 0x130 | `.local` | single |
| 305 | 0x131 | `.param` | single |
| 306 | 0x132 | `.shared` | single |
| 307 | 0x133 | `.tex` | single |
| 308 | 0x134 | `.1d_buffer\|.a1d\|.a2d\|.cube\|.acube\|.2dms\|.a2dms` | modifier-group(7) |
| 309 | 0x135 | `.width\|.height\|.depth\|.channel_data_type\|.channel_order\|.normalized_coords\|.filter_mode\|.addr_mode_0\|.addr_mode_1\|.addr_mode_2\|.force_unnormalized_coords\|.array_size\|.num_mipmap_levels\|.num_samples\|.memory_layout` | modifier-group(15) |
| 310 | 0x136 | `.1d\|.2d\|.3d\|.4d\|.5d` | modifier-group(5) |
| 311 | 0x137 | `.shared::cta` | single |
| 312 | 0x138 | `.shared::cluster` | single |
| 313 | 0x139 | `.param::entry` | single |
| 314 | 0x13a | `.param::func` | single |
| 315 | 0x13b | `0F00000000` | single |
| 316 | 0x13c | `<float-literal>\|<float-literal>\|<float-literal>\|0D0000000000000000` | modifier-group(4) |
| 317 | 0x13d | `&&` | single |
| 318 | 0x13e | `\|\|` | single(indexed/multi-literal) |
| 319 | 0x13f | `.ptr` | single |
| 320 | 0x140 | `.eq\|.ne\|.lt\|.le\|.gt\|.ge\|.lo\|.ls\|.hi\|.hs\|.num\|.nan\|.equ\|.neu\|.ltu\|.leu\|.gtu\|.geu` | modifier-group(18) |
| 322 | 0x142 | `.and\|.or\|.xor` | modifier-group(3) |
| 323 | 0x143 | `.cas\|.exch\|.inc\|.dec\|.safeadd` | modifier-group(5) |
| 324 | 0x144 | `.add\|.min\|.max\|.maxabs\|.popc` | modifier-group(5) |
| 325 | 0x145 | `.uni` | single |
| 326 | 0x146 | `.unanimous\|.conv\|.div` | modifier-group(3) |
| 327 | 0x147 | `.sync` | single |
| 328 | 0x148 | `.aligned` | single |
| 329 | 0x149 | `.all\|.any` | modifier-group(2) |
| 330 | 0x14a | `.dual` | single |
| 331 | 0x14b | `.close` | single |
| 332 | 0x14c | `_` | single |
| 333 | 0x14d | `.func` | single |
| 334 | 0x14e | `.align` | single |
| 335 | 0x14f | `.allocno` | single |
| 336 | 0x150 | `.retaddr_allocno` | single |
| 337 | 0x151 | `.cta\|.cluster\|.gl\|.gpu\|.sys` | modifier-group(5) |
| 338 | 0x152 | `.v2\|.v4` | modifier-group(2) |
| 339 | 0x153 | `.version` | 0.0 |
| 340 | 0x154 | `.target` | single |
| 341 | 0x155 | `.address_size` | single |
| 342 | 0x156 | `.scratch` | single |
| 343 | 0x157 | `@@DWARF` | single |
| 344 | 0x158 | `.section` | single |
| 345 | 0x159 | `.file` | single |
| 346 | 0x15a | `.loc` | single |
| 347 | 0x15b | `.pragma` | single |
| 348 | 0x15c | `@progbits` | single |
| 349 | 0x15d | `.alias` | single |
| 350 | 0x15e | `inlined_at` | single |
| 351 | 0x15f | `function_name` | single |
| 352 | 0x160 | `.ballot` | single |
| 353 | 0x161 | `.approx` | single |
| 354 | 0x162 | `.relu` | single |
| 355 | 0x163 | `.ftz` | single |
| 356 | 0x164 | `.noftz` | single |
| 357 | 0x165 | `.sat` | single |
| 358 | 0x166 | `.satfinite` | single |
| 359 | 0x167 | `.cc` | single |
| 360 | 0x168 | `.shiftamt` | single |
| 361 | 0x169 | `.volatile\|.relaxed\|.acquire\|.release\|.acq_rel\|.sc` | modifier-group(6) |
| 362 | 0x16a | `.mmio` | single |
| 363 | 0x16b | `.nc` | single |
| 364 | 0x16c | `.MACRO` | single |
| 365 | 0x16d | `.NaN` | single |
| 366 | 0x16e | `.bulk_group\|.mbarrier\|.mbarrier::arrive::one\|.mbarrier::complete_tx::bytes\|.mbarrier::meet_tx::bytes` | modifier-group(5) |
| 367 | 0x16f | `.down` | single |
| 368 | 0x170 | `.no_membermask_overlap` | single |
| 369 | 0x171 | `.branchtargets` | single |
| 370 | 0x172 | `.calltargets` | single |
| 371 | 0x173 | `.callprototype` | single |
| 372 | 0x174 | `.attribute` | single |
| 373 | 0x175 | `.managed` | single |
| 374 | 0x176 | `.noreturn` | single |
| 375 | 0x177 | `.unique` | single |
| 376 | 0x178 | `.local_maxnreg` | single |
| 377 | 0x179 | `.hidden` | single |
| 378 | 0x17a | `.abi_preserve` | single |
| 379 | 0x17b | `.abi_preserve_control` | single |
| 380 | 0x17c | `.abi_preserve_after` | single |
| 381 | 0x17d | `.unified` | single |
| 382 | 0x17e | `.reserved` | single |
| 383 | 0x17f | `.metadata_section` | single |
| 384 | 0x180 | `.metadata` | single |
| 385 | 0x181 | `.metadata_index` | single |
| 386 | 0x182 | `.m8n8k4\|.m8n8k16\|.m8n8k32\|.m16n16k8\|.m16n16k16\|.m32n8k16\|.m8n32k16\|.m16n8k8\|.m16n8k16\|.m16n8k32\|.m16n8k4\|.m16n8k64\|.m16n8k128\|.m16n8k256\|.m8n8k128\|.m8n8\|.m8n16\|.m8n32\|.m8n64\|.m16n8\|.m16n16\|.m8n8k64\|.m64n8k32\|.m64n16k32\|.m64n24k32\|.m64n32k32\|.m64n40k32\|.m64n48k32\|.m64n56k32\|.m64n64k32\|.m64n72k32\|.m64n80k32\|.m64n88k32\|.m64n96k32\|.m64n104k32\|.m64n112k32\|.m64n120k32\|.m64n128k32\|.m64n136k32\|.m64n144k32\|.m64n152k32\|.m64n160k32\|.m64n168k32\|.m64n176k32\|.m64n184k32\|.m64n192k32\|.m64n200k32\|.m64n208k32\|.m64n216k32\|.m64n224k32\|.m64n232k32\|.m64n240k32\|.m64n248k32\|.m64n256k32\|.m64n8k16\|.m64n16k16\|.m64n24k16\|.m64n32k16\|.m64n40k16\|.m64n48k16\|.m64n56k16\|.m64n64k16\|.m64n72k16\|.m64n80k16\|.m64n88k16\|.m64n96k16\|.m64n104k16\|.m64n112k16\|.m64n120k16\|.m64n128k16\|.m64n136k16\|.m64n144k16\|.m64n152k16\|.m64n160k16\|.m64n168k16\|.m64n176k16\|.m64n184k16\|.m64n192k16\|.m64n200k16\|.m64n208k16\|.m64n216k16\|.m64n224k16\|.m64n232k16\|.m64n240k16\|.m64n248k16\|.m64n256k16\|.m64n8k8\|.m64n16k8\|.m64n24k8\|.m64n32k8\|.m64n40k8\|.m64n48k8\|.m64n56k8\|.m64n64k8\|.m64n72k8\|.m64n80k8\|.m64n88k8\|.m64n96k8\|.m64n104k8\|.m64n112k8\|.m64n120k8\|.m64n128k8\|.m64n136k8\|.m64n144k8\|.m64n152k8\|.m64n160k8\|.m64n168k8\|.m64n176k8\|.m64n184k8\|.m64n192k8\|.m64n200k8\|.m64n208k8\|.m64n216k8\|.m64n224k8\|.m64n232k8\|.m64n240k8\|.m64n248k8\|.m64n256k8\|.m64n8k64\|.m64n16k64\|.m64n24k64\|.m64n32k64\|.m64n40k64\|.m64n48k64\|.m64n56k64\|.m64n64k64\|.m64n72k64\|.m64n80k64\|.m64n88k64\|.m64n96k64\|.m64n104k64\|.m64n112k64\|.m64n120k64\|.m64n128k64\|.m64n136k64\|.m64n144k64\|.m64n152k64\|.m64n160k64\|.m64n168k64\|.m64n176k64\|.m64n184k64\|.m64n192k64\|.m64n200k64\|.m64n208k64\|.m64n216k64\|.m64n224k64\|.m64n232k64\|.m64n240k64\|.m64n248k64\|.m64n256k64\|.m64n8k256\|.m64n16k256\|.m64n24k256\|.m64n32k256\|.m64n48k256\|.m64n64k256\|.m64n80k256\|.m64n96k256\|.m64n112k256\|.m64n128k256\|.m64n144k256\|.m64n160k256\|.m64n176k256\|.m64n192k256\|.m64n208k256\|.m64n224k256\|.m64n240k256\|.m64n256k256\|.4x256b\|.16x32bx2\|.16x64b\|.16x128b\|.16x256b\|.32x32b\|.32x128b\|.64x128b\|.128x128b\|.128x256b` | modifier-group(178) |
| 387 | 0x183 | `.row\|.col` | modifier-group(2) |
| 388 | 0x184 | `.L2::64B\|.L2::128B\|.L2::256B` | modifier-group(3) |
| 389 | 0x185 | `.64B\|.128B\|.256B` | modifier-group(3) |
| 390 | 0x186 | `.exclusive` | single |
| 391 | 0x187 | `.transA` | single |
| 392 | 0x188 | `.negA` | single |
| 393 | 0x189 | `.transB` | single |
| 394 | 0x18a | `.negB` | single |
| 395 | 0x18b | `.ignoreC` | single |
| 396 | 0x18c | `.ignoreC_pred` | single |
| 397 | 0x18d | `.L2::evict_first\|.L2::evict_last\|.L2::evict_unchanged\|.L2::evict_normal\|.L2::no_allocate\|.L1::evict_first\|.L1::evict_last\|.L1::evict_unchanged\|.L1::evict_normal\|.L1::no_allocate` | modifier-group(10) |
| 399 | 0x18f | `.mbarrier_init` | single |
| 400 | 0x190 | `.sync_restrict::shared::cluster\|.sync_restrict::shared::cta` | modifier-group(2) |
| 401 | 0x191 | `.x1\|.x2\|.x4\|.x8\|.x16\|.x32\|.x64\|.x128` | modifier-group(8) |
| 402 | 0x192 | `.trans` | single |
| 403 | 0x193 | `.thread\|.pair\|.quad` | modifier-group(3) |
| 404 | 0x194 | `.s2\|.s4\|.u2\|.u4\|.bf16\|.bf16x2` | modifier-group(6) |
| 405 | 0x195 | `.lower::16b` | single |
| 406 | 0x196 | `.expand` | single |
| 407 | 0x197 | `.sp\|.sp::ordered_metadata` | modifier-group(2) |
| 408 | 0x198 | `.seq` | single |
| 409 | 0x199 | `.1g\|.2g\|.4g` | modifier-group(3) |
| 410 | 0x19a | `.noComplete` | single |
| 411 | 0x19b | `.noinc` | single |
| 412 | 0x19c | `.abs` | single |
| 413 | 0x19d | `.L2::cache_hint` | single |
| 414 | 0x19e | `.multicast::cluster\|.multicast::cluster::all` | modifier-group(2) |
| 415 | 0x19f | `.asc::b32\|.asc::b64` | modifier-group(2) |
| 416 | 0x1a0 | `.acc::f16\|.acc::f32` | modifier-group(2) |
| 417 | 0x1a1 | `.global_address\|.rank\|.box_dim\|.global_dim\|.global_stride\|.element_stride\|.elemtype\|.interleave_layout\|.swizzle_mode\|.swizzle_atomicity\|.fill_mode` | modifier-group(11) |
| 418 | 0x1a2 | `.b1024` | single |
| 419 | 0x1a3 | `.async::generic\|.tensormap::generic` | modifier-group(2) |
| 420 | 0x1a4 | `::before_thread_sync\|::after_thread_sync` | modifier-group(2) |
| 421 | 0x1a5 | `::ld\|::st` | modifier-group(2) |
| 422 | 0x1a6 | `.block16\|.block32` | modifier-group(2) |
|  |  | `` | no-token-rule(action 0) |
|  |  | `.DEFINE` | A |
|  |  | `.IF` | no-token-rule(action 3) |
|  |  | `.byte` | no-token-rule(action 287) |
|  |  | `.4byte` | no-token-rule(action 288) |
|  |  | `<#line-marker>` | no-token-rule(action 540) |
|  |  | `` |  |
|  |  | `/*` | no-token-rule(action 544) |
|  |  | `//` | no-token-rule(action 545) |
|  |  | `` |  |
|  | no-token-rule(action 546) | `` |  |
|  |  | `` | no-token-rule(action 547) |
|  |  | `<single-char-punct>` | no-token-rule(action 548) |
|  |  | `` | no-token-rule(action 549) |
|  |  | `` | no-token-rule(action 550) |
|  |  | `` | no-token-rule(action 551) |
