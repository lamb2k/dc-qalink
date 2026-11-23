#!/bin/bash
# tools/gate/gate.sh <branch> <head> [--lane NAME] [--plan FILE]... [--no-flycast] [--no-push]
#
# The integration gate of dc-qalink 0.2: the only way work reaches the integration branch (v0.2 unless GATE_BASE says
# otherwise). One run:
#   1. merge <head> into a scratch worktree at the integration branch's tip with --no-ff --no-commit (a conflict aborts
#      the merge and fails the gate: a lane's conflict is never resolved here); a head already in the branch is gated
#      as the baseline. The gate's own files (tools/gate/, docs/RELEASE_0.2.md) must not be changed by the head.
#   2. build every image with make -j2 (GATE_JOBS), objects in /dev/shm: the loader (adapter and modem), its return
#      stub, the test program, liveprog, the host build of the protocol core, the example program in four forms
#      (0.1's modem build, both transports, adapter with the way back, link off), and any other folder with a Makefile;
#   3. run every host test (tests/test_*.py; an unknown new one is run too), the syntax of every Python file, and the
#      controls: test_echo against the 0.2 work-in-progress PC tool (be1edc1e4) must fail;
#   4. the size rows of plan section 3 from liveprog's link map (sizes.py; never loosened: fixed memory is listed as
#      OVER while it is over), the blob check of what the head adds (blobcheck.py);
#   5. one Flycast suite (flysuite.py: guard, loop rows, bridge rows, MCP, the planted 2 ms control, the lane's own
#      controls and its two cited numbers from --plan);
#   6. PASS: commit the merge ("Merge <lane> <head> (gate PASS)"), fast-forward the integration branch, push it.
#      FAIL: the merge is thrown away; the report names the step and the file.
# The gate's own copy of tools/gate (the integration branch's worktree) runs, never the head's.
#
# Machine settings come from the file named by GATE_ENV (shell assignments; nothing of this PC is in the repository):
#   GATE_REPO     worktree of the integration branch (the gate's own files; fast-forwarded and pushed on PASS)
#   GATE_SCRATCH  worktree used only for gate merges (reset at every run)
#   GATE_OUT      evidence root: <GATE_OUT>/<lane>-<head9>/ gets gate.md, steps.tsv, build/, host/, sizes/, blob/, fly/
#   GATE_KOS_ENV  a script that sets up KallistiOS when sourced (KOS_BASE, kos-cc on PATH)
#   GATE_MKGDI    a GDI builder: [LAYOUT= SECTOR= PAD_1ST= TITLE=] mkgdi <elf> <datadir> <outdir>
#   GATE_WINPY    Windows Python (3.10+, with the mcp package) that runs flysuite.py and the MCP host test
#   GATE_FLYCAST  flycast.exe (Windows path) with a working broadband adapter;  GATE_FLYCFG its -config transients
#   GATE_PC_IP    this PC's address (the loader's host line; Flycast's adapter speaks from it)
#   GATE_PORT GATE_LPORT GATE_CTL GATE_HTTP GATE_MCP   the gate's own ports (never 52060)
#   optional: GATE_BASE (v0.2), GATE_JOBS (2), GATE_MINFREE ("C:=28 E:=20"), GATE_CPU_MAX (85), GATE_LINK_EXTRA
#             (LINK.TXT lines, ';'-separated), GATE_TRAILER (lines ending the merge commit), GATE_FLYLOCK (lock file)
set -u
usage() { echo "usage: GATE_ENV=<file> $0 <branch> <head> [--lane NAME] [--plan FILE]... [--no-flycast] [--no-push]" >&2; exit 2; }
[ $# -ge 2 ] || usage
BRANCH=$1 HEAD_REF=$2; shift 2
LANE=$BRANCH PLANS=() NOFLY=0 NOPUSH=0
while [ $# -gt 0 ]; do
  case $1 in
    --lane) LANE=$2; shift 2 ;;
    --plan) PLANS+=("$2"); shift 2 ;;
    --no-flycast) NOFLY=1; shift ;;
    --no-push) NOPUSH=1; shift ;;
    *) usage ;;
  esac
done
# GATE_FLY_ONLY=<row,row>: a measuring run (those Flycast rows only); it can never merge, move a branch or push
[ -n "${GATE_FLY_ONLY:-}" ] && NOPUSH=1
: "${GATE_ENV:?GATE_ENV names the machine settings file}"
. "$GATE_ENV"
for v in GATE_REPO GATE_SCRATCH GATE_OUT GATE_KOS_ENV GATE_MKGDI GATE_WINPY GATE_FLYCAST GATE_FLYCFG GATE_PC_IP \
         GATE_PORT GATE_LPORT GATE_CTL GATE_HTTP GATE_MCP; do
  [ -n "${!v:-}" ] || { echo "gate: $v is not set in $GATE_ENV" >&2; exit 2; }
done
for p in $GATE_PORT $GATE_LPORT $GATE_CTL $GATE_HTTP $GATE_MCP; do
  [ "$p" != 52060 ] || { echo "gate: port 52060 is the real console's loader port" >&2; exit 2; }
