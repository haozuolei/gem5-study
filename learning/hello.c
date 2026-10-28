/*
 * hello.c -- a tiny "hello" workload for the gem5 x86 setup in this directory.
 *
 * The loop over `data` exists only to keep the CPU busy for a while (and to
 * touch the cache hierarchy), so the simulation does not finish immediately.
 *
 * Build (from the repository root):
 *
 *   gcc -O2 -static -o learning/hello learning/hello.c
 */

#include <inttypes.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#ifndef ARRAY_BYTES
#define ARRAY_BYTES (512 * 1024)
#endif

#ifndef ROUNDS
#define ROUNDS 3
#endif

static uint64_t data[ARRAY_BYTES / sizeof(uint64_t)];

int
main(void)
{
    const size_t n = ARRAY_BYTES / sizeof(uint64_t);
    uint64_t sum = 0;

    for (size_t i = 0; i < n; i++) {
        data[i] = i;
    }

    for (int r = 0; r < ROUNDS; r++) {
        for (size_t i = 0; i < n; i++) {
            sum += data[i];
        }
    }

    printf("Hello, gem5! sum = %" PRIu64 "\n", sum);
    return 0;
}
