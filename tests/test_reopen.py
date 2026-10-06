"""reopen 命令的回归测试。

通过真实命令行入口（python main.py）核对退出码、标准输出与标准错误。
每个用例在独立临时数据库中准备两条合成工单（仅第一条已结案），
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
    """每个用例使用独立临时数据库：两条工单中仅第一条为 closed。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.id_a = self._create_ticket(TICKET_A["title"], TICKET_A["description"])
        self.id_b = self._create_ticket(TICKET_B["title"], TICKET_B["description"])
        # 仅将第一条结案，第二条保持 open
        closed = self.run_cli("close", str(self.id_a))
        self.assertEqual(closed.returncode, 0, f"准备样例失败: {closed.stderr}")
        self.assertEqual(closed.stderr, "")

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

    def snapshot_state(self):
        """记录两条工单与记录总数，供失败调用前后比对。"""
        return (
            self.show_ticket(self.id_a),
            self.show_ticket(self.id_b),
            self.ticket_count(),
        )

    def assert_state_matches(self, before):
        """两条工单的编号、标题、描述、状态及总数与调用前一致。"""
        self.assertEqual(self.snapshot_state(), before)

    # ---------- 正常流程 ----------

    def test_reopen_success_restores_open_status(self):
        # 原工单为已结案状态
        before = self.show_ticket(self.id_a)
        self.assertEqual(before["status"], "closed")

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
        # 与原工单相比仅 status 从 closed 变为 open
        expected = dict(before, status="open")
        self.assertEqual(reopened, expected)

        # 另一次独立 show 调用应读到已保存的 open 状态
        self.assertEqual(self.show_ticket(self.id_a), expected)
        # 第二条工单（描述为空字符串）保持 open 且内容不变
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

    def test_reopen_again_returns_open_without_duplicate(self):
        first = self.run_cli("reopen", str(self.id_a))
        self.assertEqual(first.returncode, 0)
        reopened = json.loads(first.stdout)
        self.assertEqual(reopened["status"], "open")

        # 对恢复后的工单再次 reopen，仍成功返回同一 open 工单
        second = self.run_cli("reopen", str(self.id_a))
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stderr, "")
        self.assertEqual(json.loads(second.stdout), reopened)

        # 不增加记录，show 读到的仍是同一 open 工单
        self.assertEqual(self.ticket_count(), 2)
        self.assertEqual(self.show_ticket(self.id_a), reopened)

    def test_reopen_never_closed_ticket_returns_open(self):
        # 第二条工单未结案，reopen 成功并返回原工单的 open 状态
        result = self.run_cli("reopen", str(self.id_b))

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {
                "id": self.id_b,
                "title": TICKET_B["title"],
                "description": TICKET_B["description"],
                "status": "open",
            },
        )
        # 不增加记录，第一条仍为 closed，第二条仍为 open
        self.assertEqual(self.ticket_count(), 2)
        self.assertEqual(self.show_ticket(self.id_a)["status"], "closed")
        self.assertEqual(self.show_ticket(self.id_b)["status"], "open")

    # ---------- 编号格式错误 ----------

    def test_reopen_rejects_non_positive_integer_ids(self):
        for bad_id in ("abc", "0", "-1"):
            with self.subTest(bad_id=bad_id):
                before = self.snapshot_state()
                result = self.run_cli("reopen", bad_id)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                # 标准错误仅有一行“工单编号必须为正整数”
                self.assertEqual(result.stderr, "工单编号必须为正整数\n")
                # 失败后两条工单及数量与调用前一致
                self.assert_state_matches(before)

    # ---------- 编号不存在 ----------

    def test_reopen_reports_missing_ticket(self):
        before = self.snapshot_state()
        result = self.run_cli("reopen", "999")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        # 标准错误仅有一行“工单不存在”
        self.assertEqual(result.stderr, "工单不存在\n")
        # 失败后两条工单及数量与调用前一致
        self.assert_state_matches(before)

    # ---------- 参数用法错误 ----------

    def test_reopen_usage_errors_exit_with_code_2(self):
        for argv in (("reopen",), ("reopen", str(self.id_a), "--unknown")):
            with self.subTest(argv=argv):
                before = self.snapshot_state()
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                # 标准错误包含用法提示，不固定提示中的路径文字
                self.assertIn("usage", result.stderr.lower())
                # 失败后两条工单及数量与调用前一致
                self.assert_state_matches(before)


if __name__ == "__main__":
    unittest.main()