done
BASE=${GATE_BASE:-v0.2} J=${GATE_JOBS:-2} MINFREE=${GATE_MINFREE:-C:=28 E:=20} CPUMAX=${GATE_CPU_MAX:-85}
GR=$GATE_REPO S=$GATE_SCRATCH
export PYTHONDONTWRITEBYTECODE=1
winp() { wslpath -w "$1"; }
FLYPY=$(winp "$GR/tools/gate/flysuite.py")

git -C "$GR" fetch -q origin 2>/dev/null
HEADSHA=$(git -C "$GR" rev-parse --verify -q "$HEAD_REF^{commit}") || { echo "gate: no commit $HEAD_REF" >&2; exit 2; }
BASESHA=$(git -C "$GR" rev-parse --verify -q "$BASE^{commit}") || { echo "gate: no branch $BASE" >&2; exit 2; }
ID=$LANE-${HEADSHA:0:9}
G=$GATE_OUT/$ID; k=2
while [ -e "$G" ]; do G=$GATE_OUT/$ID-run$k; k=$((k + 1)); done
mkdir -p "$G"/build
W=/dev/shm/dcql-gate/$ID
rm -rf "${W:?}"; mkdir -p "$W"/host
export GATE_W=$W   # plan host rows may name it ($GATE_W/host/...)
export GATE_WINPY  # and the Windows Python (a lane's checks that need Windows-only packages)
STEPS=$G/steps.tsv; : > "$STEPS"
row() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$STEPS"; printf '[%s] %s: %s\n' "$2" "$1" "$3"; }
started=$(date '+%Y-%m-%d %H:%M:%S %z')
echo "gate: $LANE $BRANCH $HEADSHA into $BASE $BASESHA; evidence $G"

finish() {   # $1 verdict
  local verdict=$1 ended
  ended=$(date '+%Y-%m-%d %H:%M:%S %z')
  [ -d "$W/host" ] && cp -r "$W/host" "$G/" 2>/dev/null
  {
    echo "# Gate: $LANE $BRANCH ${HEADSHA:0:9} into $BASE"
    echo
    echo "**Verdict: $verdict** (mode: ${MODE:-?}; $started to $ended)"
    echo
    echo "- head: \`$HEADSHA\` ($BRANCH); base: \`$BASESHA\` ($BASE)${NEWSHA:+; merge commit \`$NEWSHA\`}"
    echo "- tiers: builds and host tests on this PC; Flycast rows are function checks (Flycast tier, never console numbers);"
    echo "  sizes from the link map (exact bytes)"
    echo "- tables: steps.tsv, build/, host/, sizes/sizes.md, blob/blob.md, fly/fly.md"
    echo
    echo "| step | verdict | detail |"
    echo "|---|---|---|"
    awk -F'\t' '{ gsub(/\|/, "/", $3); printf "| %s | %s | %s |\n", $1, $2, $3 }' "$STEPS"
    [ -f "$G/sizes/sizes.md" ] && { echo; sed -n '1,12p' "$G/sizes/sizes.md" | sed 's/^# /## /'; }
    [ -f "$G/fly/fly.md" ] && { echo; sed 's/^# /## /' "$G/fly/fly.md"; }
  } > "$G/gate.md"
  rm -rf "${W:?}"
  echo "gate: $verdict ($G/gate.md)"
  case $verdict in PASS*) exit 0 ;; *) exit 1 ;; esac
}

# ---------------------------------------------------------------------------------------------------- 0. machine
m=$("$GATE_WINPY" "$FLYPY" machine --cpu-max "$CPUMAX" --min-free $MINFREE 2>/dev/null); rc=$?
if [ $rc -ne 0 ]; then row machine STOPPED "$m"; MODE=none; finish "STOPPED (machine, not the lane)"; fi
row machine PASS "$m"

# ------------------------------------------------------------------------------------------------------ 1. merge
cd "$S" || exit 2
git merge --abort -q 2>/dev/null
git reset -q --hard && git clean -fdxq
git checkout -q --detach "$BASESHA" || { row merge FAIL "scratch worktree would not check out $BASESHA"; finish FAIL; }
if git merge-base --is-ancestor "$HEADSHA" "$BASESHA"; then
  MODE=baseline
  RANGE=origin/main..$BASESHA
  row merge PASS "baseline: $HEADSHA is already in $BASE ($BASESHA); nothing merged"
else
  MODE=merge
  RANGE=$BASESHA..$HEADSHA
  if ! git merge --no-ff --no-commit "$HEADSHA" > "$G/merge.log" 2>&1; then
    c=$(git diff --name-only --diff-filter=U | tr '\n' ' ')
    git merge --abort -q 2>/dev/null; git reset -q --hard
    row merge FAIL "conflict, merge aborted (not resolved here): ${c:-see merge.log}"
    finish FAIL
  fi
  row merge PASS "$(git diff --cached --shortstat | sed 's/^ //')"
  # the gate's own files (gate.sh, the suite, sizes, flysuite: tools/gate/) must come out of the merge as the base has
  # them. A gate change a lane was ruled to make lands on the integration branch first (by ql-int), so the merge then
  # changes nothing there. docs/RELEASE_0.2.md is a release document, not a gate file (ruling 2026-11-23).
  own=$(git diff --cached --name-only "$BASESHA" -- tools/gate/ | tr '\n' ' ')
  [ -z "$own" ] && row "gate files untouched" PASS "the merge leaves tools/gate/ as $BASE has it" \
                || row "gate files untouched" FAIL "the merge changes the gate's own files: $own"
