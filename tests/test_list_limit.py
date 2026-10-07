"""list --limit 返回数量控制的回归测试。

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

TICKET_2 = {"id": 2, "title": "登录失败", "description": "", "status": "open"}
TICKET_3 = {"id": 3, "title": "登录超时", "description": "", "status": "open"}


class ListLimitTestCase(unittest.TestCase):
    """固定三张样例工单上的 list --limit 行为。"""

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
        """数量用法错误：退出 2、标准输出为空、标准错误含用法提示及 --limit。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 2, f"argv={argv!r}: {result.stderr}")
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assertIn("--limit", result.stderr)
        return result

    # ---------- 需求验收主流程 ----------

    def test_acceptance_limit_one_returns_only_ticket_2(self):
        tickets = self.list_tickets(*ACCEPTANCE_QUERY, "--limit", "1")
        # 匹配集合为编号 2、3，取编号最小的一张即完整的编号 2
        self.assertEqual(tickets, [TICKET_2])
        self.assertEqual(tickets[0]["title"], "登录失败")
        self.assertEqual(tickets[0]["description"], "")
        self.assertEqual(tickets[0]["status"], "open")
        for ticket in tickets:
            self.assertTicketShape(ticket)

    def test_acceptance_limit_five_returns_tickets_2_and_3(self):
        tickets = self.list_tickets(*ACCEPTANCE_QUERY, "--limit", "5")
        # 数量超过匹配数时返回全部匹配项，不补齐空位
        self.assertEqual(tickets, [TICKET_2, TICKET_3])
        self.assertEqual([t["id"] for t in tickets], [2, 3])
        for ticket in tickets:
            self.assertTicketShape(ticket)

    def test_acceptance_without_limit_returns_tickets_2_and_3(self):
        tickets = self.list_tickets(*ACCEPTANCE_QUERY)
        self.assertEqual(tickets, [TICKET_2, TICKET_3])

    # ---------- 取交集后再截取，编号整数升序 ----------

    def test_limit_applies_after_intersection_in_id_order(self):
        # 无条件全量列表：编号最小的两张为 1、2（编号 1 不参与备注查询）
        self.assertEqual(
            [t["id"] for t in self.list_tickets("--limit", "2")], [1, 2]
        )
        # 仅文本条件匹配编号 2、3：limit 1 取 2，limit 2 取 2、3
        self.assertEqual(
            [t["id"] for t in self.list_tickets("--keyword", "登录", "--limit", "1")],
            [2],
        )
        self.assertEqual(
            [t["id"] for t in self.list_tickets("--keyword", "登录", "--limit", "2")],
            [2, 3],
        )
        # 备注条件同样匹配编号 2、3（编号 2 的两条备注命中只计一次）
        self.assertEqual(
            [
                t["id"]
                for t in self.list_tickets("--note-keyword", "等待确认", "--limit", "1")
            ],
            [2],
        )

    # ---------- 数量解析：Python int() 规则 ----------

    def test_limit_accepts_plus_leading_zeros_and_surrounding_whitespace(self):
        for raw in ("+1", "01", "0001", "  1  ", "\t1\n", "+0000001", "１２"):
            with self.subTest(raw=raw):
                tickets = self.list_tickets(*ACCEPTANCE_QUERY, "--limit", raw)
                # 以上写法均解析为正整数：前五个为 1（只留编号 2），
                # 全角数字“１２”解析为 12（超过匹配数，返回 2、3）
                expected = [TICKET_2] if int(raw) == 1 else [TICKET_2, TICKET_3]
                self.assertEqual(tickets, expected)

    def test_limit_larger_than_match_count_returns_all_without_padding(self):
        for raw in ("3", "5", "100"):
            with self.subTest(raw=raw):
                tickets = self.list_tickets(*ACCEPTANCE_QUERY, "--limit", raw)
                self.assertEqual(tickets, [TICKET_2, TICKET_3])
                self.assertEqual(len(tickets), 2)

    def test_limit_beyond_sqlite_max_int_still_succeeds(self):
        # 即使超过 SQLite 有符号整数上限，也按“返回全部匹配项”语义成功处理
        huge = str(SQLITE_MAX_INT + 1)
        tickets = self.list_tickets(*ACCEPTANCE_QUERY, "--limit", huge)
        self.assertEqual(tickets, [TICKET_2, TICKET_3])
        # 全量列表配超大数量同样成功
        all_tickets = self.list_tickets("--limit", "9" * 40)
        self.assertEqual([t["id"] for t in all_tickets], [1, 2, 3])

    # ---------- 空结果 ----------

    def test_limit_with_empty_result_still_returns_empty_array(self):
        for argv in (
            ("list", "--keyword", "不存在的内容", "--limit", "1"),
            ("list", "--status", "closed", "--limit", "1"),
            ("list", *ACCEPTANCE_QUERY, "--note-keyword", "肯定不存在", "--limit", "5"),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(
                    self.parse_single_json_array(result.stdout), []
                )
                self.assertEqual(result.stdout.strip(), "[]")

    def test_limit_on_empty_database_returns_empty_array(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = str(Path(tmpdir.name) / "empty.sqlite")

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, str(MAIN_PY), "--db", db_path, *args],
                capture_output=True,
                text=True,
            )

        for raw in ("1", "5", str(SQLITE_MAX_INT + 1)):
            with self.subTest(raw=raw):
                result = run_cli("list", "--limit", raw)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout.strip(), "[]")

    # ---------- 数量无效：退出 2 ----------

    def test_invalid_limit_values_exit_2(self):
        for raw in ("0", "-1", "-0", "1.5", "0.0", "abc", "1e3", "", "   ", "\t \n"):
            with self.subTest(raw=raw):
                self.assert_usage_error(("list", *ACCEPTANCE_QUERY, "--limit", raw))
                # 用法错误不改动任何存量数据
                self.assertEqual(self.dump_storage(), self._expected_storage())

    def test_limit_missing_value_exits_2(self):
        self.assert_usage_error(("list", *ACCEPTANCE_QUERY, "--limit"))
        self.assertEqual(self.dump_storage(), self._expected_storage())

    # ---------- 与空白关键字同时出现时的优先级 ----------

    def test_valid_limit_with_blank_keyword_still_exits_1(self):
        # 有效数量配合空白关键字：保留退出 1、空 stdout、固定 stderr
        for blank in ("", "   ", "\t \n"):
            with self.subTest(blank=blank):
                result = self.run_cli(
                    "list", *ACCEPTANCE_QUERY, "--keyword", blank, "--limit", "1"
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "关键字不能为空\n")

                result = self.run_cli(
                    "list", *ACCEPTANCE_QUERY, "--note-keyword", blank, "--limit", "1"
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "关键字不能为空\n")

    def test_invalid_limit_takes_precedence_over_blank_keyword(self):
        # 数量无效时优先返回用法错误（argparse 在命令逻辑之前拒绝）
        for raw in ("0", "-1", "abc", ""):
            with self.subTest(raw=raw):
                self.assert_usage_error(
                    ("list", "--keyword", "   ", "--limit", raw)
                )
                self.assert_usage_error(
                    ("list", "--note-keyword", "   ", "--limit", raw)
                )

    # ---------- 只读与可重复性 ----------

    def test_limit_queries_are_repeatable_and_read_only(self):
        before = self.dump_storage()

        first = self.list_tickets(*ACCEPTANCE_QUERY, "--limit", "1")
        second = self.list_tickets(*ACCEPTANCE_QUERY, "--limit", "1")
        self.assertEqual(first, [TICKET_2])
        self.assertEqual(second, first)

        for argv in (
            ("list", *ACCEPTANCE_QUERY, "--limit", "1"),
            ("list", *ACCEPTANCE_QUERY, "--limit", "5"),
            ("list", *ACCEPTANCE_QUERY, "--limit", str(SQLITE_MAX_INT + 1)),
            ("list", *ACCEPTANCE_QUERY, "--limit", "0"),
            ("list", *ACCEPTANCE_QUERY, "--limit", "abc"),
            ("list", *ACCEPTANCE_QUERY, "--limit"),
            ("list", "--keyword", "不存在", "--limit", "1"),
        ):
            self.run_cli(*argv)
            # 成功、空结果及失败查询都不改动工单、备注与状态历史
            self.assertEqual(self.dump_storage(), before)

        # 重新运行相同查询结果一致
        self.assertEqual(
            self.list_tickets(*ACCEPTANCE_QUERY, "--limit", "5"),
            [TICKET_2, TICKET_3],
        )
        self.assertEqual(
            self.list_tickets(*ACCEPTANCE_QUERY), [TICKET_2, TICKET_3]
        )


if __name__ == "__main__":
    unittest.main()
