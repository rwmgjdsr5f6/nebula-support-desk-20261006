"""list 命令的回归测试。

通过真实命令行入口（python main.py）核对退出码、标准输出与标准错误。
每个用例在独立临时数据库中准备合成工单，测试结束后清理，
不读取或改动工作目录下的默认库。

固定样例依次创建三条工单：
    1. 登录失败 / 重置密码后仍无法登录（open）
    2. 打印异常 / 更换网络后无法打印（结案后为 closed）
    3. 导出失败 / 空字符串描述（open）

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

TICKETS = [
    {"title": "登录失败", "description": "重置密码后仍无法登录", "status": "open"},
    {"title": "打印异常", "description": "更换网络后无法打印", "status": "closed"},
    {"title": "导出失败", "description": "", "status": "open"},
]
# 仅第二条工单结案
CLOSED_INDEX = 1


class ListCommandTestCase(unittest.TestCase):
    """使用固定三条样例工单的用例（第二条为 closed，其余 open）。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.ticket_ids = []
        for ticket in TICKETS:
            ticket_id = self._create_ticket(ticket["title"], ticket["description"])
            self.ticket_ids.append(ticket_id)
        self._close_ticket(self.ticket_ids[CLOSED_INDEX])

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

    def _close_ticket(self, ticket_id):
        result = self.run_cli("close", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")

    def list_tickets(self, *args):
        """执行 list 命令并将标准输出解析为唯一 JSON 数组。"""
        result = self.run_cli("list", *args)
        self.assertEqual(result.returncode, 0, f"list 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json_array(result.stdout)

    @staticmethod
    def parse_single_json_array(stdout):
        """标准输出必须恰好是一个 JSON 数组，不允许前后夹带说明文字。"""
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        assert isinstance(value, list)
        if stripped[end:].strip() != "":
            raise AssertionError(f"JSON 数组后存在额外输出: {stdout!r}")
        return value

    def expected_tickets(self, indexes):
        """按给定样例下标构造期望工单列表（编号按创建顺序）。"""
        return [
            {
                "id": self.ticket_ids[i],
                "title": TICKETS[i]["title"],
                "description": TICKETS[i]["description"],
                "status": TICKETS[i]["status"],
            }
            for i in indexes
        ]

    def assertTicketShape(self, ticket):
        """每项仅含 id/title/description/status；id 为整数，其余为字符串。"""
        self.assertIsInstance(ticket, dict)
        self.assertEqual(
            set(ticket.keys()),
            {"id", "title", "description", "status"},
        )
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(ticket["id"]), int)
        for key in ("title", "description", "status"):
            self.assertIs(type(ticket[key]), str)

    def ticket_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        finally:
            conn.close()

    def assert_storage_unchanged(self, expected):
        """存量工单的数量、编号与各字段内容保持不变。"""
        self.assertEqual(self.ticket_count(), len(expected))
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [list(row) for row in rows],
            [[t["id"], t["title"], t["description"], t["status"]] for t in expected],
        )

    # ---------- 全量列表 ----------

    def test_list_all_returns_all_tickets_in_id_order(self):
        result = self.run_cli("list")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

        tickets = self.parse_single_json_array(result.stdout)
        expected = self.expected_tickets([0, 1, 2])

        self.assertEqual(tickets, expected)
        self.assertEqual([t["id"] for t in tickets], self.ticket_ids)
        self.assertEqual(
            [t["status"] for t in tickets], ["open", "closed", "open"]
        )
        for ticket in tickets:
            self.assertTicketShape(ticket)
        # 空字符串描述原样保留
        self.assertEqual(tickets[2]["description"], "")

    # ---------- 状态筛选 ----------

    def test_list_filters_by_status(self):
        for status, indexes in (("open", [0, 2]), ("closed", [1])):
            with self.subTest(status=status):
                tickets = self.list_tickets("--status", status)
                expected = self.expected_tickets(indexes)

                self.assertEqual(tickets, expected)
                # 编号仍按整数升序
                ids = [t["id"] for t in tickets]
                self.assertEqual(ids, sorted(ids))
                self.assertTrue(all(t["status"] == status for t in tickets))
                for ticket in tickets:
                    self.assertTicketShape(ticket)

    # ---------- 空结果 ----------

    def test_list_no_matching_status_returns_empty_array(self):
        # 三条样例均存在，open 与 closed 都有工单；另建一个只有 open
        # 工单的数据库，验证没有匹配状态时成功返回 []
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = str(Path(tmpdir.name) / "test_tickets.sqlite")

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, str(MAIN_PY), "--db", db_path, *args],
                capture_output=True,
                text=True,
            )

        for title in ("登录失败", "导出失败"):
            created = run_cli("create", "--title", title, "--description", "")
            self.assertEqual(created.returncode, 0, created.stderr)

        result = run_cli("list", "--status", "closed")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.parse_single_json_array(result.stdout), [])

    # ---------- 只读与可重复性 ----------

    def test_list_is_repeatable_and_read_only(self):
        expected = self.expected_tickets([0, 1, 2])

        first = self.list_tickets()
        second = self.list_tickets()
        self.assertEqual(second, first)
        self.assertEqual(second, expected)

        # 成功查询与参数错误前后，存量数据完全一致
        for argv in (
            ("list",),
            ("list", "--status", "open"),
            ("list", "--status", "closed"),
            ("list", "--status", "pending"),
            ("list", "--status"),
            ("list", "--unknown"),
        ):
            self.run_cli(*argv)
            self.assertEqual(self.list_tickets(), expected)
            self.assert_storage_unchanged(expected)

    # ---------- 参数边界 ----------

    def test_list_argument_errors_exit_with_code_2(self):
        expected = self.expected_tickets([0, 1, 2])

        for argv in (
            ("list", "--status", "pending"),
            ("list", "--status"),
            ("list", "--unknown"),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage", result.stderr.lower())
                # 参数错误不改动任何存量工单
                self.assertEqual(self.list_tickets(), expected)
                self.assert_storage_unchanged(expected)


class EmptyDatabaseListTestCase(unittest.TestCase):
    """空库上的 list 查询。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def test_empty_database_list_returns_empty_array(self):
        for argv in (
            ("list",),
            ("list", "--status", "open"),
            ("list", "--status", "closed"),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                # 只有一个空 JSON 数组，无任何说明文字
                self.assertEqual(result.stdout.strip(), "[]")
                self.assertEqual(json.loads(result.stdout), [])


if __name__ == "__main__":
    unittest.main()
