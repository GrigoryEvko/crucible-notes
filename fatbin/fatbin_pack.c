/*
 * NVIDIA Fat Binary Packer - nvFatbin API Implementation
 * Reverse-engineered from libnvfatbin.so.13 (CUDA 13.x)
 * Copyright (c) 2025 - Licensed under MIT License
 */
#define _GNU_SOURCE

#include "nvFatbin.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <zstd.h>
typedef struct __attribute__((packed)) {
    uint16_t type;
    uint16_t unknown_02;
    uint32_t size;
    uint32_t aligned_data_size;
    uint32_t padding;
    uint32_t data_size;
    uint32_t unknown_14;
    uint32_t minor_version;
    uint32_t sm_arch;
    uint32_t reloc_offset;
    uint32_t reloc_size;
    uint64_t flags;
    uint8_t  reserved[16];
} EntryHeader;

typedef struct EntryNode {
    EntryHeader header;
    uint8_t *extended_header;
    void *compressed_data;
    size_t compressed_size;
    void *original_data;
    size_t original_size;
    char *identifier;
    char *arch_string;
    struct EntryNode *next;
} EntryNode;

typedef struct nvFatbinHandle_impl {
    uint32_t magic;
    uint16_t version_low;
    uint16_t version_high;
    uint64_t header_size;
    EntryNode *entries;
    int entry_count;
    int compress_enabled;
    int compress_all;
    int compress_mode;  /* 0=default, 1=size, 2=speed, 3=balance, 4=none */
    int compress_level; /* 1-22 or 0 for mode defaults */
    int is_64bit;
    int is_cuda;
    int has_calculated_size;
    size_t total_size;
    uint8_t padding[76];
} nvFatbinHandle_impl;

_Static_assert(sizeof(nvFatbinHandle_impl) == 144, "Handle size must be 144 bytes");
_Static_assert(sizeof(EntryHeader) == 64, "Entry header must be 64 bytes");
_Static_assert(offsetof(EntryHeader, type) == 0, "type offset must be 0");
_Static_assert(offsetof(EntryHeader, unknown_02) == 2, "unknown_02 offset must be 2");
_Static_assert(offsetof(EntryHeader, size) == 4, "size offset must be 4");
_Static_assert(offsetof(EntryHeader, aligned_data_size) == 8, "aligned_data_size offset must be 8");
_Static_assert(offsetof(EntryHeader, padding) == 12, "padding offset must be 12");
_Static_assert(offsetof(EntryHeader, data_size) == 16, "data_size offset must be 16");
_Static_assert(offsetof(EntryHeader, sm_arch) == 28, "sm_arch offset must be 28");
_Static_assert(offsetof(EntryHeader, flags) == 40, "flags offset must be 40");
_Static_assert(offsetof(EntryHeader, reserved) == 48, "reserved offset must be 48");

#define FATBIN_MAGIC_EXECUTABLE  0xBA55ED50
#define FATBIN_MAGIC_RELOCATABLE 0x466243B1
#define FATBIN_VERSION_LOW       0x0001
#define FATBIN_VERSION_HIGH      0x0010
#define FATBIN_ENTRY_SIZE        64
#define FATBIN_HEADER_SIZE       16

#define ELF_MAGIC                0x464C457F
#define ELF_MACHINE_NVIDIA       0xBE
#define LLVM_BITCODE_WRAPPER_MAGIC  0xDEC04217
#define LLVM_BITCODE_RAW_MAGIC   0x4342
#define ZSTD_MAGIC               0xFD2FB528
#define COMPRESSION_THRESHOLD    1024
#define ALIGN_8(size)            (((size_t)(size) + 7) & ~7UL)
#define ZSTD_LEVEL_DEFAULT       3
#define ZSTD_LEVEL_SIZE          9
#define ZSTD_LEVEL_SPEED         1
#define ZSTD_LEVEL_BALANCE       5
#define ENTRY_TYPE_PTX           0x01
#define ENTRY_TYPE_ELF           0x02
#define ENTRY_TYPE_LTOIR         0x04
#define ENTRY_TYPE_ELF_ALT       0x10

static int is_valid_arch(uint32_t arch);
static uint32_t parse_arch_string(const char *arch);
static nvFatbinResult compress_cubin_data(nvFatbinHandle_impl *impl, EntryNode *entry);
static void free_entry_node(EntryNode *entry);

const char *nvFatbinGetErrorString(nvFatbinResult result)
{
    switch (result) {
        case NVFATBIN_SUCCESS:
            return NULL;
        case NVFATBIN_ERROR_INTERNAL:
            return "Internal error";
        case NVFATBIN_ERROR_ELF_ARCH_MISMATCH:
            return "ELF architecture mismatch";
        case NVFATBIN_ERROR_ELF_SIZE_MISMATCH:
            return "ELF size mismatch";
        case NVFATBIN_ERROR_MISSING_PTX_VERSION:
            return "Missing PTX version directive";
        case NVFATBIN_ERROR_NULL_POINTER:
            return "Null pointer provided";
        case NVFATBIN_ERROR_COMPRESSION_FAILED:
            return "Compression failed";
        case NVFATBIN_ERROR_COMPRESSED_SIZE_EXCEEDED:
            return "Compressed size exceeded";
        case NVFATBIN_ERROR_UNRECOGNIZED_OPTION:
            return "Unrecognized option";
        case NVFATBIN_ERROR_INVALID_ARCH:
            return "Invalid architecture";
        case NVFATBIN_ERROR_INVALID_NVVM:
            return "Invalid NVVM";
        case NVFATBIN_ERROR_EMPTY_INPUT:
            return "Empty input";
        case NVFATBIN_ERROR_MISSING_PTX_ARCH:
            return "Missing PTX architecture";
        case NVFATBIN_ERROR_PTX_ARCH_MISMATCH:
            return "PTX architecture mismatch";
        case NVFATBIN_ERROR_MISSING_FATBIN:
            return "Missing fatbin";
        case NVFATBIN_ERROR_INVALID_INDEX:
            return "Invalid index";
        case NVFATBIN_ERROR_IDENTIFIER_REUSE:
            return "Identifier reuse";
        case NVFATBIN_ERROR_INTERNAL_PTX_OPTION:
            return "Internal PTX option error";
        default:
            return "Unknown error";
    }
}