fi
# the owner's direction (2026-11-22, "we can get rid of dialo"): no mascot in the tree, by text or by path
hits=$( { git grep -n -i -w -I -E 'mascots?|dialo' -- . ':(exclude)tools/gate/*'
          git ls-files | grep -i -E '(^|[/_.-])(mascots?|dialo)([/_.-]|$)'; } | head -5 | cut -c1-120 | tr '\n' ' ')
[ -z "$hits" ] && row "no mascot" PASS "no mascot or Dialo in the tree (text or path)" \
               || row "no mascot" FAIL "the owner removed the mascot (2026-11-22); found: $hits"
git log -1 --format='%H %s' "$HEADSHA" > "$G/head.txt"
mkdir -p "$G/tree"
for d in pc tests tools relay; do [ -d "$S/$d" ] && cp -r "$S/$d" "$G/tree/"; done
# the examples' Python tools too (a lane's disc rows run them on Windows, e.g. example/menu/shots.py)
(cd "$S" && find example -name '*.py' -type f -exec cp --parents {} "$G/tree/" \; 2>/dev/null)
find "$G/tree" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null

# ----------------------------------------------------------------------------------------------------- 2. builds
pushd "$(dirname "$GATE_KOS_ENV")" > /dev/null && . "./$(basename "$GATE_KOS_ENV")" > /dev/null 2>&1; popd > /dev/null
command -v kos-cc > /dev/null || { row "KallistiOS" FAIL "kos-cc not on PATH after $GATE_KOS_ENV"; finish FAIL; }
BID="gate-${HEADSHA:0:9}"
build_row() {   # name, outputs (space-separated, must exist after), command...
  local name=$1 outs=$2 log=$G/build/$1.log; shift 2
  if timeout 1200 "$@" > "$log" 2>&1; then
    local miss="" f
    for f in $outs; do [ -s "$f" ] || miss="$miss $(basename "$f")"; done
    if [ -n "$miss" ]; then row "build $name" FAIL "built, but missing:$miss (build/$name.log)"; return 1; fi
    local w; w=$(grep -c 'warning:' "$log")
    row "build $name" PASS "$(for f in $outs; do printf '%s %s B; ' "$(basename "$f")" "$(stat -c %s "$f")"; done)warnings $w"
  else
    row "build $name" FAIL "build/$name.log: $(grep -m2 -E 'error|Error|No rule' "$log" | tr '\n' ' ' | cut -c1-240)"
    return 1
  fi
}
O=$W/both
build_row loader "$O/loader.elf $O/LOADER.BIN $O/testprog.elf $O/testprog.bin $O/stub.bin $O/liveprog.elf $O/liveprog.map" \
  make -C "$S/loader" -j"$J" OUT="$O" BUILD="$BID" all
build_row loader-modem-only "$W/modem/loader.elf" make -C "$S/loader" -j"$J" OUT="$W/modem" LOADER_NET=modem BUILD="$BID" "$W/modem/loader.elf"
build_row host-core "$W/hostb/cl_host" make -C "$S/loader" OUT="$W/hostb" host
for v in "modem-0.1:DCQL_ENABLE=1 DCQL_NET=modem" "both:DCQL_ENABLE=1 DCQL_NET=both" \
         "bba-loop:DCQL_ENABLE=1 DCQL_NET=bba DCQL_LOOP=1" "link-off:DCQL_ENABLE=0"; do
  n=${v%%:*} vars=${v#*:} d=$W/ex-${v%%:*}
  rm -rf "${d:?}"; cp -r "$S/example" "$d"
  # shellcheck disable=SC2086
  build_row "example-$n" "$d/dcql-example.elf" make -C "$d" -j"$J" DCQL_DIR="$S/lib" DCQL_OBJDIR="$d/obj" $vars
done
for mk in "$S"/*/Makefile "$S"/example/*/Makefile; do   # any other program (example/netdisc/ and the like)
  [ -f "$mk" ] || continue
  rel=${mk#"$S"/}; rel=${rel%/Makefile}
  case $rel in loader|example) continue ;; esac
  dn=$(echo "$rel" | tr / -) d=$W/extra-$(echo "$rel" | tr / -)
  rm -rf "${d:?}"; mkdir -p "$d"
  # in place (its Makefile finds the library by its own path), outputs in OUT; the scratch tree is reset every run
  build_row "extra-$dn" "" make -C "$S/$rel" -j"$J" OUT="$d/out" DCQL_DIR="$S/lib" DCQL_OBJDIR="$d/obj" DCQL_ENABLE=1 \
    && for f in "$d"/out/*.elf; do [ -f "$f" ] && cp "$f" "$G/build/"; done
  ls "$d"/out/*.elf > /dev/null 2>&1 || row "build extra-$dn outputs" FAIL "no ELF in OUT (build/extra-$dn.log)"
done
{ for f in "$O"/*.elf "$W"/modem/loader.elf "$W"/ex-*/dcql-example.elf; do [ -f "$f" ] && sh-elf-size "$f" | tail -1 | sed "s#$W/##"; done; } \
  > "$G/build/elf-sizes.txt" 2>/dev/null
