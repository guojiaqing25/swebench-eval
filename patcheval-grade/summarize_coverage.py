"""汇总 PatchEval 覆盖集判分结果（给 Actions 的 report job 用）。

单独一份脚本而不是 workflow 里的内联 heredoc，原因有两条：

1. 内联 Python 没法本地跑测试。这个汇总直接决定「分母」——它算错，
   通过率就是错的，而错误的形式往往很隐蔽（少几条 case 谁都看不出来）。
   抽成文件后可以在本地用构造数据验证。
2. 内联写法在 YAML 里必须严格对齐缩进，改一行就可能整段失效。

最重要的职责是**分母完整性校验**：matrix 里 226 条，如果某个 job 崩了、
artifact 没上传，那条 case 就不会出现在 results 里。如果只是数 artifact，
它会静默消失——分母变小、通过率变好看。因此这里显式比对 manifest：
缺哪条、缺几条，全部列出来并计入 `missing`。missing 不为 0 时退出码为 1，
让这个 job 变红——宁可报告显示失败，也不要安静地给出一个虚高的数字。

用法:
  python3 patcheval-grade/summarize_coverage.py \
      --artifacts artifacts --manifest patcheval-coverage/manifest.json \
      --subset patcheval-coverage/subset58.txt --out summary/patcheval_coverage_summary.json
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os


def load_rows(artifacts: str) -> list[dict]:
    rows: list[dict] = []
    for path in glob.glob(os.path.join(artifacts, "**", "results", "*.json"), recursive=True):
        try:
            rows.append(json.load(open(path, encoding="utf-8")))
        except Exception as exc:
            # 解析不了的也留下来，按 infra 记，不能直接丢掉
            rows.append(
                {
                    "case_id": os.path.basename(path).removesuffix(".json"),
                    "status": "infra_error",
                    "detail": f"result.json 解析失败: {exc}",
                    "solved": False,
                }
            )
    return rows


def dedupe(rows: list[dict]) -> list[dict]:
    """同一 case 可能被多个 artifact 目录收进来（路径不同、内容相同）。

    直接数会把分母放大；这里按 case_id 保留最后一条（后到的覆盖先到的）。
    """
    unique: dict[str, dict] = {}
    for row in rows:
        unique[str(row.get("case_id"))] = row
    return [unique[key] for key in sorted(unique)]


def bucket(rows: list[dict], key_of) -> dict[str, dict[str, int]]:
    """按某个维度分桶统计。

    同时给出两组数：
    - total / solved：整体口径，no_patch 也算进分母（端到端能不能修好）
    - patched / patched_solved：条件口径，只算真生成了补丁的 case
      （在「有补丁」前提下修对多少）

    两组必须都留着。只给条件口径等于把没生成出来的悄悄剔出分母，
    那是最坏的一种「让数字变好看」；只给整体口径则定位不到问题在哪一环。
    """
    table: dict[str, dict[str, int]] = {}
    for row in rows:
        key = key_of(row)
        slot = table.setdefault(key, {"total": 0, "solved": 0, "patched": 0, "patched_solved": 0})
        hit = 1 if row.get("solved") else 0
        slot["total"] += 1
        slot["solved"] += hit
        if str(row.get("status")) != "no_patch":
            slot["patched"] += 1
            slot["patched_solved"] += hit
    return table


def split_ids(raw: str) -> list[str]:
    """把 rerun.txt / --only 这类输入解析成 case 列表。

    分隔符同时接受逗号与换行：两种写法在仓库里都出现过（rerun.txt 用逗号，
    人工补跑时习惯一行一条）。只认逗号会把整份文件当成一个 case。
    """
    return [item.strip() for line in (raw or "").splitlines()
            for item in line.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总覆盖集判分结果并校验分母")
    parser.add_argument("--artifacts", default="artifacts")
    parser.add_argument("--manifest", default="patcheval-coverage/manifest.json")
    parser.add_argument("--subset", default="patcheval-coverage/subset58.txt")
    parser.add_argument("--out", default="summary/patcheval_coverage_summary.json")
    parser.add_argument(
        "--only",
        default="",
        help="只把这些 case 视为本次计划范围（逗号或换行分隔）。"
             "定向重跑时矩阵只跑其中几条，分母必须跟着缩小，"
             "否则剩下的两百条会被当成「静默丢失」而误报分母不完整。",
    )
    args = parser.parse_args()

    manifest = json.load(open(args.manifest, encoding="utf-8"))
    full_total = len(manifest)
    planned_ids = split_ids(args.only)
    if planned_ids:
        known = {str(m["case_id"]) for m in manifest}
        unknown = [c for c in planned_ids if c not in known]
        if unknown:
            # 指定的 case 不在清单里，说明 run 配置和清单已经不同源了；
            # 直接报出来，不要静默地把它算成「已计划但缺失」。
            print(f"✗ --only 里有 {len(unknown)} 条不在 manifest：{unknown[:10]}")
            return 1
        manifest = [m for m in manifest if str(m["case_id"]) in set(planned_ids)]
    # scoped 的含义是「本次只跑了清单的一部分」，不是「传了 --only」。
    # 全量跑也会传 --only（把计划清单透传下来），若只按「是否传参」判断，
    # 226 条的全量成绩单会被标题写成「定向重跑」，读者会以为不能当整体分数引用。
    scoped = bool(planned_ids) and len(manifest) < full_total
    langs = {m["case_id"]: m.get("language", "unknown") for m in manifest}
    cwes = {m["case_id"]: m.get("cwe_id", "") for m in manifest}

    rows = dedupe(load_rows(args.artifacts))
    if planned_ids:
        # artifacts 里可能混着上一次全量跑的产物（同一个 case 两份结果）。
        # 统计口径必须严格等于「本次计划」，否则定向重跑会混进旧数据。
        planned = set(planned_ids)
        rows = [r for r in rows if str(r.get("case_id")) in planned]
    graded = [r for r in rows if r.get("status") != "infra_error"]
    solved = sum(1 for r in graded if r.get("solved"))
    counts = collections.Counter(str(r.get("status")) for r in rows)

    # 条件通过率：只算「确实生成了补丁」的那些 case。
    #
    # 为什么必须单独给这个数：整体通过率把 no_patch 也算进分母，它衡量的是
    # 「端到端能不能修好」——这当然是最该看的数，但它把两件事揉在了一起：
    # 生成不出补丁，和补丁修不对。改造生成环节后整体通过率会动，可到底是
    # 生成率变了还是修复质量变了，从单一数字看不出来。
    # 两个数并列给：整体=端到端，条件=在「有补丁」前提下修对的比例。
    # 只报条件通过率是作弊（等于把没生成出来的悄悄剔出分母），
    # 只报整体则无法定位问题出在哪一环。
    patched = [r for r in graded if r.get("status") != "no_patch"]
    patched_solved = sum(1 for r in patched if r.get("solved"))

    # 分母完整性：manifest 里有、results 里没有的，必须逐条列出。
    seen = {str(r.get("case_id")) for r in rows}
    missing = [str(m["case_id"]) for m in manifest if str(m["case_id"]) not in seen]

    subset58: set[str] = set()
    if os.path.exists(args.subset):
        subset58 = {
            line.strip()
            for line in open(args.subset, encoding="utf-8").read().splitlines()
            if line.strip()
        }
    sub_rows = [r for r in graded if str(r.get("case_id")) in subset58]
    sub_solved = sum(1 for r in sub_rows if r.get("solved"))

    payload = {
        # manifest_total 现在是「本次计划范围」的条数：全量跑=226，定向重跑=计划的那几条。
        # 分母永远等于计划，而不是永远等于 226——否则定向重跑每次都会红。
        "manifest_total": len(manifest),
        "scoped": scoped,
        "total": len(rows),
        "results_total": len(rows),
        "graded": len(graded),
        "excluded_infra": len(rows) - len(graded),
        "solved": solved,
        "solve_rate": (solved / len(graded)) if graded else 0.0,
        # 条件通过率与整体通过率必须成对出现（理由见上文注释）
        "patched_total": len(patched),
        "patched_solved": patched_solved,
        "patched_solve_rate": (patched_solved / len(patched)) if patched else 0.0,
        "status_counts": dict(counts),
        "by_language": bucket(graded, lambda r: langs.get(str(r.get("case_id")), "unknown")),
        "by_cwe": dict(sorted(bucket(graded, lambda r: cwes.get(str(r.get("case_id")), "")).items())),
        "no_patch": counts.get("no_patch", 0),
        "apply_failed": counts.get("apply_failed", 0),
        "missing": missing,
        "infra_reasons": [
            {"case_id": r["case_id"], "detail": str(r.get("detail") or "")[:200]}
            for r in rows
            if r.get("status") == "infra_error"
        ],
        "subset58": {
            "graded": len(sub_rows),
            "solved": sub_solved,
            "solve_rate": (sub_solved / len(sub_rows)) if sub_rows else 0.0,
        },
        "rows": rows,
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump(payload, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(
        f"graded={len(graded)} solved={solved} excluded_infra={payload['excluded_infra']} "
        f"missing={len(missing)} status={dict(counts)}"
    )
    if missing:
        print(f"✗ 有 {len(missing)} 条 case 没有任何判分结果：{missing[:10]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