nvFatbinResult nvFatbinVersion(unsigned int *major, unsigned int *minor)
{
    if (!major || !minor) {
        return NVFATBIN_ERROR_NULL_POINTER;
    }
    *major = 13;
    *minor = 0;
    return NVFATBIN_SUCCESS;
}

static int is_valid_arch(uint32_t arch)
{
    switch (arch) {
        case 50: case 52: case 53:
        case 60: case 61: case 62:
        case 70: case 72: case 75:
        case 80: case 86: case 87:
        case 89: case 90:
        case 100: case 120:
            return 1;
        default:
            if (arch >= 50 && arch <= 200) {
                return 1;
            }
            return 0;
    }
}

static uint32_t parse_arch_string(const char *arch)
{
    if (!arch) {
        return 0;
    }
    if (strncmp(arch, "sm_", 3) == 0) {
        return (uint32_t)atoi(arch + 3);
    }
    else if (strncmp(arch, "compute_", 8) == 0) {
        return (uint32_t)atoi(arch + 8);
    }
    else if (strncmp(arch, "lto_", 4) == 0) {
        return (uint32_t)atoi(arch + 4);
    }
    if (arch[0] >= '0' && arch[0] <= '9') {
        return (uint32_t)atoi(arch);
    }
    return 0;
}

static nvFatbinResult compress_cubin_data(nvFatbinHandle_impl *impl, EntryNode *entry)
{
    int should_compress = 0;
    if (impl->compress_mode == 4) {
        should_compress = 0;
    }
    else if (impl->compress_all) {
        should_compress = 1;
    }
    else {
        should_compress = (entry->original_size > COMPRESSION_THRESHOLD);
    }

    if (!should_compress) {
        entry->compressed_data = entry->original_data;
        entry->compressed_size = entry->original_size;
        entry->original_data = NULL;
        entry->header.data_size = (uint32_t)entry->compressed_size;
        entry->header.aligned_data_size = (uint32_t)ALIGN_8(entry->compressed_size);
        entry->header.padding = 0;
        entry->header.flags &= ~0x8000;  /* Clear compression flag (cuobjdump checks bit 15) */
        return NVFATBIN_SUCCESS;
    }

    size_t max_compressed = ZSTD_compressBound(entry->original_size);
    entry->compressed_data = malloc(max_compressed);
    if (!entry->compressed_data) {
        return NVFATBIN_ERROR_INTERNAL;
    }

    int zstd_level;
    if (impl->compress_level > 0) {
        zstd_level = impl->compress_level;
    } else {
        switch (impl->compress_mode) {
            case 0:  zstd_level = ZSTD_LEVEL_DEFAULT; break;
            case 1:  zstd_level = ZSTD_LEVEL_SIZE; break;
            case 2:  zstd_level = ZSTD_LEVEL_SPEED; break;
            case 3:  zstd_level = ZSTD_LEVEL_BALANCE; break;
            default: zstd_level = ZSTD_LEVEL_DEFAULT; break;
        }
    }

    size_t compressed_size;
    if (zstd_level > 19) {
        /* Ultra compression levels (20-22) require advanced API */
        ZSTD_CCtx* cctx = ZSTD_createCCtx();
        if (!cctx) {
            free(entry->compressed_data);
            entry->compressed_data = NULL;
            return NVFATBIN_ERROR_COMPRESSION_FAILED;
        }
        ZSTD_CCtx_setParameter(cctx, ZSTD_c_compressionLevel, zstd_level);
        compressed_size = ZSTD_compress2(cctx,
            entry->compressed_data,
            max_compressed,
            entry->original_data,
            entry->original_size);
        ZSTD_freeCCtx(cctx);
    } else {
        /* Standard compression levels (1-19) */
        compressed_size = ZSTD_compress(
            entry->compressed_data,
            max_compressed,
            entry->original_data,
            entry->original_size,
            zstd_level
        );
    }

    if (ZSTD_isError(compressed_size)) {
        free(entry->compressed_data);
        entry->compressed_data = NULL;
        return NVFATBIN_ERROR_COMPRESSION_FAILED;
    }
    if (compressed_size >= entry->original_size && !impl->compress_all) {
        free(entry->compressed_data);
        entry->compressed_data = entry->original_data;
        entry->compressed_size = entry->original_size;
        entry->original_data = NULL;
        entry->header.flags &= ~0x8000;
        entry->header.data_size = (uint32_t)entry->compressed_size;
        entry->header.aligned_data_size = (uint32_t)ALIGN_8(entry->compressed_size);
        entry->header.padding = 0;
    }
    else {
        entry->compressed_size = compressed_size;
        uint8_t *resized = (uint8_t *)realloc(entry->compressed_data, compressed_size);
        if (resized != NULL) {
            entry->compressed_data = resized;
        }
        free(entry->original_data);
        entry->original_data = NULL;
    }

    entry->header.data_size = (uint32_t)entry->compressed_size;
    entry->header.aligned_data_size = (uint32_t)ALIGN_8(entry->compressed_size);
    entry->header.padding = 0;

    if (entry->compressed_size > 0xFFFFFFFFULL) {
        return NVFATBIN_ERROR_COMPRESSED_SIZE_EXCEEDED;
    }

    return NVFATBIN_SUCCESS;
}

