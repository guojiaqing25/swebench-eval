#!/usr/bin/env bash
# 下载 LiveCVEBench 的官方判分测试文件到 patcheval-grade/tests/<CVE>-lower/
#
# 用法: bash patcheval-grade/fetch_tests.sh [CVE-ID ...]
#       不带参数则按 manifest 全量下载
# 说明: 只下载 tests/{_run_test.sh,test.patch,test_outputs.py}；
#       上游目录名是小写 cve-xxxx-xxxx，本地统一转小写保存。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GRADE_DIR="$ROOT/patcheval-grade"
BASE="https://raw.githubusercontent.com/livecvebench/LiveCVEBench-Preview/master/tasks/PatchEval-verified"
PROXY_BASE="https://ghfast.top/https://raw.githubusercontent.com/livecvebench/LiveCVEBench-Preview/master/tasks/PatchEval-verified"

if [ "$#" -gt 0 ]; then
  CASES=("$@")
else
  mapfile -t CASES < <(python3 - "$GRADE_DIR/manifest.json" <<'PY'
import json, sys
for item in json.load(open(sys.argv[1], encoding="utf-8")):
    print(item["case_id"])
PY
)
fi

fail=0
for CASE in "${CASES[@]}"; do
  LOWER=$(echo "$CASE" | tr '[:upper:]' '[:lower:]')
  DEST="$GRADE_DIR/tests/$CASE"
  mkdir -p "$DEST"
  ok=1
  # 本地已有上游 clone 时直接复制（离线/被墙环境更稳）；否则走 raw（Actions 海外直连）
  LOCAL_TASK="${LCB_TASKS_DIR:-}/$LOWER/tests"
  for F in _run_test.sh test.patch test_outputs.py; do
    if [ -s "$DEST/$F" ]; then continue; fi
    if [ -n "${LCB_TASKS_DIR:-}" ] && [ -f "$LOCAL_TASK/$F" ]; then
      cp "$LOCAL_TASK/$F" "$DEST/$F"
      continue
    fi
    if curl -fsSL -m 60 "$BASE/$LOWER/tests/$F" -o "$DEST/$F" 2>/dev/null; then
      continue
    fi
    if curl -fsSL -m 60 "$PROXY_BASE/$LOWER/tests/$F" -o "$DEST/$F" 2>/dev/null; then
      continue
    fi
    echo "[fetch] $CASE 失败: $F"
    ok=0
    fail=1
  done
  chmod +x "$DEST/_run_test.sh" 2>/dev/null || true
  [ "$ok" = 1 ] && echo "[fetch] $CASE ok"
done

echo "[fetch] 完成，失败标记=$fail"
exit "$fail"
