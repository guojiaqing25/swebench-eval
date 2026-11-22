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
# 目录名可用 GRADE_DIR_NAME 覆盖：58 条均衡子集用 patcheval-grade，
# 全量覆盖集（226 条）用 patcheval-coverage。判分逻辑保持唯一一份，避免两份实现漂移。
GRADE_DIR="$ROOT/${GRADE_DIR_NAME:-patcheval-grade}"
PATCH_FILE="$GRADE_DIR/patches/$CASE.diff"
TESTS_DIR="$GRADE_DIR/tests/$CASE"
RESULT_DIR="$GRADE_DIR/results"
RESULT_FILE="$RESULT_DIR/$CASE.json"
# 判分入口二选一：
#   livecve  （默认，历史口径）LiveCVEBench 提取的 tests/_run_test.sh
#   official （官方 PatchEval-Verified 口径）镜像内置 /workspace/fix-run.sh
# 两者判据都是「退出码 0 = 通过」，但测试来源不同。要跟官方 leaderboard 比较时
# 必须用 official；历史数据仍是 livecve 口径，不能混报。
HARNESS="${GRADE_HARNESS:-livecve}"
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
if [ "$HARNESS" != "official" ] && [ ! -f "$TESTS_DIR/_run_test.sh" ]; then
  emit infra_error "缺少官方 tests/_run_test.sh"
  exit 0
fi

# 镜像清单允许回落到覆盖集那份：消融/对照等实验组与主结果用的是**同一批 226 个
# 官方镜像**，差别只在补丁。再复制一份 226 条的 manifest 进实验目录，除了制造
# 两份需要同步的文件之外没有任何收益，而且一旦上游镜像改了、只改了一处，
# 实验组就会静默地用旧镜像判分。
MANIFEST="$GRADE_DIR/manifest.json"
if [ ! -f "$MANIFEST" ] && [ -f "$ROOT/patcheval-coverage/manifest.json" ]; then
  MANIFEST="$ROOT/patcheval-coverage/manifest.json"
  echo "[grade] $CASE 无独立 manifest，回落到 patcheval-coverage/manifest.json"
fi
if [ ! -f "$MANIFEST" ]; then
  emit infra_error "找不到 manifest（$GRADE_DIR/manifest.json 与覆盖集清单都不存在）"
  exit 0
fi

IMAGE=$(python3 - "$MANIFEST" "$CASE" <<'PY'
import json, sys
manifest = {m["case_id"]: m for m in json.load(open(sys.argv[1], encoding="utf-8"))}
print(manifest.get(sys.argv[2], {}).get("image_url") or "")
PY
)
if [ -z "$IMAGE" ]; then
  emit infra_error "manifest 中无 image_url"
  exit 0
fi

# 仓库目录：livecve 口径从测试脚本解析（其内含 cd /workspace/<repo>）；
# official 口径由镜像内 fix-run.sh 自己处理，这里只为记录 IMAGE_HEAD 尽力找。
if [ "$HARNESS" = "official" ]; then
  REPO_DIR=""
else
  REPO_DIR=$(grep -oE 'cd /workspace/[A-Za-z0-9_.:-]+' "$TESTS_DIR/_run_test.sh" | head -1 | sed 's|cd /workspace/||')
  if [ -z "$REPO_DIR" ]; then
    emit infra_error "无法从 _run_test.sh 解析仓库目录"
    exit 0
  fi
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
if [ -n "$REPO_DIR" ]; then
  IMAGE_HEAD=$(docker exec "$CID" bash -lc "cd /workspace/$REPO_DIR 2>/dev/null && git rev-parse HEAD" 2>/dev/null || echo "")
else
  IMAGE_HEAD=$(docker exec "$CID" bash -lc 'cd /workspace && ls -d */ 2>/dev/null | head -1 | xargs -I{} git -C {} rev-parse HEAD 2>/dev/null' || echo "")
fi

TEST_LOG=/tmp/test_$CASE.log
TEST_TIMEOUT="${TEST_TIMEOUT_SECONDS:-240}"

if [ "$HARNESS" = "official" ]; then
  # 官方 PatchEval-Verified 入口：补丁放 /workspace/fix.patch，由镜像内
  # fix-run.sh 自己应用补丁、重置测试文件、执行 PoC/功能测试。
  # 这与官方 run_evaluation.py 的做法一致（它也只挂载 fix.patch）。
  if ! docker exec "$CID" bash -lc 'test -f /workspace/fix-run.sh'; then
    emit infra_error "镜像缺少 /workspace/fix-run.sh（无法按官方口径判分）"
    exit 0
  fi
  docker cp "$PATCH_FILE" "$CID:/workspace/fix.patch" >/dev/null 2>&1 || {
    emit infra_error "补丁拷贝到 /workspace/fix.patch 失败"
    exit 0
  }
  timeout "$TEST_TIMEOUT" docker exec "$CID" bash -lc 'cd /workspace && bash fix-run.sh' > "$TEST_LOG" 2>&1
  RC=$?
  # fix-run.sh 把「补丁贴不上」也记成非 0。从日志单独识别，保留
  # apply_failed 与 test_failed 的可区分性（不改变通过判据：只认退出码）。
  if [ "$RC" -ne 0 ] && [ "$RC" -ne 124 ] && grep -qiE 'patch does not apply|patch failed|error: patch|No such file or directory.*\.patch' "$TEST_LOG"; then
    APPLY_TAIL=$(python3 -c "print(open('$TEST_LOG').read()[-400:].replace(chr(10),' '))")
    emit apply_failed "$APPLY_TAIL" "{\"image_head\":\"$IMAGE_HEAD\",\"harness\":\"official\"}"
    exit 0
  fi