static void free_entry_node(EntryNode *entry)
{
    if (!entry) {
        return;
    }
    free(entry->extended_header);
    free(entry->compressed_data);
    free(entry->original_data);
    free(entry->identifier);
    free(entry->arch_string);
    free(entry);
}

nvFatbinResult nvFatbinCreate(nvFatbinHandle *handle_indirect,
                               const char **options,
                               size_t optionsCount)
{
    if (!handle_indirect) {
        return NVFATBIN_ERROR_NULL_POINTER;
    }

    nvFatbinHandle_impl *impl = calloc(1, sizeof(nvFatbinHandle_impl));
    if (!impl) {
        return NVFATBIN_ERROR_INTERNAL;
    }

    impl->magic = FATBIN_MAGIC_EXECUTABLE;
    impl->version_low = FATBIN_VERSION_LOW;
    impl->version_high = FATBIN_VERSION_HIGH;
    impl->header_size = 0;
    impl->entries = NULL;
    impl->entry_count = 0;
    impl->has_calculated_size = 0;
    impl->compress_enabled = 1;
    impl->compress_all = 0;
    impl->compress_mode = 0;
    impl->compress_level = 0;
    impl->is_64bit = 1;
    impl->is_cuda = 1;
    for (size_t i = 0; i < optionsCount; i++) {
        const char *opt = options[i];

        if (strcmp(opt, "-32") == 0) {
            impl->is_64bit = 0;
        }
        else if (strcmp(opt, "-64") == 0) {
            impl->is_64bit = 1;
        }
        else if (strcmp(opt, "-compress=true") == 0) {
            impl->compress_enabled = 1;
        }
        else if (strcmp(opt, "-compress=false") == 0) {
            impl->compress_enabled = 0;
        }
        else if (strcmp(opt, "-compress-all") == 0) {
            impl->compress_all = 1;
        }
        else if (strncmp(opt, "-compress-mode=", 15) == 0) {
            const char *mode = opt + 15;
            if (strcmp(mode, "default") == 0) {
                impl->compress_mode = 0;
            }
            else if (strcmp(mode, "size") == 0) {
                impl->compress_mode = 1;
            }
            else if (strcmp(mode, "speed") == 0) {
                impl->compress_mode = 2;
            }
            else if (strcmp(mode, "balance") == 0) {
                impl->compress_mode = 3;
            }
            else if (strcmp(mode, "none") == 0) {
                impl->compress_enabled = 0;
                impl->compress_mode = 4;
            }
            else {
                free(impl);
                return NVFATBIN_ERROR_UNRECOGNIZED_OPTION;
            }
        }
        else if (strcmp(opt, "-cuda") == 0) {
            impl->is_cuda = 1;
        }
        else if (strcmp(opt, "-opencl") == 0) {
            impl->is_cuda = 0;
        }
        else if (strcmp(opt, "-g") == 0) {
        }
        else if (strcmp(opt, "-c") == 0) {
        }
        else if (strncmp(opt, "-host=", 6) == 0) {
        }
        else if (strncmp(opt, "-compress-level=", 16) == 0) {
            const char *level_str = opt + 16;
            int level = atoi(level_str);
            if (level >= 1 && level <= 22) {
                impl->compress_level = level;
            } else {
                free(impl);
                return NVFATBIN_ERROR_UNRECOGNIZED_OPTION;
            }
        }
        else {
            free(impl);
            return NVFATBIN_ERROR_UNRECOGNIZED_OPTION;
        }
    }

    *handle_indirect = (nvFatbinHandle)impl;
    return NVFATBIN_SUCCESS;
}

nvFatbinResult nvFatbinDestroy(nvFatbinHandle *handle_indirect)
{
    if (!handle_indirect) {
        return NVFATBIN_ERROR_NULL_POINTER;
    }

    nvFatbinHandle_impl *impl = (nvFatbinHandle_impl *)*handle_indirect;
    if (!impl) {
        return NVFATBIN_ERROR_NULL_POINTER;
    }

    EntryNode *entry = impl->entries;
    while (entry) {
        EntryNode *next = entry->next;
        free_entry_node(entry);
        entry = next;
    }

    free(impl);
    *handle_indirect = NULL;

    return NVFATBIN_SUCCESS;
}