for f in testprog.elf liveprog.elf loader.elf LOADER.BIN stub.bin liveprog.map; do [ -f "$O/$f" ] && cp "$O/$f" "$G/build/"; done

# ------------------------------------------------------------------------------------------------- 3. host tests
H=$W/host
# Each host test runs in a network namespace of its own when it can (root: unshare -n, loopback up), so fixed test
# ports (test_loop's 53100 and up) never meet another lane's tests on the same machine. GATE_NETNS=0 turns it off.
NETNS=()
if [ "${GATE_NETNS:-1}" != 0 ] && unshare -n sh -c 'ip link set lo up' 2> /dev/null; then
  NETNS=(unshare -n sh -c 'ip link set lo up && exec "$@"' netns)
  row "host test isolation" PASS "each host test in its own network namespace (no port shared with other lanes)"
else
  row "host test isolation" PASS "shared network (no unshare): a port clash runs the test once more"
fi
test_row() {   # name, expect (0 or fail), command...
  local name=$1 expect=$2 log=$H/$1.log rc; shift 2
  (cd "$S" && timeout 900 "${NETNS[@]}" "$@") > "$log" 2>&1; rc=$?
  local again=""
  if [ $rc -ne 0 ] && grep -q 'Address already in use' "$log"; then   # a port another program held: once more
    mv "$log" "$log.clash"
    (cd "$S" && timeout 900 "${NETNS[@]}" "$@") > "$log" 2>&1; rc=$?
    again=" (run again after a port clash: $name.log.clash)"
  fi
  local tail; tail=$(grep -E '[0-9]+ of [0-9]+|passed|^OK|^FAILED|Ran [0-9]+|PASS|FAIL' "$log" | tail -2 | tr '\n' ' ' | cut -c1-200)
  if { [ "$expect" = 0 ] && [ $rc -eq 0 ]; } || { [ "$expect" = fail ] && [ $rc -ne 0 ] && [ $rc -ne 124 ]; }; then
    row "test $name" PASS "rc $rc${expect:+ (must: $expect)}$again; $tail"
  else
    row "test $name" FAIL "rc $rc (must: $expect)$again; host/$name.log; $tail"
  fi
}
pyfiles=$(cd "$S" && git ls-files '*.py' | tr '\n' ' ')
# shellcheck disable=SC2086
test_row python-syntax 0 python3 -c 'import ast,sys; [ast.parse(open(f,encoding="utf-8").read(),f) for f in sys.argv[1:]]; print(len(sys.argv)-1,"files parse")' $pyfiles
# the tag gate runs test_loop whole (audit 2026-11-23, item 6): with --quick it left out the 60 KB modem-rate
# transfer and its efficiency assertion. That row must be in the log, or this is a FAIL row of its own.
test_row test_loop 0 python3 tests/test_loop.py --host "$W/hostb/cl_host" --out "$H/loop"
grep -qE '^modem rate 1390 B/s: 60 KB raw image +PASS' "$H/test_loop.log" \
  && row "control test_loop modem-rate row" PASS "the 60 KB modem-rate transfer ran and passed (no --quick)" \
  || row "control test_loop modem-rate row" FAIL "the modem-rate row is missing or failed (host/test_loop.log)"
test_row test_bridge 0 python3 tests/test_bridge.py --out "$H/bridge"
test_row test_autoconnect 0 python3 tests/test_autoconnect.py --out "$H/autoconnect"
test_row test_echo 0 python3 tests/test_echo.py
if git -C "$S" show be1edc1e4:pc/dcql_loop.py > "$W/dcql_loop_be1edc1e4.py" 2>/dev/null; then
  # test_echo --old runs its control row itself: be1edc1e4's PC tool must take its own echo, and the row says so
  test_row control-test_echo-old 0 python3 tests/test_echo.py --old "$W/dcql_loop_be1edc1e4.py"
  grep -qE "^control: be1edc1e4.*must fail\) +PASS" "$H/control-test_echo-old.log" \
    && row "control be1edc1e4 takes its own echo" PASS "the old PC tool fails as it must (test_echo's control row)" \
    || row "control be1edc1e4 takes its own echo" FAIL "test_echo's control row did not show the old tool failing"
else
  row "test control-test_echo-old" FAIL "be1edc1e4's pc/dcql_loop.py not found"
fi
test_row test_link 0 sh -c 'cd tests && python3 -m unittest -v test_link'
# host_lib (ruling 2026-11-23, a standing row): the console library's host checks (gcc, ASan+UBSan+leaks), whose
# control (copies of the 1990fb9 code) must fail inside them. Required once the base has it; before that, not in the tree.
if [ -f "$S/lib/test/run.sh" ]; then
  test_row host_lib 0 sh lib/test/run.sh "$H/host_lib"
  grep -qE 'host_lib: PASS \(head passes, control fails as it must\)' "$H/host_lib.log" \
    && row "control host_lib" PASS "the head passes and its 1990fb9 control fails as it must" \
    || row "control host_lib" FAIL "host/host_lib.log lacks the head-passes, control-fails line"
elif git -C "$S" cat-file -e "$BASESHA:lib/test/run.sh" 2>/dev/null; then
  row "test host_lib" FAIL "lib/test/run.sh is in $BASE but not in the merge (a required test)"
