/* dcql_dirent.h: one entry of the PC's /cd directory listing, as lib/dcql_disc.c reads it (lane ql-fix-dc, 2026-11-23,
 * audit item 2). MIT licence. Plain C, so the host test (lib/test/host_lib.c) checks the console's own rule.
 *
 * Wire form of an entry: u32 size (0xffffffff = a directory), u16 name length n, n bytes of name. The console keeps at
 * most `cap` bytes of the name and skips the rest. An entry whose declared name runs past the end of the listing is
 * truncated: it is rejected whole, and the listing ends there (the read position never passes the listing's size). */
#ifndef DCQL_DIRENT_H
#define DCQL_DIRENT_H
#include <stdint.h>

#define DCQL_DIRENT_HEAD 6u

/* pos: the read position just after the entry's head; size: the listing's size; n: the declared name length.
   Returns 0 with *keep (name bytes to read) and *skip (bytes to pass over), or -1 for a truncated entry. */
static inline int dcql_dirent_take(uint32_t pos, uint32_t size, uint32_t n, uint32_t cap, uint32_t *keep,
                                   uint32_t *skip) {
    if (pos > size || n > size - pos) return -1;
    *keep = n < cap ? n : cap;
    *skip = n - *keep;
    return 0;
}
#endif
