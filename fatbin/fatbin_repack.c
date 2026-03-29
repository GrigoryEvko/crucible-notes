/*
 * fatbin_repack.c - Repack extracted cubins into NVIDIA fat binary
 */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <sys/stat.h>
#include <zstd.h>
#include "fatbin_pack.c"

static uint8_t* read_file(const char *path, size_t *size) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    *size = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *data = malloc(*size);
    if (data && fread(data, 1, *size, f) != *size) {
        free(data);
        data = NULL;
    }
    fclose(f);
    return data;
}

static int write_file(const char *path, const void *data, size_t size) {
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    size_t written = fwrite(data, 1, size, f);
    fclose(f);
    return (written == size) ? 0 : -1;
}

int main(int argc, char **argv) {
    if (argc < 3 || argc > 4) {
        fprintf(stderr, "Usage: %s <unpacked_dir> <output.bin> [compress_level]\n", argv[0]);
        fprintf(stderr, "  compress_level: 1-22 (default: 3)\n");
        return 1;
    }
    const char *unpacked_dir = argv[1];
    const char *output_path = argv[2];
    int compress_level = 3;
    if (argc == 4) {
        compress_level = atoi(argv[3]);
        if (compress_level < 1 || compress_level > 22) {
            fprintf(stderr, "Error: compress_level must be 1-22\n");
            return 1;
        }
    }
    char manifest_path[4096];
    snprintf(manifest_path, sizeof(manifest_path), "%s/manifest.txt", unpacked_dir);
    FILE *manifest = fopen(manifest_path, "r");
    if (!manifest) {
        fprintf(stderr, "Error: Cannot open manifest: %s\n", manifest_path);
        return 1;
    }
    nvFatbinHandle handle;
    char level_opt[32];
    snprintf(level_opt, sizeof(level_opt), "-compress-level=%d", compress_level);
    const char *options[] = {level_opt, "-compress-all"};
    nvFatbinResult res = nvFatbinCreate(&handle, options, 2);
    if (res != NVFATBIN_SUCCESS) {
        fprintf(stderr, "nvFatbinCreate failed: %s\n", nvFatbinGetErrorString(res));
        fclose(manifest);
        return 1;
    }
    printf("Repacking from %s with compression level %d\n", manifest_path, compress_level);
    char line[8192];
    int added = 0;
    while (fgets(line, sizeof(line), manifest)) {
        if (line[0] == '#' || line[0] == '\n') continue;
        int index;
        unsigned int type, sm_arch;
        size_t data_size, entry_size, entry_data_offset;
        char filename[256];
        if (sscanf(line, "%d %x %u %zu %zu %zu %s",
                   &index, &type, &sm_arch, &data_size, &entry_size,
                   &entry_data_offset, filename) != 7) {
            continue;
        }
        char full_path[4096];
        snprintf(full_path, sizeof(full_path), "%s/%s", unpacked_dir, filename);
        size_t cubin_size;
        uint8_t *cubin_data = read_file(full_path, &cubin_size);
        if (!cubin_data) {
            fprintf(stderr, "Warning: Cannot read %s\n", filename);
            continue;
        }
        char arch_str[16];
        snprintf(arch_str, sizeof(arch_str), "sm_%u", sm_arch);
        if (type == 0x01) {
            res = nvFatbinAddPTX(handle, (const char*)cubin_data, cubin_size,
                                arch_str, NULL, NULL);
        } else if (type == 0x02 || type == 0x10) {
            res = nvFatbinAddCubin(handle, cubin_data, cubin_size,
                                  arch_str, NULL);
        } else {
            fprintf(stderr, "Warning: Unknown type 0x%x for %s\n", type, filename);
            free(cubin_data);
            continue;
        }
        free(cubin_data);
        if (res != NVFATBIN_SUCCESS) {
            fprintf(stderr, "Warning: Failed to add %s: %s\n",
                    filename, nvFatbinGetErrorString(res));
            continue;
        }
        added++;
        if (added % 100 == 0) {
            printf("Added %d entries...\n", added);
        }
    }
    fclose(manifest);
    printf("Added %d entries\n", added);
    size_t fatbin_size;
    res = nvFatbinSize(handle, &fatbin_size);
    if (res != NVFATBIN_SUCCESS) {
        fprintf(stderr, "nvFatbinSize failed: %s\n", nvFatbinGetErrorString(res));
        nvFatbinDestroy(&handle);
        return 1;
    }
    void *fatbin_data = malloc(fatbin_size);
    if (!fatbin_data) {
        fprintf(stderr, "Out of memory for fat binary (%zu bytes)\n", fatbin_size);
        nvFatbinDestroy(&handle);
        return 1;
    }
    res = nvFatbinGet(handle, fatbin_data);
    if (res != NVFATBIN_SUCCESS) {
        fprintf(stderr, "nvFatbinGet failed: %s\n", nvFatbinGetErrorString(res));
        free(fatbin_data);
        nvFatbinDestroy(&handle);
        return 1;
    }
    if (write_file(output_path, fatbin_data, fatbin_size) != 0) {
        fprintf(stderr, "Error: Cannot write output file: %s\n", output_path);
        free(fatbin_data);
        nvFatbinDestroy(&handle);
        return 1;
    }
    printf("Wrote %s (%zu bytes)\n", output_path, fatbin_size);
    free(fatbin_data);
    nvFatbinDestroy(&handle);
    return 0;
}
