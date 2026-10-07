"""list --after-id 游标翻页的回归测试。

通过真实命令行入口（python main.py）核对退出码、标准输出与标准错误。
每个用例在独立临时数据库中准备合成工单，测试结束后清理，
不读取或改动工作目录下的默认库。

固定样例即需求验收的三张工单（描述均为空字符串，状态均为 open）：
    1. 打印异常（无备注）
    2. 登录失败（两条均含“等待确认”的备注）
    3. 登录超时（一条含“等待确认”的备注）

因此
    list --status open --keyword 登录 --note-keyword 等待确认
的匹配集合固定为编号 2、3（编号 1 不含“登录”，且无备注）。

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

# SQLite INTEGER 主键可保存的最大有符号整数
SQLITE_MAX_INT = 9223372036854775807

TICKETS = [
    {"title": "打印异常", "description": "", "status": "open"},
    {"title": "登录失败", "description": "", "status": "open"},
    {"title": "登录超时", "description": "", "status": "open"},
]
# 第二张工单两条备注均含“等待确认”，第三张有一条
NOTES = {
    2: ["已联系用户，等待确认", "再次等待确认结果"],
    3: ["等待确认"],
}

# 样例准备完成后三张表的完整存量（字面量期望）
EXPECTED_TICKETS = [
    [1, "打印异常", "", "open"],
    [2, "登录失败", "", "open"],
    [3, "登录超时", "", "open"],
]
EXPECTED_NOTES = [
    [2, 1, "已联系用户，等待确认"],
    [2, 2, "再次等待确认结果"],
    [3, 1, "等待确认"],
]
EXPECTED_HISTORY = []

ACCEPTANCE_QUERY = (
    "--status", "open", "--keyword", "登录", "--note-keyword", "等待确认"
)

TICKET_1 = {"id": 1, "title": "打印异常", "description": "", "status": "open"}
TICKET_2 = {"id": 2, "title": "登录失败", "description": "", "status": "open"}
TICKET_3 = {"id": 3, "title": "登录超时", "description": "", "status": "open"}


class ListAfterIdTestCase(unittest.TestCase):
    """固定三张样例工单上的 list --after-id 行为。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        ticket_ids = []
        for ticket in TICKETS:
            created = self.run_cli(
                "create",
                "--title",
                ticket["title"],
                "--description",
                ticket["description"],
            )
            self.assertEqual(
                created.returncode, 0, f"准备样例失败: {created.stderr}"
            )
            self.assertEqual(created.stderr, "")
            ticket_ids.append(json.loads(created.stdout)["id"])
        self.assertEqual(ticket_ids, [1, 2, 3])

        for ticket_id, texts in NOTES.items():
            for text in texts:
                noted = self.run_cli(
                    "add-note", str(ticket_id), "--text", text
                )
                self.assertEqual(noted.returncode, 0, f"准备样例失败: {noted.stderr}")
                self.assertEqual(noted.stderr, "")

        # 准备完成后三张表即处于期望状态（均为 open，无状态历史）
        self.assertEqual(self.dump_storage(), self._expected_storage())

    # ---------- 辅助方法 ----------

    def run_cli(self, *args):
        """以真实命令行入口运行 main.py，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

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

    def dump_storage(self):
        """直接读取 SQLite，返回工单、备注、状态历史三张表的全部行。"""
        conn = sqlite3.connect(self.db_path)
        try:
            tickets = [
                list(row)
                for row in conn.execute(
                    "SELECT id, title, description, status FROM tickets ORDER BY id"
                )
            ]
            notes = [
                list(row)
                for row in conn.execute(
                    "SELECT ticket_id, note_id, text FROM ticket_notes "
                    "ORDER BY ticket_id, note_id"
                )
            ]
            history = [
                list(row)
                for row in conn.execute(
                    "SELECT ticket_id, event_id, from_status, to_status "
                    "FROM ticket_history ORDER BY ticket_id, event_id"
                )
            ]
        finally:
            conn.close()
        return tickets, notes, history

    def _expected_storage(self):
        return (
            [list(row) for row in EXPECTED_TICKETS],
            [list(row) for row in EXPECTED_NOTES],
            [list(row) for row in EXPECTED_HISTORY],
        )

    def assertTicketShape(self, ticket):
        """与 show 相同的四字段：id 为整数，其余为字符串。"""
        self.assertIsInstance(ticket, dict)
        self.assertEqual(
            set(ticket.keys()), {"id", "title", "description", "status"}
        )
        # bool 虽是 int 子类但不合法
        self.assertIs(type(ticket["id"]), int)
        for key in ("title", "description", "status"):
            self.assertIs(type(ticket[key]), str)

    def assert_usage_error(self, argv):
        """游标用法错误：退出 2、标准输出为空、标准错误含用法提示及 --after-id。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 2, f"argv={argv!r}: {result.stderr}")
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assertIn("--after-id", result.stderr)
        return result

    # ---------- 需求验收主流程 ----------

    def test_acceptance_after_id_zero_with_limit_one_returns_only_ticket_2(self):
        tickets = self.list_tickets(
            *ACCEPTANCE_QUERY, "--limit", "1", "--after-id", "0"
        )
        # 游标 0 等价于未设置边界：匹配集合 2、3 中 limit 1 取编号 2
        self.assertEqual(tickets, [TICKET_2])
        self.assertEqual(tickets[0]["title"], "登录失败")
        self.assertEqual(tickets[0]["description"], "")
        self.assertEqual(tickets[0]["status"], "open")
        for ticket in tickets:
            self.assertTicketShape(ticket)

    def test_acceptance_after_id_two_with_limit_one_returns_only_ticket_3(self):
        tickets = self.list_tickets(
            *ACCEPTANCE_QUERY, "--limit", "1", "--after-id", "2"
        )
        # 用上次返回的最后一个编号 2 继续翻页：只剩编号 3
        self.assertEqual(tickets, [TICKET_3])
        self.assertEqual(tickets[0]["title"], "登录超时")
        for ticket in tickets:
            self.assertTicketShape(ticket)

    # ---------- 游标语义：严格大于、与原有条件取交集 ----------

    def test_after_id_filters_strictly_greater_in_id_order(self):
        # 无条件全量列表：游标 1 保留 2、3；游标 2 只留 3（严格大于）
        self.assertEqual(
            [t["id"] for t in self.list_tickets("--after-id", "1")], [2, 3]
        )
        self.assertEqual(
            [t["id"] for t in self.list_tickets("--after-id", "2")], [3]
        )
        # 与关键字取交集：编号 1 被关键字排除，游标 1 之后只剩 2、3
        self.assertEqual(
            [
                t["id"]
                for t in self.list_tickets(
                    "--keyword", "登录", "--after-id", "1"
                )
            ],
            [2, 3],
        )
        # 与备注条件取交集：编号 2 的两条备注命中只计一次
        self.assertEqual(
            [
                t["id"]
                for t in self.list_tickets(
                    "--note-keyword", "等待确认", "--after-id", "2"
                )
            ],
            [3],
        )

    def test_after_id_applies_before_limit(self):
        # 先按游标过滤再截取：全量列表中游标 1 配 limit 1 得编号 2 而非 1
        self.assertEqual(
            self.list_tickets("--after-id", "1", "--limit", "1"), [TICKET_2]
        )
        self.assertEqual(
            self.list_tickets("--after-id", "1", "--limit", "2"),
            [TICKET_2, TICKET_3],
        )

    def test_after_id_zero_equals_no_boundary(self):
        # “-0”同样解析为 0：非负，等价于未设置边界
        for raw in ("0", "+0", "-0", "00", "  0  "):
            with self.subTest(raw=raw):
                self.assertEqual(
                    self.list_tickets(*ACCEPTANCE_QUERY, "--after-id", raw),
                    [TICKET_2, TICKET_3],
                )
        # 与不传游标的全量列表一致
        self.assertEqual(
            self.list_tickets("--after-id", "0"),
            [TICKET_1, TICKET_2, TICKET_3],
        )

    def test_after_id_boundary_ticket_need_not_exist(self):
        # 游标只是编号边界：库中不存在编号 4，游标 4 合法且之后无记录
        result = self.run_cli("list", "--after-id", "4")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.parse_single_json_array(result.stdout), [])
        # 编号 1 被状态条件排除也不影响游标本身边界
        self.assertEqual(
            self.list_tickets("--status", "open", "--after-id", "1"),
            [TICKET_2, TICKET_3],
        )

    # ---------- 游标解析：Python int() 规则 ----------

    def test_after_id_accepts_plus_leading_zeros_and_surrounding_whitespace(self):
        for raw in ("+2", "02", "0002", "  2  ", "\t2\n", "+0000002", "２"):
            with self.subTest(raw=raw):
                # 以上写法均解析为 2：匹配集合 2、3 中只留编号 3
                tickets = self.list_tickets(*ACCEPTANCE_QUERY, "--after-id", raw)
                self.assertEqual(tickets, [TICKET_3])

    def test_after_id_beyond_sqlite_max_int_returns_empty_array(self):
        for raw in (str(SQLITE_MAX_INT + 1), "9" * 40):
            with self.subTest(raw=raw):
                result = self.run_cli("list", *ACCEPTANCE_QUERY, "--after-id", raw)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout.strip(), "[]")
        # 边界值本身仍可命中：游标 2 时保留编号 3
        self.assertEqual(
            self.list_tickets("--after-id", "2"), [TICKET_3]
        )

    # ---------- 空结果 ----------

    def test_after_id_with_no_later_records_returns_empty_array(self):
        for argv in (
            ("list", "--after-id", "3"),
            ("list", *ACCEPTANCE_QUERY, "--after-id", "3"),
            ("list", "--keyword", "不存在的内容", "--after-id", "1"),
            ("list", "--status", "closed", "--after-id", "0"),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(
                    self.parse_single_json_array(result.stdout), []
                )
                self.assertEqual(result.stdout.strip(), "[]")

    def test_after_id_on_empty_database_returns_empty_array(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = str(Path(tmpdir.name) / "empty.sqlite")

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, str(MAIN_PY), "--db", db_path, *args],
                capture_output=True,
                text=True,
            )

        for raw in ("0", "1", "5", str(SQLITE_MAX_INT + 1)):
            with self.subTest(raw=raw):
                result = run_cli("list", "--after-id", raw)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout.strip(), "[]")

    # ---------- 游标无效：退出 2 ----------

    def test_invalid_after_id_values_exit_2(self):
        for raw in ("-1", "-2", "1.5", "0.0", "abc", "1e3", "", "   ", "\t \n"):
            with self.subTest(raw=raw):
                self.assert_usage_error(("list", *ACCEPTANCE_QUERY, "--after-id", raw))
                # 用法错误不改动任何存量数据
                self.assertEqual(self.dump_storage(), self._expected_storage())

    def test_after_id_missing_value_exits_2(self):
        self.assert_usage_error(("list", *ACCEPTANCE_QUERY, "--after-id"))
        self.assertEqual(self.dump_storage(), self._expected_storage())

    # ---------- 与空白关键字同时出现时的优先级 ----------

    def test_valid_after_id_with_blank_keyword_still_exits_1(self):
        # 有效游标配合空白关键字：保留退出 1、空 stdout、固定 stderr
        for blank in ("", "   ", "\t \n"):
            with self.subTest(blank=blank):
                result = self.run_cli(
                    "list", *ACCEPTANCE_QUERY, "--keyword", blank, "--after-id", "1"
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "关键字不能为空\n")

                result = self.run_cli(
                    "list",
                    *ACCEPTANCE_QUERY,
                    "--note-keyword",
                    blank,
                    "--after-id",
                    "1",
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "关键字不能为空\n")

    def test_invalid_after_id_takes_precedence_over_blank_keyword(self):
        # 游标无效时优先返回用法错误（argparse 在命令逻辑之前拒绝）
        for raw in ("-1", "abc", ""):
            with self.subTest(raw=raw):
                self.assert_usage_error(
                    ("list", "--keyword", "   ", "--after-id", raw)
                )
                self.assert_usage_error(
                    ("list", "--note-keyword", "   ", "--after-id", raw)
                )

    # ---------- 只读与可重复性 ----------

    def test_after_id_queries_are_repeatable_and_read_only(self):
        before = self.dump_storage()

        first = self.list_tickets(*ACCEPTANCE_QUERY, "--limit", "1", "--after-id", "0")
        second = self.list_tickets(*ACCEPTANCE_QUERY, "--limit", "1", "--after-id", "0")
        self.assertEqual(first, [TICKET_2])
        self.assertEqual(second, first)

        for argv in (
            ("list", *ACCEPTANCE_QUERY, "--limit", "1", "--after-id", "0"),
            ("list", *ACCEPTANCE_QUERY, "--limit", "1", "--after-id", "2"),
            ("list", "--after-id", str(SQLITE_MAX_INT + 1)),
            ("list", "--after-id", "3"),
            ("list", "--after-id", "-1"),
            ("list", "--after-id", "abc"),
            ("list", "--after-id"),
            ("list", "--keyword", "不存在", "--after-id", "1"),
        ):
            self.run_cli(*argv)
            # 成功、空结果及失败查询都不改动工单、备注与状态历史
            self.assertEqual(self.dump_storage(), before)

        # 重新运行相同查询结果一致
        self.assertEqual(
            self.list_tickets(*ACCEPTANCE_QUERY, "--after-id", "2"),
            [TICKET_3],
        )
        self.assertEqual(
            self.list_tickets(*ACCEPTANCE_QUERY), [TICKET_2, TICKET_3]
        )


if __name__ == "__main__":
    unittest.main()
