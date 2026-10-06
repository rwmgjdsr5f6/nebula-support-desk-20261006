"""reopen 命令的回归测试。

通过真实命令行入口（python main.py）核对退出码、标准输出与标准错误。
每个用例在独立临时数据库中准备两条合成工单（仅第一条结案），
测试结束后清理，不读取或改动用户已有数据库。

运行方式（项目根目录）：
    python -m unittest discover -s tests
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = PROJECT_ROOT / "main.py"

TICKET_A = {"title": "登录失败", "description": "重置密码后仍无法登录"}
TICKET_B = {"title": "打印异常", "description": ""}


class ReopenCommandTestCase(unittest.TestCase):
    """每个用例使用独立临时数据库，预置两条工单，仅第一条已结案。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.id_a = self._create_ticket(TICKET_A["title"], TICKET_A["description"])
        self.id_b = self._create_ticket(TICKET_B["title"], TICKET_B["description"])

        # 仅将第一条工单结案，作为 reopen 的作用对象
        result = self.run_cli("close", str(self.id_a))
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")

    # ---------- 辅助方法 ----------

    def run_cli(self, *args):
        """以真实命令行入口运行 main.py，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def _create_ticket(self, title, description):
        result = self.run_cli(
            "create", "--title", title, "--description", description
        )
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)["id"]

    def show_ticket(self, ticket_id):
        result = self.run_cli("show", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"show 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def ticket_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        finally:
            conn.close()

    def expected_ticket_a(self, status):
        return {
            "id": self.id_a,
            "title": TICKET_A["title"],
            "description": TICKET_A["description"],
            "status": status,
        }

    def expected_ticket_b(self, status="open"):
        return {
            "id": self.id_b,
            "title": TICKET_B["title"],
            "description": TICKET_B["description"],
            "status": status,
        }

    def assert_sample_tickets_untouched(self):
        """两条样例工单保持初始状态（A 已结案、B 未结案），总数仍为 2。"""
        self.assertEqual(self.show_ticket(self.id_a), self.expected_ticket_a("closed"))
        self.assertEqual(self.show_ticket(self.id_b), self.expected_ticket_b())
        self.assertEqual(self.ticket_count(), 2)

    # ---------- 正常流程 ----------

    def test_reopen_success_updates_only_status(self):
        before = self.show_ticket(self.id_a)
        self.assertEqual(before, self.expected_ticket_a("closed"))

        result = self.run_cli("reopen", str(self.id_a))

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        # 标准输出只有一个 JSON 对象，结构与字段类型同 show 一致
        reopened = json.loads(result.stdout)
        self.assertIsInstance(reopened, dict)
        self.assertEqual(
            {k: type(v) for k, v in reopened.items()},
            {k: type(v) for k, v in before.items()},
        )
        # 仅状态从 closed 变为 open，编号、标题、描述保持原值
        expected = dict(before, status="open")
        self.assertEqual(reopened, expected)

        # 重新打开后再用 show 查看，证明 open 状态已保存
        self.assertEqual(self.show_ticket(self.id_a), expected)
        # 第二条工单仍为 open 且内容不变
        self.assertEqual(self.show_ticket(self.id_b), self.expected_ticket_b())
        self.assertEqual(self.ticket_count(), 2)

    def test_reopen_is_idempotent(self):
        first = self.run_cli("reopen", str(self.id_a))
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stderr, "")

        # 对恢复后的工单再次 reopen 仍成功，返回同一工单
        second = self.run_cli("reopen", str(self.id_a))
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stderr, "")
        self.assertEqual(json.loads(second.stdout), json.loads(first.stdout))

        # 不增加工单数量，重新查看得到相同的 open 状态
        self.assertEqual(self.ticket_count(), 2)
        self.assertEqual(self.show_ticket(self.id_a), json.loads(first.stdout))
        # 第二条工单不受影响
        self.assertEqual(self.show_ticket(self.id_b), self.expected_ticket_b())

    def test_reopen_open_ticket_returns_it_unchanged(self):
        # 第二条工单从未结案，reopen 仍成功返回其 open 状态
        result = self.run_cli("reopen", str(self.id_b))

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), self.expected_ticket_b())

        # 不增加记录，两条工单状态保持原样
        self.assertEqual(self.ticket_count(), 2)
        self.assertEqual(self.show_ticket(self.id_a), self.expected_ticket_a("closed"))
        self.assertEqual(self.show_ticket(self.id_b), self.expected_ticket_b())

    # ---------- 编号格式错误 ----------

    def test_reopen_rejects_non_positive_integer_ids(self):
        for bad_id in ("abc", "0", "-1"):
            with self.subTest(bad_id=bad_id):
                result = self.run_cli("reopen", bad_id)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单编号必须为正整数\n")
                # 失败后已有工单及数量保持原样
                self.assert_sample_tickets_untouched()

    # ---------- 编号不存在 ----------

    def test_reopen_reports_missing_ticket(self):
        result = self.run_cli("reopen", "999")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")
        # 失败后已有工单及数量保持原样
        self.assert_sample_tickets_untouched()

    # ---------- 参数用法错误 ----------

    def test_reopen_usage_errors_exit_with_code_2(self):
        for argv in (("reopen",), ("reopen", str(self.id_a), "--unknown")):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage", result.stderr.lower())
                # 失败后已有工单及数量保持原样
                self.assert_sample_tickets_untouched()


if __name__ == "__main__":
    unittest.main()
