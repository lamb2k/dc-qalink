#!/bin/sh
# lib/test/run.sh [outdir]: the console library's host test (host_lib.c) and its control (lane ql-fix-dc, 2026-11-23).
# Builds with gcc, AddressSanitizer (leaks included) and UBSan; the head must pass, the control (the code at 1990fb9)
# must fail on the truncated directory entry and on both ring-ownership checks. Exit 0 only when both hold.
set -u
D=$(cd "$(dirname "$0")" && pwd)
O=${1:-${TMPDIR:-/tmp}/dcql-host-lib}
mkdir -p "$O"
gcc -std=c99 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer \
  -o "$O/host_lib" "$D/host_lib.c" || { echo "host_lib: BUILD FAILED"; exit 2; }
"$O/host_lib" > "$O/head.log" 2>&1; head_rc=$?
ASAN_OPTIONS=detect_leaks=1 "$O/host_lib" --control > "$O/control.log" 2>&1; ctl_rc=$?
cat "$O/head.log"
echo "--- control (1990fb9 copies), rc $ctl_rc:"
grep '^FAIL' "$O/control.log"
rc=0
[ $head_rc -eq 0 ] && echo "HEAD PASS" || { echo "HEAD FAIL (rc $head_rc)"; rc=1; }
for must in "dirent: a truncated entry is rejected even when its kept part is present" \
            "rings: a failed start keeps no built-in ring" \
            "rings: the program's own rings free the built-in ones they replace"; do
  if grep -qF "FAIL $must" "$O/control.log"; then echo "control caught: $must"
  else echo "CONTROL MISSED: $must"; rc=1; fi
done
[ $ctl_rc -ne 0 ] || { echo "CONTROL PASSED (it must fail)"; rc=1; }
[ $rc -eq 0 ] && echo "host_lib: PASS (head passes, control fails as it must)"
exit $rc