else
  row "test host_lib" N/A "lib/test/run.sh not in this tree yet (it lands with ql-fix-dc)"
fi
mkdir -p "$G/host-mcp"
log=$G/host-mcp/test_mcp-host.log
(cd "$G/tree" && timeout 600 "$GATE_WINPY" -u tests/test_mcp.py --out "$(winp "$G/host-mcp")") > "$log" 2>&1; rc=$?
t=$(grep -E 'of [0-9]+ passed' "$log" | tail -1)
[ $rc -eq 0 ] && row "test test_mcp (host, simulated console)" PASS "$t" || row "test test_mcp (host, simulated console)" FAIL "rc $rc; host-mcp/test_mcp-host.log; $t"
# the lane's own host controls, from its plan: {"host": [{"name", "cmd" (sh, run in the merged tree), "expect": 0|"fail",
# "grep": a line the log must hold}]}
for p in "${PLANS[@]}"; do
  while IFS=$'\t' read -r hn hcmd hexp hgrep; do
    [ -n "$hn" ] || continue
    test_row "$hn" "$hexp" sh -c "$hcmd"
    if [ -n "$hgrep" ]; then
      grep -qE -- "$hgrep" "$H/$hn.log" && row "control $hn" PASS "log holds: $hgrep" || row "control $hn" FAIL "log lacks: $hgrep"
    fi
  done < <(python3 -c 'import json,sys
for h in json.load(open(sys.argv[1])).get("host", []):
    print("\t".join([h["name"], h["cmd"], str(h.get("expect", 0)), h.get("grep", "")]))' "$p")
done
# required controls (audit 2026-11-23, item 6): each its own row; a control the test no longer offers is a FAIL row,
# never silently left out. test_assets --controls: its four broken pieces (twiddle, strips, mip layout, 565) must each
# fail. test_safety: the PC boundaries (lane ql-fix-pc), and --controls on 1990fb9's pc/ (the tree the audit read):
# every one of its five items must fail there.
if grep -q -- "'--controls'" "$S/tests/test_assets.py" 2>/dev/null; then
  test_row control-test_assets 0 python3 tests/test_assets.py --controls --out "$H/assets-controls"
  grep -qE '^controls: 4 of 4 failed as they must' "$H/control-test_assets.log" \
    && row "control test_assets four broken pieces" PASS "4 of 4 failed as they must" \
    || row "control test_assets four broken pieces" FAIL "test_assets --controls did not show 4 of 4 failing"
else
  row "control test_assets four broken pieces" FAIL "tests/test_assets.py has no --controls (a required control)"
fi
# pinned safety rows (audit round 2, item 6): the gate names every row itself. A positive run must show each pinned row
# PASS (a row that is missing is a FAIL row of its own); the control on the audited tree must show each control row
# failing with its expected discrepancy (tests/ctlrows.py prints "control <id> (...) PASS" only then: an exception, a
# missing helper or another failure is a control FAIL). One gate row per pinned row.
safety_rows() {   # test, old revision, "control ids", "positive-only ids"
  local t=$1 rev=$2 ctl=$3 pos=$4 id
  if [ ! -f "$S/tests/$t.py" ]; then row "test $t" FAIL "tests/$t.py missing (a required test)"; return; fi
  test_row "$t" 0 python3 "tests/$t.py" --out "$H/$t"
  for id in $ctl $pos; do
    grep -qE "^${id//./\\.} +.* PASS found=" "$H/$t.log" \
      && row "pinned $t $id" PASS "present and passing" \
      || row "pinned $t $id" FAIL "missing or not passing (host/$t.log)"
  done
  if rm -rf "$W/old-$rev" && mkdir -p "$W/old-$rev" && git -C "$S" archive "$rev" pc tools | tar -x -C "$W/old-$rev"; then
    test_row "control-$t-$rev" 0 python3 "tests/$t.py" --pc "$W/old-$rev/pc" --controls --out "$H/$t-old"
    for id in $ctl; do
      grep -qE "^control ${id//./\\.} \(.*\) PASS$" "$H/control-$t-$rev.log" \
        && row "control $rev $id" PASS "fails on $rev with its expected discrepancy" \
        || row "control $rev $id" FAIL "did not fail as expected on $rev (host/control-$t-$rev.log)"
    done
  else
    row "control $rev $t" FAIL "$rev's pc/ could not be taken from git"
  fi
}
safety_rows test_safety 1990fb9 \
  "A1.bind_default A1.bind_flycast A1.lan_unparsed A2.pc_links A2.folder_disc A2.gdi_names A2.model_deps A2.asset_path
   A2.mcp_path A3.stills_cli A3.stills_bridge A3.open_files A3.http_length A3.mcp_base64 A4.bridge_end A5.parse_way_back" \
  "A1.allow_lan A1.local_linked A4.assembler A5.decoder A5.first_console"
safety_rows test_safety2 2ff9b9d \
  "B1.cli_wildcard B1.core_wildcard B2.plugin_link B2.loader_zip_link B2.gdi_swap B2.subfolder_closed B3.files_retired
   B3.files_capped B3.disc_capped B3.disc_retired B3.elf_span B3.png_dims B3.report_len B3.report_offsets
   B3.queue_capped B3.raw_dropped B3.mcp_upload B4.ladder_exhausted B4.bad_crc_report B4.failed_report
   B4.label_full_restart B5.bridge_receipt B5.cli_receipts_bounded B5.bridge_expire B5.cli_expire B5.bridge_exact_len
   B5.cli_exact_len" \
  "B1.allow_lan B1.flycast_exception B2.subfolder_api B4.label_stub B6.harness B7.disc_back_to_back B7.disc_whole_big"
# B7.disc_back_to_back's control is the head the gate refused (24cd792: back-to-back network-disc runs refused)
if [ -f "$S/tests/test_safety2.py" ] && rm -rf "$W/old-24cd792" && mkdir -p "$W/old-24cd792" \
   && git -C "$S" archive 24cd792 pc tools | tar -x -C "$W/old-24cd792"; then
  test_row control-test_safety2-24cd792 0 python3 tests/test_safety2.py --pc "$W/old-24cd792/pc" --controls \
    --control-rev 24cd792 --out "$H/test_safety2-old24"
  grep -qE '^control B7\.disc_back_to_back \(.*\) PASS$' "$H/control-test_safety2-24cd792.log" \
    && row "control 24cd792 B7.disc_back_to_back" PASS "fails on 24cd792 with its expected discrepancy" \
    || row "control 24cd792 B7.disc_back_to_back" FAIL "did not fail as expected on 24cd792 (host/control-test_safety2-24cd792.log)"
else
  row "control 24cd792 B7.disc_back_to_back" FAIL "24cd792's pc/ or tests/test_safety2.py not available"
fi
for t in "$S"/tests/test_*.py; do
  n=$(basename "$t" .py)
  case $n in test_loop|test_bridge|test_autoconnect|test_echo|test_link|test_mcp|test_safety|test_safety2) continue ;; esac
  if grep -q -- "'--out'" "$t"; then test_row "$n (new)" 0 python3 "tests/$n.py" --out "$H/$n"
  else test_row "$n (new)" 0 python3 "tests/$n.py"; fi
