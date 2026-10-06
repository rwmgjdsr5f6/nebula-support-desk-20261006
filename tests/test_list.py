"""list 命令的回归测试。

通过真实命令行入口（python main.py）核对退出码、标准输出与标准错误。
每个用例在独立临时数据库中准备三条合成工单（仅第二条结案），
测试结束后清理，不读取或改动用户已有数据库，也不使用工作目录下的默认库。

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
TICKET_C = {"title": "导出失败", "description": ""}


class ListCommandTestCase(unittest.TestCase):
    """每个用例使用独立临时数据库，预置三条工单，仅第二条为 closed。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.id_a = self._create_ticket(TICKET_A["title"], TICKET_A["description"])
        self.id_b = self._create_ticket(TICKET_B["title"], TICKET_B["description"])
        self.id_c = self._create_ticket(TICKET_C["title"], TICKET_C["description"])
        # 仅将第二条工单结案
        self._close_ticket(self.id_b)

    # ---------- 辅助方法 ----------

    def run_cli(self, *args, db_path=None):
        """以真实命令行入口运行 main.py，返回 CompletedProcess。"""
        return subprocess.run(
            [
                sys.executable,
                str(MAIN_PY),
                "--db",
                db_path if db_path is not None else self.db_path,
                *args,
            ],
            capture_output=True,
            text=True,
        )

    def run_list(self, *extra_args):
        return self.run_cli("list", *extra_args)

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

    def expected_tickets(self):
        """按编号升序的三条样例工单的期望数据。"""
        return [
            {
                "id": self.id_a,
                "title": TICKET_A["title"],
                "description": TICKET_A["description"],
                "status": "open",
            },
            {
                "id": self.id_b,
                "title": TICKET_B["title"],
                "description": TICKET_B["description"],
                "status": "closed",
            },
            {
                "id": self.id_c,
                "title": TICKET_C["title"],
                "description": TICKET_C["description"],
                "status": "open",
            },
        ]

    def assert_sample_tickets_untouched(self, expected=None):
        """三条样例工单的编号、标题、描述、状态及总数均保持不变。"""
        if expected is None:
            expected = self.expected_tickets()
        for ticket in expected:
            self.assertEqual(self.show_ticket(ticket["id"]), ticket)
        self.assertEqual(self.ticket_count(), len(expected))

    @staticmethod
    def parse_single_json_array(stdout):
        """断言标准输出只含一个 JSON 值且为数组，返回解析后的列表。

        使用 raw_decode 消费整段输出，确保 JSON 之后没有额外说明文字；
        不依赖键序或格式空格。
        """
        decoder = json.JSONDecoder()
        data, end = decoder.raw_decode(stdout)
        # JSON 之后只允许空白，不得有额外输出
        assert stdout[end:].strip() == "", f"JSON 之后存在额外输出: {stdout[end:]!r}"
        assert isinstance(data, list), f"期望 JSON 数组，实际为: {type(data)!r}"
        return data

    @staticmethod
    def assert_ticket_shape(item):
        """每项仅含四个字段，id 为整数，其余为字符串。"""
        assert isinstance(item, dict)
        assert set(item.keys()) == {"id", "title", "description", "status"}
        # bool 是 int 的子类，需排除
        assert type(item["id"]) is int
        for key in ("title", "description", "status"):
            assert type(item[key]) is str, f"{key} 应为字符串"

    # ---------- 正常流程：列出全部 ----------

    def test_list_returns_all_tickets_in_id_order(self):
        result = self.run_list()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

        tickets = self.parse_single_json_array(result.stdout)
        self.assertEqual(len(tickets), 3)
        for ticket in tickets:
            self.assert_ticket_shape(ticket)

        # 按整数编号升序排列
        ids = [ticket["id"] for ticket in tickets]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, [self.id_a, self.id_b, self.id_c])

        # 内容对应创建结果，状态依次为 open、closed、open（含空描述）
        self.assertEqual(tickets, self.expected_tickets())

    # ---------- 按状态筛选 ----------

    def test_list_filters_open_tickets(self):
        result = self.run_list("--status", "open")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

        tickets = self.parse_single_json_array(result.stdout)
        for ticket in tickets:
            self.assert_ticket_shape(ticket)
        # 仅第一、第三条，且保持编号升序
        self.assertEqual(
            tickets,
            [ticket for ticket in self.expected_tickets() if ticket["status"] == "open"],
        )
        self.assertEqual([t["id"] for t in tickets], [self.id_a, self.id_c])

    def test_list_filters_closed_tickets(self):
        result = self.run_list("--status", "closed")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

        tickets = self.parse_single_json_array(result.stdout)
        for ticket in tickets:
            self.assert_ticket_shape(ticket)
        # 仅第二条
        self.assertEqual(
            tickets,
            [
                ticket
                for ticket in self.expected_tickets()
                if ticket["status"] == "closed"
            ],
        )
        self.assertEqual([t["id"] for t in tickets], [self.id_b])

    # ---------- 空库与无匹配 ----------

    def test_empty_database_and_no_match_return_empty_array(self):
        empty_db = str(Path(self._tmpdir.name) / "empty_tickets.sqlite")

        # 空库：不带筛选、按 open、按 closed 均成功返回 []
        for argv in (("list",), ("list", "--status", "open"), ("list", "--status", "closed")):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv, db_path=empty_db)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.parse_single_json_array(result.stdout), [])

        # 有数据但筛选状态无匹配时同样返回 []（样例库无 pending 概念，
        # 改为在只含 open 工单的库中筛选 closed）
        open_only_db = str(Path(self._tmpdir.name) / "open_only_tickets.sqlite")
        self.run_cli(
            "create",
            "--title",
            TICKET_A["title"],
            "--description",
            TICKET_A["description"],
            db_path=open_only_db,
        )
        result = self.run_cli("list", "--status", "closed", db_path=open_only_db)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.parse_single_json_array(result.stdout), [])

    # ---------- 参数边界 ----------

    def test_list_invalid_arguments_exit_with_code_2(self):
        expected = self.expected_tickets()
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
                # 参数错误不改变任何已有工单
                self.assert_sample_tickets_untouched(expected)

    # ---------- 只读与可重复性 ----------

    def test_list_is_read_only_and_repeatable(self):
        expected = self.expected_tickets()

        # 成功查询前后数据一致，且重复调用返回相同数据
        for argv in (
            ("list",),
            ("list", "--status", "open"),
            ("list", "--status", "closed"),
        ):
            with self.subTest(argv=argv):
                first = self.run_list(*argv[1:])
                second = self.run_list(*argv[1:])
                self.assertEqual(first.returncode, 0)
                self.assertEqual(second.returncode, 0)
                self.assertEqual(
                    self.parse_single_json_array(first.stdout),
                    self.parse_single_json_array(second.stdout),
                )
                self.assert_sample_tickets_untouched(expected)

        # 参数错误后再查询，数据与错误前完全相同
        for argv in (
            ("list", "--status", "pending"),
            ("list", "--status"),
            ("list", "--unknown"),
        ):
            self.assertEqual(self.run_cli(*argv).returncode, 2)
        self.assert_sample_tickets_untouched(expected)
        self.assertEqual(
            self.parse_single_json_array(self.run_list().stdout), expected
        )


if __name__ == "__main__":
    unittest.main()
