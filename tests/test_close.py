"""close 命令的回归测试。

通过真实命令行入口（python main.py）核对退出码、标准输出与标准错误。
每个用例在独立临时数据库中准备两条合成工单，测试结束后清理，
不读取或改动用户已有数据库。

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
TICKET_B = {"title": "打印异常", "description": "更换网络后无法打印"}


class CloseCommandTestCase(unittest.TestCase):
    """每个用例使用独立临时数据库，预置两条 open 状态的工单。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.id_a = self._create_ticket(TICKET_A["title"], TICKET_A["description"])
        self.id_b = self._create_ticket(TICKET_B["title"], TICKET_B["description"])

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

    def assert_sample_tickets_untouched(self):
        """两条样例工单保持 open 且内容不变，总数仍为 2。"""
        self.assertEqual(
            self.show_ticket(self.id_a),
            {
                "id": self.id_a,
                "title": TICKET_A["title"],
                "description": TICKET_A["description"],
                "status": "open",
            },
        )
        self.assertEqual(
            self.show_ticket(self.id_b),
            {
                "id": self.id_b,
                "title": TICKET_B["title"],
                "description": TICKET_B["description"],
                "status": "open",
            },
        )
        self.assertEqual(self.ticket_count(), 2)

    # ---------- 正常流程 ----------

    def test_close_success_updates_only_status(self):
        before = self.show_ticket(self.id_a)

        result = self.run_cli("close", str(self.id_a))

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        # 标准输出只有一个 JSON 对象，结构与字段类型同 show 一致
        closed = json.loads(result.stdout)
        self.assertIsInstance(closed, dict)
        self.assertEqual(
            {k: type(v) for k, v in closed.items()},
            {k: type(v) for k, v in before.items()},
        )
        # 仅状态变为 closed，其余字段保持不变
        expected = dict(before, status="closed")
        self.assertEqual(closed, expected)

        # 结案后再用 show 查看，证明 closed 状态已保存
        self.assertEqual(self.show_ticket(self.id_a), expected)
        # 第二条工单仍为 open 且内容不变
        self.assertEqual(
            self.show_ticket(self.id_b),
            {
                "id": self.id_b,
                "title": TICKET_B["title"],
                "description": TICKET_B["description"],
                "status": "open",
            },
        )
        self.assertEqual(self.ticket_count(), 2)

    def test_close_is_idempotent(self):
        first = self.run_cli("close", str(self.id_a))
        self.assertEqual(first.returncode, 0)

        # 再次结案同一编号仍成功，返回同一工单
        second = self.run_cli("close", str(self.id_a))
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stderr, "")
        self.assertEqual(second.stdout, first.stdout)

        # 不增加工单数量，重新查看得到相同结果
        self.assertEqual(self.ticket_count(), 2)
        self.assertEqual(self.show_ticket(self.id_a), json.loads(first.stdout))
        # 第二条工单不受影响
        self.assertEqual(self.show_ticket(self.id_b)["status"], "open")

    # ---------- 编号格式错误 ----------

    def test_close_rejects_non_positive_integer_ids(self):
        for bad_id in ("abc", "0", "-1"):
            with self.subTest(bad_id=bad_id):
                result = self.run_cli("close", bad_id)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单编号必须为正整数\n")
                # 失败后已有工单及数量保持原样
                self.assert_sample_tickets_untouched()

    # ---------- 编号不存在 ----------

    def test_close_reports_missing_ticket(self):
        result = self.run_cli("close", "999")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")
        # 失败后已有工单及数量保持原样
        self.assert_sample_tickets_untouched()

    # ---------- 参数用法错误 ----------

    def test_close_usage_errors_exit_with_code_2(self):
        for argv in (("close",), ("close", str(self.id_a), "--unknown")):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage", result.stderr.lower())
                # 失败后已有工单及数量保持原样
                self.assert_sample_tickets_untouched()


if __name__ == "__main__":
    unittest.main()
