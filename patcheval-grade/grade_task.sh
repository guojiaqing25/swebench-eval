#!/usr/bin/env bash
# 用 LiveCVEBench 的 PatchEval-verified 官方环境判分单个 CVE。
#
# 流程：docker pull 官方镜像 → 应用模型补丁到 /workspace/<repo> →
#       跑官方 tests/_run_test.sh（内部 rm 旧测试 + git apply test.patch + 执行真实测试）
#
# 用法: bash patcheval-grade/grade_task.sh <CVE-ID>
# 前提: 当前目录含 patcheval-grade/{manifest.json,patches/<CVE>.diff}
#       tests 已下载到 patcheval-grade/tests/<CVE>/（由 workflow 或 fetch_tests.sh 准备）
# 输出: patcheval-grade/results/<CVE>.json；退出码恒 0（结果靠 JSON 传达）

set -uo pipefail

CASE="${1:?需要 CVE-ID}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GRADE_DIR="$ROOT/patcheval-grade"
PATCH_FILE="$GRADE_DIR/patches/$CASE.diff"
TESTS_DIR="$GRADE_DIR/tests/$CASE"
RESULT_DIR="$GRADE_DIR/results"
RESULT_FILE="$RESULT_DIR/$CASE.json"
mkdir -p "$RESULT_DIR"

emit() {  # emit <status> <detail> [extra_json]
  python3 - "$CASE" "$1" "$2" "${3:-{\}}" "$RESULT_FILE" <<'PY'
import json, sys
case, status, detail, extra, out = sys.argv[1:6]
try:
    extra_obj = json.loads(extra)
except Exception:
    extra_obj = {}
payload = {"case_id": case, "status": status, "detail": detail, "solved": status == "solved"}
payload.update(extra_obj)
json.dump(payload, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"[grade] {case} status={status} {detail}")
PY
}

emit infra_error "started" '{"phase":"init"}'

if [ ! -f "$PATCH_FILE" ] || [ ! -s "$PATCH_FILE" ]; then
  emit no_patch "补丁为空"
  exit 0
fi
if [ ! -f "$TESTS_DIR/_run_test.sh" ]; then
  emit infra_error "缺少官方 tests/_run_test.sh"
  exit 0
fi

IMAGE=$(python3 - "$GRADE_DIR/manifest.json" "$CASE" <<'PY'
import json, sys
manifest = {m["case_id"]: m for m in json.load(open(sys.argv[1], encoding="utf-8"))}
print(manifest.get(sys.argv[2], {}).get("image_url") or "")
PY
)
if [ -z "$IMAGE" ]; then
  emit infra_error "manifest 中无 image_url"
  exit 0
fi

# 仓库目录从官方测试脚本解析（其内含 cd /workspace/<repo>），不猜
REPO_DIR=$(grep -oE 'cd /workspace/[A-Za-z0-9_.:-]+' "$TESTS_DIR/_run_test.sh" | head -1 | sed 's|cd /workspace/||')
if [ -z "$REPO_DIR" ]; then
  emit infra_error "无法从 _run_test.sh 解析仓库目录"
  exit 0
fi

echo "[grade] $CASE image=$IMAGE repo=/workspace/$REPO_DIR"
if ! docker pull "$IMAGE" > /tmp/pull_$CASE.log 2>&1; then
  emit infra_error "docker pull 失败（镜像不可用）"
  exit 0
fi

CID=$(docker run -d "$IMAGE" sleep infinity 2>/tmp/run_$CASE.log) || {
  emit infra_error "容器启动失败"
  exit 0
}
cleanup() { docker rm -f "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# 记录镜像内 commit（与数据集记录比对，用于区分「补丁错」与「环境不一致」）
IMAGE_HEAD=$(docker exec "$CID" bash -lc "cd /workspace/$REPO_DIR 2>/dev/null && git rev-parse HEAD" 2>/dev/null || echo "")

docker cp "$PATCH_FILE" "$CID:/tmp/model.patch" >/dev/null 2>&1
docker cp "$TESTS_DIR/." "$CID:/tests" >/dev/null 2>&1 || {
  emit infra_error "测试文件拷贝失败"
  exit 0
}

# 1) 应用模型补丁
APPLY_LOG=/tmp/apply_$CASE.log
if ! docker exec "$CID" bash -lc "cd /workspace/$REPO_DIR && git apply -v --whitespace=nowarn /tmp/model.patch" > "$APPLY_LOG" 2>&1; then
  if ! docker exec "$CID" bash -lc "cd /workspace/$REPO_DIR && git apply -v --3way --whitespace=nowarn /tmp/model.patch" >> "$APPLY_LOG" 2>&1; then
    tail -c 800 "$APPLY_LOG" > /tmp/apply_tail_$CASE.txt
    DETAIL=$(python3 -c "print(open('/tmp/apply_tail_$CASE.txt').read()[-400:].replace(chr(10),' '))")
    emit apply_failed "$DETAIL" "{\"image_head\":\"$IMAGE_HEAD\"}"
    exit 0
  fi
fi

# 2) 跑官方测试（脚本内部会 rm 旧测试文件 + git apply test.patch + 执行真实测试）
TEST_LOG=/tmp/test_$CASE.log
if docker exec "$CID" bash /tests/_run_test.sh > "$TEST_LOG" 2>&1; then
  emit solved "官方测试通过" "{\"image_head\":\"$IMAGE_HEAD\"}"
else
  DETAIL=$(python3 -c "print(open('$TEST_LOG').read()[-400:].replace(chr(10),' '))")
  emit test_failed "$DETAIL" "{\"image_head\":\"$IMAGE_HEAD\"}"
fi