nvFatbinResult nvFatbinAddCubin(nvFatbinHandle handle,
                                 const void *code,
                                 size_t size,
                                 const char *arch,
                                 const char *identifier)
{
    nvFatbinHandle_impl *impl = (nvFatbinHandle_impl *)handle;
    if (!handle || !code || !arch) {
        return NVFATBIN_ERROR_NULL_POINTER;
    }

    if (size == 0) {
        return NVFATBIN_ERROR_EMPTY_INPUT;
    }

    if (size < 4) {
        return NVFATBIN_ERROR_ELF_SIZE_MISMATCH;
    }

    uint32_t elf_magic = *(uint32_t *)code;
    if (elf_magic != ELF_MAGIC) {
        return NVFATBIN_ERROR_INTERNAL;
    }

    uint32_t sm_arch = parse_arch_string(arch);
    if (sm_arch == 0 || !is_valid_arch(sm_arch)) {
        return NVFATBIN_ERROR_INVALID_ARCH;
    }

    if (size >= 0x14) {
        uint16_t e_machine = *(uint16_t *)((uint8_t *)code + 0x12);
        if (e_machine != ELF_MACHINE_NVIDIA) {
            return NVFATBIN_ERROR_ELF_ARCH_MISMATCH;
        }
    }

    EntryNode *entry = calloc(1, sizeof(EntryNode));
    if (!entry) {
        return NVFATBIN_ERROR_INTERNAL;
    }

    entry->header.type = ENTRY_TYPE_ELF;
    entry->header.unknown_02 = 0x0101;
    if (sm_arch >= 100) {
        entry->header.size = 112;
        entry->header.unknown_14 = 0x40;
    } else {
        entry->header.size = 64;
        entry->header.unknown_14 = 0x00;
    }

    entry->header.sm_arch = sm_arch;
    entry->header.minor_version = 0x00010008;
    entry->header.reloc_offset = 0;
    entry->header.reloc_size = 0;
    entry->header.flags = 0x8011;
    memset(entry->header.reserved, 0, sizeof(entry->header.reserved));
    *(uint64_t*)(&entry->header.reserved[8]) = size;

    if (sm_arch >= 100) {
        entry->extended_header = (uint8_t*)calloc(48, 1);
        if (!entry->extended_header) {
            free(entry);
            return NVFATBIN_ERROR_INTERNAL;
        }
        *(uint32_t*)(entry->extended_header + 0) = 0x48;
        *(uint32_t*)(entry->extended_header + 4) = 0x24;
        static const uint8_t capability_data[40] = {
            0x00, 0x02, 0x09, 0x00, 0x00, 0x02, 0x02, 0x01,
            0x00, 0x02, 0x05, 0x05, 0x00, 0x03, 0x07, 0x01,
            0x01, 0x02, 0x03, 0x00, 0x00, 0x02, 0x06, 0x01,
            0x00, 0x04, 0x0b, 0x08, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
        };
        memcpy(entry->extended_header + 8, capability_data, 40);
    } else {
        entry->extended_header = NULL;
    }

    entry->original_data = malloc(size);
    if (!entry->original_data) {
        free(entry);
        return NVFATBIN_ERROR_INTERNAL;
    }
    memcpy(entry->original_data, code, size);
    entry->original_size = size;

    if (identifier) {
        entry->identifier = strdup(identifier);
    }
    entry->arch_string = strdup(arch);

    if (impl->compress_enabled) {
        nvFatbinResult result = compress_cubin_data(impl, entry);
        if (result != NVFATBIN_SUCCESS) {
            free_entry_node(entry);
            return result;
        }
    }
    else {
        entry->compressed_data = entry->original_data;
        entry->compressed_size = entry->original_size;
        entry->original_data = NULL;
        entry->header.data_size = (uint32_t)entry->compressed_size;
        entry->header.aligned_data_size = (uint32_t)ALIGN_8(entry->compressed_size);
        entry->header.padding = 0;
    }

    entry->next = NULL;
    if (!impl->entries) {
        impl->entries = entry;
    }
    else {
        EntryNode *last = impl->entries;
        while (last->next) {
            last = last->next;
        }
        last->next = entry;
    }
    impl->entry_count++;
    impl->has_calculated_size = 0;
    return NVFATBIN_SUCCESS;
}

static int contains_directive(const char *code, size_t size, const char *directive) {
    if (code == NULL || directive == NULL || size == 0) {
        return 0;
    }
    size_t directive_len = strlen(directive);
    if (directive_len == 0 || directive_len > size) {
        return 0;
    }
    for (size_t i = 0; i <= size - directive_len; i++) {
        if (strncmp(&code[i], directive, directive_len) == 0) {
            if (i > 0) {
                char prev = code[i - 1];
                if (prev != ' ' && prev != '\t' && prev != '\n' && prev != '\r') {
                    continue;
                }
            }
            return 1;
        }
    }
    return 0;
}

static uint32_t extract_ptx_arch(const char *code, size_t size) {
    if (code == NULL || size == 0) {
        return 0;
    }
    const char *target = ".target sm_";
    size_t target_len = strlen(target);
    for (size_t i = 0; i <= size - target_len - 2; i++) {
        if (strncmp(&code[i], target, target_len) == 0) {
            const char *arch_start = &code[i + target_len];
            uint32_t arch = 0;
            int digit_count = 0;
            for (size_t j = 0; j < 3 && (i + target_len + j) < size; j++) {
                char c = arch_start[j];
                if (c >= '0' && c <= '9') {
                    arch = arch * 10 + (c - '0');
                    digit_count++;
                } else {
                    if (c == ' ' || c == '\t' || c == '\n' || c == '\r' ||
                        c == ',' || c == ';' || c == '{') {
                        break;
                    }
                    arch = 0;
                    break;
                }
            }
            if (digit_count >= 2 && arch >= 10) {
                return arch;
            }
        }
    }
    return 0;
}

