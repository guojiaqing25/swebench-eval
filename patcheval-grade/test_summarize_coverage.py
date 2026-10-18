"""summarize_coverage.py 的回归测试（标准库 unittest，无第三方依赖）。

为什么需要：这个脚本直接决定「分母」。分母算错，通过率就是错的，而且错法很隐蔽——
少几条 case 谁都看不出来。改它的时候必须能本地跑一遍，不能靠「跑一次 CI 看看」。

跑法:
  python3 -m unittest discover -s patcheval-grade -p "test_*.py" -v
  # 或者
  python3 patcheval-grade/test_summarize_coverage.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "summarize_coverage.py")


def write_result(art_root: str, artifact: str, case_id: str, status: str) -> None:
    d = os.path.join(art_root, artifact, "results")
    os.makedirs(d, exist_ok=True)
    payload = {"case_id": case_id, "status": status,
               "solved": status == "solved", "detail": f"{case_id} {status}"}
    with open(os.path.join(d, case_id + ".json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


class SummarizeCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.art = os.path.join(self.root, "artifacts")
        self.manifest = os.path.join(self.root, "manifest.json")
        self.out = os.path.join(self.root, "summary.json")
        with open(self.manifest, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {"case_id": "CVE-1", "language": "python", "cwe_id": "CWE-20"},
                    {"case_id": "CVE-2", "language": "javascript", "cwe_id": "CWE-79"},
                    {"case_id": "CVE-3", "language": "python", "cwe_id": "CWE-20"},
                ],
                fh,
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_summary(self, *extra: str) -> tuple[int, dict]:
        cmd = [sys.executable, SCRIPT, "--artifacts", self.art,
               "--manifest", self.manifest, "--subset",
               os.path.join(self.root, "none.txt"), "--out", self.out, *extra]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        data = {}
        if os.path.exists(self.out):
            with open(self.out, encoding="utf-8") as fh:
                data = json.load(fh)
        return proc.returncode, data

    def test_full_run_counts_everything(self) -> None:
        write_result(self.art, "cov-CVE-1", "CVE-1", "solved")
        write_result(self.art, "cov-CVE-2", "CVE-2", "test_failed")
        write_result(self.art, "cov-CVE-3", "CVE-3", "no_patch")
        rc, d = self.run_summary()
        self.assertEqual(rc, 0)
        self.assertEqual(d["manifest_total"], 3)
        self.assertEqual(d["graded"], 3)
        self.assertEqual(d["solved"], 1)
        self.assertEqual(d["missing"], [])
        self.assertFalse(d["scoped"])

    def test_missing_result_fails_the_run(self) -> None:
        """少一条结果必须让 job 变红，而不是安静地给出虚高的通过率。"""
        write_result(self.art, "cov-CVE-1", "CVE-1", "solved")
        write_result(self.art, "cov-CVE-2", "CVE-2", "solved")
        rc, d = self.run_summary()
        self.assertEqual(rc, 1)
        self.assertEqual(d["missing"], ["CVE-3"])
        # 分母没缩水：通过率按计划条数算，不是按拿到的条数算
        self.assertEqual(d["manifest_total"], 3)

    def test_infra_error_is_excluded_from_denominator(self) -> None:
        write_result(self.art, "cov-CVE-1", "CVE-1", "solved")
        write_result(self.art, "cov-CVE-2", "CVE-2", "infra_error")
        write_result(self.art, "cov-CVE-3", "CVE-3", "test_failed")
        rc, d = self.run_summary()
        self.assertEqual(rc, 0)
        self.assertEqual(d["excluded_infra"], 1)
        self.assertEqual(d["graded"], 2)
        self.assertAlmostEqual(d["solve_rate"], 0.5)
        self.assertEqual([r["case_id"] for r in d["infra_reasons"]], ["CVE-2"])

    def test_scoped_run_shrinks_the_denominator(self) -> None:
        """定向重跑只跑 2 条时，分母必须是 2 —— 否则剩下的会被当成静默丢失。"""
        write_result(self.art, "cov-CVE-1", "CVE-1", "solved")
        write_result(self.art, "cov-CVE-2", "CVE-2", "test_failed")
        rc, d = self.run_summary("--only", "CVE-1,CVE-2")
        self.assertEqual(rc, 0)
        self.assertTrue(d["scoped"])
        self.assertEqual(d["manifest_total"], 2)
        self.assertEqual(d["missing"], [])
        self.assertEqual(d["solved"], 1)

    def test_scoped_run_ignores_leftover_artifacts(self) -> None:
        """artifacts 里混着上一次全量跑的旧结果时，不能算进本次口径。"""
        write_result(self.art, "cov-CVE-1", "CVE-1", "solved")
        write_result(self.art, "old-run", "CVE-3", "solved")  # 上次的产物
        rc, d = self.run_summary("--only", "CVE-1")
        self.assertEqual(rc, 0)
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["solved"], 1)
        self.assertEqual([r["case_id"] for r in d["rows"]], ["CVE-1"])

    def test_only_accepts_newlines_too(self) -> None:
        write_result(self.art, "cov-CVE-1", "CVE-1", "solved")
        write_result(self.art, "cov-CVE-2", "CVE-2", "solved")
        rc, d = self.run_summary("--only", "CVE-1\nCVE-2\n")
        self.assertEqual(rc, 0)
        self.assertEqual(d["manifest_total"], 2)

    def test_unknown_case_in_only_is_an_error(self) -> None:
        """点名的 case 不在清单里 = 运行配置和清单不同源，不能静默当成「已计划缺失」。"""
        rc, _ = self.run_summary("--only", "CVE-1,CVE-NOT-EXIST")
        self.assertEqual(rc, 1)

    def test_duplicate_artifacts_do_not_inflate_total(self) -> None:
        """同一 case 出现在两个 artifact 目录里时，分母不能翻倍。"""
        write_result(self.art, "cov-CVE-1", "CVE-1", "solved")
        write_result(self.art, "cov-CVE-1-retry", "CVE-1", "solved")
        write_result(self.art, "cov-CVE-2", "CVE-2", "solved")
        write_result(self.art, "cov-CVE-3", "CVE-3", "solved")
        rc, d = self.run_summary()
        self.assertEqual(rc, 0)
        self.assertEqual(d["total"], 3)
        self.assertEqual(d["graded"], 3)

    def test_unreadable_result_is_counted_as_infra(self) -> None:
        """result.json 解析失败不能直接丢掉——丢掉就是分母静默变小。"""
        write_result(self.art, "cov-CVE-1", "CVE-1", "solved")
        write_result(self.art, "cov-CVE-2", "CVE-2", "solved")
        write_result(self.art, "cov-CVE-3", "CVE-3", "solved")
        broken = os.path.join(self.art, "cov-CVE-3", "results")
        with open(os.path.join(broken, "CVE-3.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        rc, d = self.run_summary()
        self.assertEqual(rc, 0)
        self.assertEqual(d["excluded_infra"], 1)
        self.assertTrue(d["infra_reasons"])

    def test_subset_rate_uses_only_subset(self) -> None:
        subset = os.path.join(self.root, "subset.txt")
        with open(subset, "w", encoding="utf-8") as fh:
            fh.write("CVE-1\nCVE-3\n")
        write_result(self.art, "cov-CVE-1", "CVE-1", "solved")
        write_result(self.art, "cov-CVE-2", "CVE-2", "test_failed")
        write_result(self.art, "cov-CVE-3", "CVE-3", "test_failed")
        cmd = [sys.executable, SCRIPT, "--artifacts", self.art, "--manifest", self.manifest,
               "--subset", subset, "--out", self.out]
        subprocess.run(cmd, capture_output=True, text=True)
        with open(self.out, encoding="utf-8") as fh:
            d = json.load(fh)
        self.assertEqual(d["subset58"]["graded"], 2)
        self.assertEqual(d["subset58"]["solved"], 1)
        # 整体 1/3，子集 1/2 —— 两个口径不能互相污染
        self.assertAlmostEqual(d["solve_rate"], 1 / 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
