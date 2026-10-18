#!/usr/bin/env bash
# 对照组：**不打任何补丁**直接跑官方测试，必须失败。
#
# 为什么需要这一步：
# 「通过率高」有两种可能——智能体真修好了，或者测试本身被放松/失效了。
# 这个脚本专门排掉后者：镜像停在漏洞版本、什么都不改，官方测试若仍然通过，
# 说明该 case 的测试根本没在测这个漏洞（无区分度），必须从评估集剔除并说明原因。
#
# 判据（与主判分相反）：
#   test_failed（非零退出）→ 对照成立，测试有区分度  ✅
#   no_discrimination（0） → 测试无区分度，该 case 作废  ❌
#   test_timeout           → 视为无效样本（不计入区分度统计）
#   infra_error            → 环境问题，重跑
#
# 用法: bash patcheval-grade/control_task.sh <CVE-ID>
# 前提: 当前目录含 <GRADE_DIR>/{manifest.json,tests/<CVE>/}；GRADE_DIR_NAME 可覆盖目录名
# 输出: <GRADE_DIR>/results/<CVE>.json + <GRADE_DIR>/results/<CVE>.log（测试日志尾部）
#
# 注意：本文件**不能**用 `set -e`。这里「命令失败」正是被观察的对象，
# 一旦 -e 生效，docker exec 的非零退出会直接中断脚本，永远走不到采集退出码那一步，
# 结果就是「拿不到判定」被误读成「测试没区分度」或「job 失败」。
set -uo pipefail

CASE="${1:?需要 CVE-ID}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GRADE_DIR="$ROOT/${GRADE_DIR_NAME:-patcheval-grade}"
RESULT_DIR="$GRADE_DIR/results"
RESULT_FILE="$RESULT_DIR/$CASE.json"
LOG_FILE="$RESULT_DIR/$CASE.log"
mkdir -p "$RESULT_DIR"

emit() {  # emit <status> <detail> [extra_json]
  python3 - "$CASE" "$1" "$2" "${3:-{\}}" "$RESULT_FILE" <<'PY'
import json, sys
case, status, detail, extra, out = sys.argv[1:6]
try:
    extra_obj = json.loads(extra)
except Exception:
    extra_obj = {}
payload = {"case_id": case, "status": status, "detail": detail,
           # 对照组里「有区分度」= 未打补丁时测试失败，所以 solved 取 test_failed
           "solved": status == "test_failed"}
payload.update(extra_obj)
json.dump(payload, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"[control] {case} status={status} {detail}")
PY
}

dump_log() {  # 把测试日志尾部落到 results/，artifact 才能带上（否则无从诊断）
  local src="$1"
  [ -f "$src" ] || return 0
  tail -c 4000 "$src" > "$LOG_FILE" 2>/dev/null || true
}

IMAGE=$(python3 - "$GRADE_DIR/manifest.json" "$CASE" <<'PY'
import json, sys
path, case = sys.argv[1], sys.argv[2]
manifest = {m["case_id"]: m for m in json.load(open(path, encoding="utf-8"))}
if case not in manifest:
    # 必须把「这个 case 不在本目录的 manifest 里」和「在但缺镜像」分开报：
    # 前者通常是目录选错了（例如 push 触发时 grade_dir 为空、退回 58 条子集的
    # patcheval-grade，于是 226 抽样里的部分 case 根本查不到），
    # 后者才是上游真的没给镜像。混成一句「manifest 无 image_url」会把人引到错的方向。
    print("MISSING_CASE")
else:
    print(manifest[case].get("image_url") or "")
PY
)
if [ "$IMAGE" = "MISSING_CASE" ]; then
  emit infra_error "$CASE 不在 $GRADE_DIR/manifest.json 里（目录选错？检查 GRADE_DIR_NAME）"
  exit 0
fi
[ -z "$IMAGE" ] && { emit infra_error "manifest 有该 case 但 image_url 为空"; exit 0; }
[ -f "$GRADE_DIR/tests/$CASE/_run_test.sh" ] || {
  emit infra_error "缺少官方 _run_test.sh"; exit 0; }

echo "[control] $CASE image=$IMAGE （故意不打补丁）"
if ! docker pull "$IMAGE" > /tmp/ctl_pull_$CASE.log 2>&1; then
  emit infra_error "docker pull 失败"
  exit 0
fi

CID=$(docker run -d "$IMAGE" sleep infinity 2>/tmp/ctl_run_$CASE.log) || {
  emit infra_error "容器启动失败"; exit 0; }
trap 'docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT

# 上游测试文件不落我们的仓库，这里从 fetch_tests.sh 现拉的位置拷进容器
docker cp "$GRADE_DIR/tests/$CASE/." "$CID:/tests" >/dev/null 2>&1 || {
  emit infra_error "测试拷贝失败"; exit 0; }

TEST_LOG=/tmp/ctl_test_$CASE.log
# 关键：这里**不执行 git apply**，镜像保持漏洞版本
timeout "${TEST_TIMEOUT_SECONDS:-240}" docker exec "$CID" bash /tests/_run_test.sh > "$TEST_LOG" 2>&1
RC=$?
dump_log "$TEST_LOG"

if [ "$RC" -eq 0 ]; then
  emit no_discrimination "未打补丁但官方测试通过：该 case 测试无区分度，应从评估集剔除"
elif [ "$RC" -eq 124 ]; then
  emit test_timeout "未打补丁即超时（不计入区分度统计，需人工确认是否依赖外部服务）"
else
  emit test_failed "未打补丁测试失败（rc=$RC），对照成立：测试确实在测该漏洞"
fi
exit 0