done

# ---------------------------------------------------------------------------------------------- 4. sizes, blobs
mkdir -p "$G/sizes"
python3 "$GR/tools/gate/sizes.py" --map "$O/liveprog.map" --objs client.o,cl_util.o,dcql_return.o,dcql_net_bba.o,dcql_live.o \
  --nm sh-elf-nm --objdir "$O/cl" --stub "$O/stub.bin" --baseline "$GATE_OUT/sizes-$BASE.json" --out "$G/sizes" > "$G/sizes/run.log" 2>&1 \
  || [ -f "$G/sizes/sizes.json" ]
if [ -f "$G/sizes/sizes.json" ]; then
  while IFS=$'\t' read -r r lim val ver d; do
    case $ver in FAIL*) vv=FAIL ;; OVER*) vv=OVER ;; *) vv=PASS ;; esac
    row "size $r" "$vv" "$val B against $lim B; $ver; change $d"
  done < <(python3 -c 'import json,sys; [print("\t".join(str(x) for x in (r["row"],r["limit"],r["measured"],r["verdict"],r["delta_vs_baseline"]))) for r in json.load(open(sys.argv[1]))["rows"]]' "$G/sizes/sizes.json")
else
  row "size rows" FAIL "sizes.py: $(tail -2 "$G/sizes/run.log" | tr '\n' ' ')"