static nvFatbinResult validate_ptx(const char *code, size_t size,
                                    uint32_t expected_arch, uint32_t *extracted_arch) {
    if (!contains_directive(code, size, ".version")) {
        return NVFATBIN_ERROR_MISSING_PTX_VERSION;
    }
    if (!contains_directive(code, size, ".target")) {
        return NVFATBIN_ERROR_MISSING_PTX_ARCH;
    }
    uint32_t ptx_arch = extract_ptx_arch(code, size);
    if (ptx_arch == 0) {
        return NVFATBIN_ERROR_MISSING_PTX_ARCH;
    }
    if (extracted_arch != NULL) {
        *extracted_arch = ptx_arch;
    }
    if (ptx_arch != expected_arch) {
        fprintf(stderr, "PTX architecture mismatch: .target sm_%u but expected sm_%u\n",
                ptx_arch, expected_arch);
        return NVFATBIN_ERROR_PTX_ARCH_MISMATCH;
    }
    return NVFATBIN_SUCCESS;
}

nvFatbinResult nvFatbinAddPTX(
    nvFatbinHandle handle,
    const char *code,
    size_t size,
    const char *arch,
    const char *identifier,
    const char *optionsCmdLine)
{
    if (handle == NULL) {
        return NVFATBIN_ERROR_NULL_POINTER;
    }

    if (code == NULL) {
        return NVFATBIN_ERROR_NULL_POINTER;
    }

    if (size == 0) {
        return NVFATBIN_ERROR_EMPTY_INPUT;
    }

    if (arch == NULL) {
        return NVFATBIN_ERROR_INVALID_ARCH;
    }

    nvFatbinHandle_impl *impl = (nvFatbinHandle_impl *)handle;
    uint32_t sm_arch = parse_arch_string(arch);
    if (sm_arch == 0) {
        fprintf(stderr, "Invalid architecture string: '%s'\n", arch);
        return NVFATBIN_ERROR_INVALID_ARCH;
    }

    uint32_t extracted_arch = 0;
    nvFatbinResult validate_result = validate_ptx(code, size, sm_arch, &extracted_arch);
    if (validate_result != NVFATBIN_SUCCESS) {
        return validate_result;
    }

    EntryNode *entry = (EntryNode *)malloc(sizeof(EntryNode));
    if (entry == NULL) {
        return NVFATBIN_ERROR_INTERNAL;
    }

    memset(entry, 0, sizeof(EntryNode));

    entry->header.type = 0x01;
    entry->header.unknown_02 = 0x0101;
    if (sm_arch >= 120) {
        entry->header.size = 80;
        entry->header.unknown_14 = 0x40;
        entry->extended_header = (uint8_t *)calloc(16, 1);
        if (entry->extended_header == NULL) {
            free(entry);
            return NVFATBIN_ERROR_INTERNAL;
        }
        *(uint32_t *)(entry->extended_header) = 0x48;
    } else {
        entry->header.size = 64;
        entry->header.unknown_14 = 0;
    }

    entry->header.data_size = 0;
    entry->header.minor_version = 0x00010008;
    entry->header.sm_arch = sm_arch;
    entry->header.reloc_offset = 0;
    entry->header.reloc_size = 0;
    entry->header.flags = 0x8011;
    memset(entry->header.reserved, 0, 16);
    *(uint64_t*)(&entry->header.reserved[8]) = size;

    int needs_null_terminator = 0;
    if (size > 0 && code[size - 1] != '\0') {
        needs_null_terminator = 1;
    }

    size_t storage_size = needs_null_terminator ? (size + 1) : size;
    entry->original_data = (uint8_t *)malloc(storage_size);
    if (entry->original_data == NULL) {
        free(entry);
        return NVFATBIN_ERROR_INTERNAL;
    }

    memcpy(entry->original_data, code, size);
    if (needs_null_terminator) {
        ((uint8_t *)entry->original_data)[size] = '\0';
    }

    entry->original_size = storage_size;
    entry->header.data_size = (uint32_t)storage_size;

    if (identifier != NULL && identifier[0] != '\0') {
        entry->identifier = strdup(identifier);
    } else {
        entry->identifier = strdup("");
    }

    if (entry->identifier == NULL) {
        free(entry->original_data);
        free(entry);
        return NVFATBIN_ERROR_INTERNAL;
    }

    entry->arch_string = strdup(arch);
    if (entry->arch_string == NULL) {
        free(entry->identifier);
        free(entry->original_data);
        free(entry);
        return NVFATBIN_ERROR_INTERNAL;
    }

    (void)optionsCmdLine;
    if (impl->compress_enabled) {
        nvFatbinResult result = compress_cubin_data(impl, entry);
        if (result != NVFATBIN_SUCCESS) {
            free_entry_node(entry);
            return result;
        }
    }
    else {
        entry->compressed_data = entry->original_data;
        entry->compressed_size = entry->original_size;
        entry->original_data = NULL;
        entry->header.data_size = (uint32_t)entry->compressed_size;
        entry->header.aligned_data_size = (uint32_t)ALIGN_8(entry->compressed_size);
        entry->header.padding = 0;
        entry->header.flags &= ~0x8000;
    }

    entry->next = NULL;
    if (impl->entries == NULL) {
        impl->entries = entry;
    } else {
        EntryNode *last = impl->entries;
        while (last->next != NULL) {
            last = last->next;
        }
        last->next = entry;
    }

    impl->entry_count++;
    impl->has_calculated_size = 0;
    impl->total_size = 0;
    return NVFATBIN_SUCCESS;
}