else
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
  #    单条测试必须设上限：部分任务依赖 D-Bus/网络服务，会永久挂起（如 CVE-2015-1326）
  timeout "$TEST_TIMEOUT" docker exec "$CID" bash /tests/_run_test.sh > "$TEST_LOG" 2>&1
  RC=$?
fi
if [ "$RC" -eq 0 ]; then
  emit solved "官方测试通过" "{\"image_head\":\"$IMAGE_HEAD\",\"harness\":\"$HARNESS\"}"
elif [ "$RC" -eq 124 ]; then
  emit test_timeout "测试超过 ${TEST_TIMEOUT}s 未返回（判定为测试未通过，非基础设施故障）" "{\"image_head\":\"$IMAGE_HEAD\",\"harness\":\"$HARNESS\"}"
else
  # detail 取日志尾部 400 字符：这是给人看的一句话结论，不是诊断材料。
  # 尾部截断会把首个单词切掉（"AssertionError" 变成 "tionError"），
  # 所以除 detail 之外再单独抽一组**失败信号**字段，供后续归因使用：
  #   fail_kind  从整份日志里识别的失败形态（断言/符号未定义/编译/超时…）
  #   fail_lines 日志中出现失败标记的原始行（去重后最多 5 行，各 200 字符）
  # 这两个字段只增加可观测性，不参与任何判定：solved 仍然只看退出码。
  DETAIL=$(python3 -c "print(open('$TEST_LOG').read()[-400:].replace(chr(10),' '))")
  SIGNALS=$(python3 - "$TEST_LOG" <<'PY'
import json, re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
# 形态识别的顺序就是判断优先级：具体形态在前，泛化形态在后。
# 先判「期望抛异常却没抛」是因为它和「断言值不符」的改法完全不同：
# 前者说明修复没阻断危险路径，后者说明改到了错的分支或改过头。
kinds = [
    ("no_exception_raised",
     r"Missing expected exception|did not return expected error|"
     r"expected fn to throw|to throw an error|should throw|"
     r"Expected .* to be thrown|assertRaises|not ok \d+ - .*throw"),
    ("symbol_undefined",
     r"undefined: \w+|undefined reference|is not a function|has no attribute|"
     r"NameError:|ReferenceError:|too many arguments in call"),
    ("compile_error",
     r"SyntaxError|IndentationError|Unexpected token|ParseError|"
     r"invalid syntax|unexpected end of|Invalid function name"),
    ("module_missing",
     r"Cannot find module|ModuleNotFoundError|ImportError|No module named|"
     r"ERR_MODULE_NOT_FOUND|DistributionNotFound|cannot find package"),
    ("build_env",
     r"go: updates to go\.mod needed|go mod tidy|npm ERR!|error Command failed"),
    ("assertion_mismatch",
     r"AssertionError|ERR_ASSERTION|assert.*==|expected .* to (?:be|equal)"),
    ("over_blocking", r"unexpectedly threw|should not throw|but threw"),
]
kind = ""
for name, pattern in kinds:
    if re.search(pattern, text, re.I):
        kind = name
        break
# 收集失败标记行：这些行自带上下文，比尾部 400 字符有用得多
marks = re.compile(
    r"(FAILED|FAIL:|not ok \d+|AssertionError|ERR_ASSERTION|Traceback|"
    r"Error:|error:|panic:|undefined:|SyntaxError|Missing expected)", re.I)
picked, seen = [], set()
for line in text.splitlines():
    stripped = line.strip()
    if not stripped or not marks.search(stripped):
        continue
    # 全角去重：日志里同一断言常在 summary 与详情各出现一次
    key = stripped[:120]
    if key in seen:
        continue
    seen.add(key)
    picked.append(stripped[:200])
    if len(picked) >= 5:
        break
print(json.dumps({"fail_kind": kind, "fail_lines": picked}, ensure_ascii=False))
PY
)
  emit test_failed "$DETAIL" "$(
    python3 - "$SIGNALS" "$IMAGE_HEAD" "$HARNESS" <<'PY'
import json, sys
signals = json.loads(sys.argv[1] or "{}")
signals["image_head"] = sys.argv[2]
signals["harness"] = sys.argv[3]
print(json.dumps(signals, ensure_ascii=False))
PY
  )"
fi
