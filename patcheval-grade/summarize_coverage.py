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
    table: dict[str, dict[str, int]] = {}
    for row in rows:
        key = key_of(row)
        slot = table.setdefault(key, {"total": 0, "solved": 0})
        slot["total"] += 1
        slot["solved"] += 1 if row.get("solved") else 0
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总覆盖集判分结果并校验分母")
    parser.add_argument("--artifacts", default="artifacts")
    parser.add_argument("--manifest", default="patcheval-coverage/manifest.json")
    parser.add_argument("--subset", default="patcheval-coverage/subset58.txt")
    parser.add_argument("--out", default="summary/patcheval_coverage_summary.json")
    args = parser.parse_args()

    manifest = json.load(open(args.manifest, encoding="utf-8"))
    langs = {m["case_id"]: m.get("language", "unknown") for m in manifest}
    cwes = {m["case_id"]: m.get("cwe_id", "") for m in manifest}

    rows = dedupe(load_rows(args.artifacts))
    graded = [r for r in rows if r.get("status") != "infra_error"]
    solved = sum(1 for r in graded if r.get("solved"))
    counts = collections.Counter(str(r.get("status")) for r in rows)

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
        "manifest_total": len(manifest),
        "total": len(rows),
        "results_total": len(rows),
        "graded": len(graded),
        "excluded_infra": len(rows) - len(graded),
        "solved": solved,
        "solve_rate": (solved / len(graded)) if graded else 0.0,
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
