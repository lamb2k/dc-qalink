#!/bin/sh
# lib/test/run.sh [outdir]: the console library's host tests and their controls (lane ql-fix-dc, 2026-11-23).
#   host_lib.c  (gcc, AddressSanitizer with leaks, UBSan): the head must pass; the control (the code at 1990fb9) must
#               fail on the truncated directory entry and on both ring-ownership checks.
#   host_race.c (gcc, ThreadSanitizer; round 2): rings under stop/reconfigure/start with traffic, and the /pc read-ahead
#               block under three readers and the way out. The head must pass with no ThreadSanitizer report; the
#               control (the code at 2ff9b9d) must tear datagrams, return wrong bytes and draw ThreadSanitizer reports.
# Exit 0 only when all of that holds.
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

# ---- host_race (ThreadSanitizer). setarch -R: TSan needs the address layout without randomization on newer kernels.
gcc -std=c11 -O1 -g -Wall -Wextra -Werror -fsanitize=thread -fno-omit-frame-pointer -pthread \
  -o "$O/host_race" "$D/host_race.c" || { echo "host_race: BUILD FAILED"; exit 2; }
NR=""; command -v setarch >/dev/null 2>&1 && NR="setarch $(uname -m) -R"
$NR "$O/host_race" > "$O/race-head.log" 2>&1; rh=$?
$NR "$O/host_race" --control > "$O/race-control.log" 2>&1; rcc=$?
grep -v '^ *#\|^ *$' "$O/race-head.log" | grep -v '^  \|^SUMMARY\|^=\+$' | head -20
th=$(grep -c "WARNING: ThreadSanitizer" "$O/race-head.log"); tc=$(grep -c "WARNING: ThreadSanitizer" "$O/race-control.log")
echo "--- race control (2ff9b9d copies), rc $rcc, ThreadSanitizer reports $tc:"
grep '^FAIL\|^host_race' "$O/race-control.log"
if [ $rh -eq 0 ] && [ "$th" -eq 0 ]; then echo "RACE HEAD PASS (no ThreadSanitizer report)"
else echo "RACE HEAD FAIL (rc $rh, ThreadSanitizer reports $th)"; rc=1; fi
if grep -q "^FAIL rings: [0-9]* datagrams came out torn" "$O/race-control.log"; then echo "race control caught: rings, datagrams torn"
else echo "RACE CONTROL MISSED: rings"; rc=1; fi
# the read-ahead's race is caught by ThreadSanitizer in the 2ff9b9d read (old_read) every run; wrong bytes only on some
if grep -A8 "WARNING: ThreadSanitizer" "$O/race-control.log" | grep -q " old_read "; then
  echo "race control caught: ra, ThreadSanitizer race in the 2ff9b9d read ($(grep -c '^FAIL ra: ' "$O/race-control.log") wrong-bytes line)"
else echo "RACE CONTROL MISSED: ra"; rc=1; fi
[ "$tc" -gt 0 ] && echo "race control: ThreadSanitizer reported $tc races" || { echo "RACE CONTROL: no ThreadSanitizer report"; rc=1; }
[ $rc -eq 0 ] && echo "host_lib: PASS (head passes, control fails as it must)"
exit $rc