nvFatbinResult nvFatbinAddLTOIR(nvFatbinHandle handle,
                                 const void *code,
                                 size_t size,
                                 const char *arch,
                                 const char *identifier,
                                 const char *optionsCmdLine)
{
    nvFatbinHandle_impl *impl = (nvFatbinHandle_impl *)handle;
    if (!handle || !code) {
        return NVFATBIN_ERROR_NULL_POINTER;
    }

    if (size == 0) {
        return NVFATBIN_ERROR_EMPTY_INPUT;
    }

    if (!arch) {
        return NVFATBIN_ERROR_INVALID_ARCH;
    }

    uint32_t sm_arch = parse_arch_string(arch);
    if (sm_arch == 0 || !is_valid_arch(sm_arch)) {
        fprintf(stderr, "Invalid architecture string: '%s'\n", arch);
        return NVFATBIN_ERROR_INVALID_ARCH;
    }

    const uint8_t *data = (const uint8_t *)code;
    int valid_ltoir = 0;
    if (size >= 4) {
        if (data[0] == 0x0B && data[1] == 0x17 && data[2] == 0xC0 && data[3] == 0xDE) {
            valid_ltoir = 1;
        }
        else if (data[0] == 'B' && data[1] == 'C') {
            valid_ltoir = 1;
        }
    }

    if (!valid_ltoir) {
        fprintf(stderr, "Invalid LTOIR format: missing LLVM bitcode magic bytes\n");
        return NVFATBIN_ERROR_INVALID_NVVM;
    }

    EntryNode *entry = (EntryNode *)malloc(sizeof(EntryNode));
    if (!entry) {
        return NVFATBIN_ERROR_INTERNAL;
    }
    memset(entry, 0, sizeof(EntryNode));

    entry->header.type = ENTRY_TYPE_LTOIR;
    entry->header.unknown_02 = 0x0101;
    entry->header.size = FATBIN_ENTRY_SIZE;
    entry->header.data_size = 0;
    entry->header.unknown_14 = 0;
    entry->header.minor_version = 0x00010008;
    entry->header.sm_arch = sm_arch;
    entry->header.reloc_offset = 0;
    entry->header.reloc_size = 0;
    entry->header.flags = 0x8011;
    memset(entry->header.reserved, 0, 16);
    *(uint64_t*)(&entry->header.reserved[8]) = size;

    entry->original_data = malloc(size);
    if (!entry->original_data) {
        free(entry);
        return NVFATBIN_ERROR_INTERNAL;
    }
    memcpy(entry->original_data, code, size);
    entry->original_size = size;

    if (identifier && identifier[0] != '\0') {
        entry->identifier = strdup(identifier);
    } else {
        entry->identifier = strdup("");
    }

    if (!entry->identifier) {
        free(entry->original_data);
        free(entry);
        return NVFATBIN_ERROR_INTERNAL;
    }

    entry->arch_string = malloc(32);
    if (!entry->arch_string) {
        free(entry->identifier);
        free(entry->original_data);
        free(entry);
        return NVFATBIN_ERROR_INTERNAL;
    }
    snprintf(entry->arch_string, 32, "lto_%u", sm_arch);
    (void)optionsCmdLine;

    if (impl->compress_enabled) {
        nvFatbinResult result = compress_cubin_data(impl, entry);
        if (result != NVFATBIN_SUCCESS) {
            free_entry_node(entry);
            return result;
        }
    }
    else {
        entry->compressed_data = entry->original_data;
        entry->compressed_size = entry->original_size;
        entry->original_data = NULL;
        entry->header.data_size = (uint32_t)entry->compressed_size;
        entry->header.aligned_data_size = (uint32_t)ALIGN_8(entry->compressed_size);
        entry->header.padding = 0;
    }

    entry->next = NULL;
    if (!impl->entries) {
        impl->entries = entry;
    } else {
        EntryNode *last = impl->entries;
        while (last->next) {
            last = last->next;
        }
        last->next = entry;
    }

    impl->entry_count++;
    impl->has_calculated_size = 0;
    impl->total_size = 0;
    return NVFATBIN_SUCCESS;
}

nvFatbinResult nvFatbinAddIndex(nvFatbinHandle handle,
                                 const void *code,
                                 size_t size,
                                 const char *identifier)
{
    nvFatbinHandle_impl *impl = (nvFatbinHandle_impl *)handle;

    if (!handle || !code) {
        return NVFATBIN_ERROR_NULL_POINTER;
    }

    if (size == 0) {
        return NVFATBIN_ERROR_EMPTY_INPUT;
    }

    if (size > 0xFFFFFFFFULL) {
        return NVFATBIN_ERROR_COMPRESSED_SIZE_EXCEEDED;
    }

    EntryNode *entry = calloc(1, sizeof(EntryNode));
    if (!entry) {
        return NVFATBIN_ERROR_INTERNAL;
    }

    entry->header.type = 0x0F;
    entry->header.unknown_02 = 0x0101;
    entry->header.size = FATBIN_ENTRY_SIZE;
    entry->header.data_size = (uint32_t)size;
    entry->header.unknown_14 = 0;
    entry->header.minor_version = 0x00010008;
    entry->header.sm_arch = 0;
    entry->header.reloc_offset = 0;
    entry->header.reloc_size = 0;
    entry->header.flags = 0x8011;
    memset(entry->header.reserved, 0, sizeof(entry->header.reserved));
    *(uint64_t*)(&entry->header.reserved[8]) = size;

    entry->original_data = malloc(size);
    if (!entry->original_data) {
        free(entry);
        return NVFATBIN_ERROR_INTERNAL;
    }
    memcpy(entry->original_data, code, size);
    entry->original_size = size;

    if (identifier && identifier[0] != '\0') {
        entry->identifier = strdup(identifier);
    } else {
        entry->identifier = strdup("index");
    }
    entry->arch_string = strdup("index");

    if (!entry->identifier || !entry->arch_string) {
        free_entry_node(entry);
        return NVFATBIN_ERROR_INTERNAL;
    }

    if (impl->compress_enabled) {
        nvFatbinResult result = compress_cubin_data(impl, entry);
        if (result != NVFATBIN_SUCCESS) {
            free_entry_node(entry);
            return result;
        }
    } else {
        entry->compressed_data = entry->original_data;
        entry->compressed_size = entry->original_size;
        entry->original_data = NULL;
        entry->header.data_size = (uint32_t)entry->compressed_size;
        entry->header.aligned_data_size = (uint32_t)ALIGN_8(entry->compressed_size);
        entry->header.padding = 0;
    }

    entry->next = NULL;
    if (!impl->entries) {
        impl->entries = entry;
    } else {
        EntryNode *last = impl->entries;
        while (last->next) {
            last = last->next;
        }
        last->next = entry;
    }
    impl->entry_count++;
    impl->has_calculated_size = 0;
    return NVFATBIN_SUCCESS;
}

