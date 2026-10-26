#!/bin/sh
# Only starts/stops direct child Redis test processes, never a system/shared service.
set -eu
umask 077
if [ "$#" -ne 3 ]; then
    echo 'Usage: sh scripts/verify-redis-linux.sh /absolute/python /absolute/redis-server unique-run-name' >&2
    exit 2
fi
test "$(uname -s)" = Linux || { echo 'Real Linux required' >&2; exit 2; }
case "$1" in /*) ;; *) echo 'Absolute Python path required' >&2; exit 2;; esac
case "$2" in /*/redis-server) ;; *) echo 'Absolute installed redis-server path required' >&2; exit 2;; esac
case "$3" in ''|*[!a-z0-9-]*) echo 'Run name: lowercase letters, digits, hyphen only' >&2; exit 2;; esac
test -x "$1" && test -x "$2" || exit 2
test -f backend/common/queue/redis_streams.py || { echo 'Run from inference-platform' >&2; exit 2; }
test ! -L runtime && test ! -L runtime/redis-validation || { echo 'Linked runtime directory refused' >&2; exit 2; }
task_root="$(pwd -P)/runtime/redis-validation/$3"
test ! -e "$task_root" || { echo 'Refusing existing run directory; choose a new name' >&2; exit 2; }
mkdir -p runtime/redis-validation
mkdir "$task_root"
# Dependency check cannot download or modify the environment.
"$1" -c 'import redis,pytest; print("Redis client and pytest available; no dependencies installed by this script")'
export INFERENCE_REDIS_TEST_SERVER="$2"
"$1" -m pytest tests/e2e/test_redis_live.py -q --tb=short \
    --basetemp="$task_root/pytest" --junitxml="$task_root/redis-live.xml"
"$1" -c 'import sys,xml.etree.ElementTree as E; r=E.parse(sys.argv[1]).getroot(); suites=[r] if r.tag=="testsuite" else list(r); counts={k:sum(int(s.get(k,"0")) for s in suites) for k in ("tests","failures","errors","skipped")}; print(counts); sys.exit(0 if counts["tests"]>=9 and not any(counts[k] for k in ("failures","errors","skipped")) else 2)' "$task_root/redis-live.xml"