fi
# a lane's own program's sizes (listed, not judged against the link rows): plan {"sizes": [{"name", "map", "objs",
# "objdir"}]}, map and objdir relative to the build folder of that program (extra-<dir>/out)
for p in "${PLANS[@]}"; do
  while IFS=$'\t' read -r sn smap sobjs sdir; do
    [ -n "$sn" ] || continue
    python3 "$GR/tools/gate/sizes.py" --report-only --map "$W/$smap" --objs "$sobjs" --nm sh-elf-nm --objdir "$W/$sdir" \
      --out "$G/sizes/$sn" > "$G/sizes/$sn.log" 2>&1
    if [ -f "$G/sizes/$sn/sizes.json" ]; then
      row "size $sn (own program)" PASS "$(python3 -c 'import json,sys; r=json.load(open(sys.argv[1]))["rows"]; print("; ".join("%s %s B" % (x["row"], format(x["measured"], ",")) for x in r))' "$G/sizes/$sn/sizes.json") (reported, not against the link rows)"
    else
      row "size $sn (own program)" FAIL "sizes.py: $(tail -1 "$G/sizes/$sn.log")"
    fi
  done < <(python3 -c 'import json,sys
for s in json.load(open(sys.argv[1])).get("sizes", []):
    print("\t".join([s["name"], s["map"], s["objs"], s["objdir"]]))' "$p")
done
mkdir -p "$G/blob"
if python3 "$GR/tools/gate/blobcheck.py" --repo "$S" --range "$RANGE" --out "$G/blob" > "$G/blob/run.log" 2>&1; then
  row "blob check" PASS "$RANGE: $(sed -n 3p "$G/blob/blob.md")"
else
  row "blob check" FAIL "$RANGE: $(grep -E '^\| [a-z]' "$G/blob/blob.md" | grep -v '^| rule' | head -4 | tr '\n' ' ' | cut -c1-300)"
fi

# ----------------------------------------------------------------------------------------------------- 5. Flycast
if [ $NOFLY = 1 ]; then
  row flycast SKIPPED "--no-flycast (a gate without its Flycast suite cannot PASS)"
elif grep -q $'\tFAIL\t' "$STEPS"; then
  row flycast SKIPPED "an earlier step failed"
else
  D=$W/discdata X=$W/disc
  mkdir -p "$D" "$G/fly/disc" "$W/tmp"
  cp "$O/LOADER.BIN" "$D/"
  {
    echo "# dc-qalink gate disc (Flycast only): the broadband adapter, this PC as the host, the gate's own port pair"
    printf 'net bba\nhost %s\nport %s\nlport %s\nslot 1\n' "$GATE_PC_IP" "$GATE_PORT" "$GATE_LPORT"
    [ -n "${GATE_LINK_EXTRA:-}" ] && echo "$GATE_LINK_EXTRA" | tr ';' '\n'
    for p in "${PLANS[@]}"; do python3 -c 'import json,sys; [print(l) for l in json.load(open(sys.argv[1])).get("linktxt",[])]' "$p"; done
  } > "$D/LINK.TXT"
  # a lane's test data: {"disc_prep": "<sh, run in the merged tree>"} with GATE_DISCDATA (files on the loader disc) and
  # GATE_FIXTURES (files the PC serves; {fixtures} in the rows)
  export GATE_DISCDATA=$D GATE_FIXTURES=$G/fly/fixtures
  mkdir -p "$GATE_FIXTURES"
  for p in "${PLANS[@]}"; do
    dp=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("disc_prep", ""))' "$p")
    [ -n "$dp" ] || continue
    if (cd "$S" && timeout 600 sh -c "$dp") >> "$G/build/disc_prep.log" 2>&1; then
      row "test data" PASS "disc_prep of $(basename "$p"): $(du -sh "$GATE_FIXTURES" | cut -f1) served, $(du -sh "$D" | cut -f1) on the disc"
    else
      row "test data" FAIL "disc_prep of $(basename "$p"): build/disc_prep.log: $(tail -1 "$G/build/disc_prep.log")"
    fi
  done
  # a lane's own discs (phase "disc" rows): {"own_discs": [{"name", "build": "<sh in the merged tree>", "elf": "<path>",
  # "title"}]}, each its program alone on a small disc in fly/discs/<name>/
  for p in "${PLANS[@]}"; do
    while IFS=$'\t' read -r on ob oe ot; do
      [ -n "$on" ] || continue
      od=$W/own-disc-$on; rm -rf "$od"; mkdir -p "$od/data" "$G/fly/discs/$on"
      echo "dc-qalink gate: $LANE's own disc $on" > "$od/data/README.TXT"
      if (cd "$S" && timeout 900 sh -c "$ob") > "$G/build/own-$on.log" 2>&1 && oe=$(eval echo "$oe") && [ -s "$oe" ] \
         && TMPDIR=$W/tmp LAYOUT=legacy SECTOR=2048 PAD_1ST=1 TITLE="${ot:-DC-QALINK}" timeout 600 bash "$GATE_MKGDI" "$oe" "$od/data" "$od/x" \
              >> "$G/build/own-$on.log" 2>&1; then
        cp "$od"/x/disc.gdi "$od"/x/track0* "$G/fly/discs/$on/"
        row "own disc $on" PASS "$(basename "$oe") $(stat -c %s "$oe") B on its own disc"
      else
        row "own disc $on" FAIL "build/own-$on.log: $(grep -m2 -E 'error|Error|No rule' "$G/build/own-$on.log" | tr '\n' ' ' | cut -c1-200)"
      fi
      rm -rf "$od"
    done < <(python3 -c 'import json,sys
for d in json.load(open(sys.argv[1])).get("own_discs", []):
    print("\t".join([d["name"], d["build"], d["elf"], d.get("title", "")]))' "$p")
  done
  if TMPDIR=$W/tmp LAYOUT=legacy SECTOR=2048 PAD_1ST=1 TITLE="DC-QALINK LOADER" timeout 600 bash "$GATE_MKGDI" "$O/loader.elf" "$D" "$X" \
       > "$G/build/mkgdi.log" 2>&1; then
    cp "$X"/disc.gdi "$X"/track0* "$D/LINK.TXT" "$G/fly/disc/"
    row "flycast disc" PASS "$(grep -v '^#' "$D/LINK.TXT" | tr '\n' ' ')"
    m=$("$GATE_WINPY" "$FLYPY" machine --cpu-max "$CPUMAX" --min-free $MINFREE 2>/dev/null); rc=$?
    if [ $rc -ne 0 ]; then
      row flycast STOPPED "machine before Flycast: $m"
    else
      pargs=(--plan "$(winp "$GR/tools/gate/suite.json")")
      for p in "${PLANS[@]}"; do pargs+=(--plan "$(winp "$p")"); done
      timeout 3000 "$GATE_WINPY" -u "$FLYPY" run --flycast "$GATE_FLYCAST" --config "$GATE_FLYCFG" \
        --disc "$(winp "$G/fly/disc/disc.gdi")" --tree "$(winp "$G/tree")" --build "$(winp "$G/build")" \
        --pc-ip "$GATE_PC_IP" --port "$GATE_PORT" --lport "$GATE_LPORT" --ctl "$GATE_CTL" --http "$GATE_HTTP" \
        --mcp-http "$GATE_MCP" --out "$(winp "$G/fly")" --fixtures "$(winp "$GATE_FIXTURES")" --discs "$(winp "$G/fly/discs")" \
        ${GATE_FLYLOCK:+--lock "$GATE_FLYLOCK"} ${GATE_FLY_ONLY:+--only "$GATE_FLY_ONLY"} "${pargs[@]}" \
        > "$G/fly/run.out" 2>&1; rc=$?
      sum=$(python3 -c 'import json,sys
r=json.load(open(sys.argv[1]))
ok=sum(1 for x in r["rows"] if x["ok"]); c=sum(1 for x in r["cites"] if x["ok"])
bad=[x["name"] for x in r["rows"] if not x["ok"]]+[x["what"] for x in r["cites"] if not x["ok"]]
print("%d of %d rows, %d of %d cited numbers reproduced%s" % (ok, len(r["rows"]), c, len(r["cites"]), ("; failed: "+", ".join(bad)) if bad else ""))' \
        "$G/fly/fly.json" 2>/dev/null)
      case $rc in
        0) row flycast PASS "$sum" ;;
        9) row flycast FAIL "the guard refused: another loader was heard (nothing sent to it); $sum" ;;
        20|21|22) row flycast STOPPED "machine or lock (rc $rc); $(tail -1 "$G/fly/run.out")" ;;
        *) row flycast FAIL "rc $rc; $sum; fly/fly.md" ;;
      esac
    fi
    rm -f "$G"/fly/disc/track0* "$G"/fly/discs/*/track0*
    rm -rf "${GATE_FIXTURES:?}"   # generated test data: rebuilt by disc_prep
  else
    row "flycast disc" FAIL "build/mkgdi.log: $(tail -2 "$G/build/mkgdi.log" | tr '\n' ' ')"
  fi
fi

# ----------------------------------------------------------------------------------------------------- 6. verdict
if grep -qE $'\t(FAIL|SKIPPED|STOPPED)\t' "$STEPS"; then
  if grep -q $'\tSTOPPED\t' "$STEPS" && ! grep -q $'\tFAIL\t' "$STEPS"; then V="STOPPED (machine, not the lane)"; else V=FAIL; fi
  git -C "$S" merge --abort -q 2>/dev/null; git -C "$S" reset -q --hard
  finish "$V"
fi
NEWSHA=""
if [ $MODE = merge ]; then
  fixed=$(grep -P '^size fixed\t' "$STEPS" | cut -f3)
  {
    echo "Merge $LANE ${HEADSHA:0:9} (gate PASS)"
    echo
    echo "$BRANCH at $HEADSHA, gated into $BASE: $(grep -c $'\tPASS\t' "$STEPS") steps pass (builds, host tests and"
    echo "controls, size rows, blob check, the Flycast suite). Fixed memory: $fixed."
    [ -n "${GATE_TRAILER:-}" ] && { echo; echo "$GATE_TRAILER"; }
  } > "$W/msg.txt"
  git -C "$S" commit -q -F "$W/msg.txt" || { row commit FAIL "git commit failed"; finish FAIL; }
  NEWSHA=$(git -C "$S" rev-parse HEAD)
  if [ $NOPUSH = 1 ]; then
    # --no-push never moves a branch of GATE_REPO (ql-mem's run fast-forwarded the lane's own checked-out branch onto
    # the gate's merge commit): the merge commit stays in the scratch tree only, named here
    row commit PASS "$NEWSHA (scratch only; no branch of GATE_REPO moved)"
  else
    # only the integration branch itself is fast-forwarded, and only when GATE_REPO has it checked out
    [ "$(git -C "$GR" symbolic-ref -q --short HEAD)" = "$BASE" ] || { row commit FAIL "GATE_REPO is not on $BASE: nothing moved"; finish FAIL; }
    [ "$(git -C "$GR" rev-parse "$BASE")" = "$BASESHA" ] || { row commit FAIL "$BASE moved during the gate"; finish FAIL; }
    git -C "$GR" merge -q --ff-only "$NEWSHA" || { row commit FAIL "fast-forward of $BASE failed"; finish FAIL; }
    row commit PASS "$NEWSHA on $BASE"
  fi
fi
if [ -n "${GATE_FLY_ONLY:-}" ]; then   # a measuring run (only some Flycast rows) never merges or pushes
  row push SKIPPED "GATE_FLY_ONLY=$GATE_FLY_ONLY: a measuring run, not a gate; nothing moved or pushed"
  finish "MEASURED (not a gate)"
fi
if [ $NOPUSH = 1 ]; then
  row push SKIPPED "--no-push: nothing moved or pushed${NEWSHA:+; the merge would be $NEWSHA}"
  finish "PASS (not pushed)"
fi
if git -C "$GR" push -q origin "refs/heads/$BASE:refs/heads/$BASE" > "$G/push.log" 2>&1; then
  row push PASS "origin/$BASE at $(git -C "$GR" rev-parse "$BASE")"
else
  row push FAIL "push.log: $(tail -2 "$G/push.log" | tr '\n' ' ')"
  finish "FAIL (push)"
fi
cp "$G/sizes/sizes.json" "$GATE_OUT/sizes-$BASE.json"
finish PASS