nvFatbinResult nvFatbinAddReloc(nvFatbinHandle handle,
                                 const void *code,
                                 size_t size)
{
    nvFatbinHandle_impl *impl = (nvFatbinHandle_impl *)handle;
    if (!handle || !code) {
        return NVFATBIN_ERROR_NULL_POINTER;
    }

    if (size == 0) {
        return NVFATBIN_ERROR_EMPTY_INPUT;
    }

    if (size < 64) {
        return NVFATBIN_ERROR_INTERNAL;
    }

    const uint8_t *obj_data = (const uint8_t *)code;
    uint32_t elf_magic = *(uint32_t *)obj_data;

    if (elf_magic != ELF_MAGIC) {
        fprintf(stderr, "nvFatbinAddReloc: Not a valid ELF object file\n");
        return NVFATBIN_ERROR_INTERNAL;
    }

    const char *ptx_data = (const char *)(obj_data + 64);
    size_t ptx_size = size - 64;
    if (ptx_size < 20) {
        fprintf(stderr, "nvFatbinAddReloc: No PTX data found in object\n");
        return NVFATBIN_ERROR_INTERNAL;
    }

    if (!contains_directive(ptx_data, ptx_size, ".version")) {
        fprintf(stderr, "nvFatbinAddReloc: PTX missing .version directive\n");
        return NVFATBIN_ERROR_MISSING_PTX_VERSION;
    }

    if (!contains_directive(ptx_data, ptx_size, ".target")) {
        fprintf(stderr, "nvFatbinAddReloc: PTX missing .target directive\n");
        return NVFATBIN_ERROR_MISSING_PTX_ARCH;
    }

    uint32_t ptx_arch = extract_ptx_arch(ptx_data, ptx_size);
    if (ptx_arch == 0) {
        fprintf(stderr, "nvFatbinAddReloc: Could not extract PTX architecture\n");
        return NVFATBIN_ERROR_MISSING_PTX_ARCH;
    }

    if (!is_valid_arch(ptx_arch)) {
        fprintf(stderr, "nvFatbinAddReloc: Invalid architecture sm_%u\n", ptx_arch);
        return NVFATBIN_ERROR_INVALID_ARCH;
    }

    char identifier[64];
    snprintf(identifier, sizeof(identifier), "relocatable_sm_%u", ptx_arch);

    for (EntryNode *e = impl->entries; e; e = e->next) {
        if (e->header.type == ENTRY_TYPE_PTX &&
            e->header.sm_arch == ptx_arch &&
            e->identifier != NULL &&
            strcmp(e->identifier, identifier) == 0) {
            fprintf(stderr, "nvFatbinAddReloc: Identifier '%s' already exists\n", identifier);
            return NVFATBIN_ERROR_IDENTIFIER_REUSE;
        }
    }

    EntryNode *entry = calloc(1, sizeof(EntryNode));
    if (!entry) {
        return NVFATBIN_ERROR_INTERNAL;
    }

    entry->header.type = ENTRY_TYPE_PTX;
    entry->header.unknown_02 = 0x0101;
    if (ptx_arch >= 120) {
        entry->header.size = 80;
        entry->header.unknown_14 = 0x40;
        entry->extended_header = (uint8_t *)calloc(16, 1);
        if (entry->extended_header == NULL) {
            free(entry);
            return NVFATBIN_ERROR_INTERNAL;
        }
        *(uint32_t *)(entry->extended_header) = 0x48;
    } else {
        entry->header.size = FATBIN_ENTRY_SIZE;
        entry->header.unknown_14 = 0;
    }

    entry->header.data_size = 0;
    entry->header.minor_version = 0x00010008;
    entry->header.sm_arch = ptx_arch;
    entry->header.reloc_offset = 0;             /* Could extract from ELF sections */
    entry->header.reloc_size = 0;
    entry->header.flags = 0x8011;               /* Compressed flag - required! */
    memset(entry->header.reserved, 0, sizeof(entry->header.reserved));
    /* FIX: reserved[8] (offset +56) = UNCOMPRESSED SIZE for cuobjdump buffer allocation */
    *(uint64_t*)(&entry->header.reserved[8]) = ptx_size;


    int needs_null_terminator = 0;
    if (ptx_size > 0 && ptx_data[ptx_size - 1] != '\0') {
        needs_null_terminator = 1;
    }

    size_t storage_size = needs_null_terminator ? (ptx_size + 1) : ptx_size;
    entry->original_data = malloc(storage_size);
    if (!entry->original_data) {
        free(entry);
        return NVFATBIN_ERROR_INTERNAL;
    }

    memcpy(entry->original_data, ptx_data, ptx_size);
    if (needs_null_terminator) {
        ((uint8_t *)entry->original_data)[ptx_size] = '\0';
    }
    entry->original_size = storage_size;
    entry->header.data_size = (uint32_t)storage_size;


    entry->identifier = strdup(identifier);
    if (!entry->identifier) {
        free_entry_node(entry);
        return NVFATBIN_ERROR_INTERNAL;
    }

    entry->arch_string = malloc(16);
    if (!entry->arch_string) {
        free_entry_node(entry);
        return NVFATBIN_ERROR_INTERNAL;
    }
    snprintf(entry->arch_string, 16, "sm_%u", ptx_arch);


    /* Relocatable PTX handling - apply compression settings */
    if (impl->compress_enabled) {
        nvFatbinResult result = compress_cubin_data(impl, entry);
        if (result != NVFATBIN_SUCCESS) {
            free_entry_node(entry);
            return result;
        }
    }
    else {
       
        entry->compressed_data = entry->original_data;
        entry->compressed_size = entry->original_size;
        entry->original_data = NULL;
        entry->header.data_size = (uint32_t)entry->compressed_size;
        entry->header.aligned_data_size = (uint32_t)ALIGN_8(entry->compressed_size);
        entry->header.padding = 0;
    }


    entry->next = NULL;
    if (!impl->entries) {
        impl->entries = entry;
    } else {
        EntryNode *last = impl->entries;
        while (last->next) {
            last = last->next;
        }
        last->next = entry;
    }
    impl->entry_count++;


    impl->has_calculated_size = 0;

    return NVFATBIN_SUCCESS;
}


/**
 * nvFatbinSize - Calculate and return fat binary size
 *
 * @handle: Handle
 * @size: Output size in bytes
 *
 * Returns: NVFATBIN_SUCCESS or error code
 */
nvFatbinResult nvFatbinSize(nvFatbinHandle handle, size_t *size)
{
    nvFatbinHandle_impl *impl = (nvFatbinHandle_impl *)handle;

    if (!handle || !size) {
        return NVFATBIN_ERROR_NULL_POINTER;
    }

    size_t total = FATBIN_HEADER_SIZE;  /* 16-byte fat binary header */
    /* Iterate through all entries */
    EntryNode *entry = impl->entries;
    while (entry) {
        total += entry->header.size;              /* Entry header (usually 64 bytes) */
        total += entry->header.aligned_data_size; /* Compressed data with 8-byte padding */
        entry = entry->next;
    }
    /* CRITICAL: NO terminator entry! Last entry ends exactly at container boundary */
    /* This matches NVIDIA's actual format - no zero-filled terminator */

    impl->total_size = total;
    impl->has_calculated_size = 1;
    /* FIX: header_size does NOT include the 16-byte container header itself! */
    impl->header_size = total - FATBIN_HEADER_SIZE;

    *size = total;
    return NVFATBIN_SUCCESS;
}

/**
 * nvFatbinGet - Serialize fat binary to buffer
 *
 * @handle: Handle
 * @buffer: Output buffer (must be pre-allocated)
 *
 * Returns: NVFATBIN_SUCCESS or error code
 */
nvFatbinResult nvFatbinGet(nvFatbinHandle handle, void *buffer)
{
    nvFatbinHandle_impl *impl = (nvFatbinHandle_impl *)handle;

    if (!handle || !buffer) {
        return NVFATBIN_ERROR_NULL_POINTER;
    }

    if (!impl->has_calculated_size) {
        return NVFATBIN_ERROR_INTERNAL;
    }

    uint8_t *write_ptr = (uint8_t *)buffer;

    *(uint32_t *)(write_ptr + 0) = impl->magic;
    *(uint16_t *)(write_ptr + 4) = impl->version_low;
    *(uint16_t *)(write_ptr + 6) = impl->version_high;
    *(uint64_t *)(write_ptr + 8) = impl->header_size;
    write_ptr += FATBIN_HEADER_SIZE;

    EntryNode *entry = impl->entries;
    while (entry) {
        /* Write base entry header (64 bytes) */
        memcpy(write_ptr, &entry->header, 64);
        write_ptr += 64;
        /* Write extended header if present (sm_100+: 48 bytes for ELF, 16 bytes for PTX) */
        if (entry->extended_header) {
            size_t extended_size = entry->header.size - 64;  /* 112-64=48 or 80-64=16 */
            memcpy(write_ptr, entry->extended_header, extended_size);
            write_ptr += extended_size;
        }
        /* Write compressed data with padding to aligned_data_size */
        memcpy(write_ptr, entry->compressed_data, entry->compressed_size);
        write_ptr += entry->compressed_size;
        /* Write padding zeros to reach aligned_data_size */
        size_t padding = entry->header.aligned_data_size - entry->compressed_size;
        if (padding > 0) {
            memset(write_ptr, 0, padding);
            write_ptr += padding;
        }

        entry = entry->next;
    }
    /* CRITICAL: NO terminator entry! File ends exactly after last entry's data */
    /* This matches NVIDIA's actual format */

    size_t bytes_written = write_ptr - (uint8_t *)buffer;
    if (bytes_written != impl->total_size) {
        return NVFATBIN_ERROR_INTERNAL;
    }

    return NVFATBIN_SUCCESS;
}
