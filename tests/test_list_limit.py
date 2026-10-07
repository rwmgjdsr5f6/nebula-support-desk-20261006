"""list --limit 数量选项的回归测试。

通过真实命令行入口（python main.py）核对退出码、标准输出与标准错误。
每个用例在独立临时数据库中准备合成工单，测试结束后清理，
不读取或改动工作目录下的默认库。

固定样例即需求验收中的三张合成工单（描述均为空字符串，状态均为 open）：
    1. 打印异常（无备注）
    2. 登录失败（两条备注均含“等待确认”）
    3. 登录超时（一条备注含“等待确认”）

三条件查询 list --status open --keyword 登录 --note-keyword 等待确认
命中编号 2 与 3：--limit 1 只返回编号 2，--limit 5 返回 2 和 3，
不传 --limit 时仍返回 2 和 3。

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

# 超过 SQLite 有符号整数上限的数量，仍应按“返回全部匹配项”成功处理
HUGE_LIMIT = str(9223372036854775807 + 1)


class LimitListTestCase(unittest.TestCase):
    """需求验收样例上的 --limit 行为。"""

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

    # 三条件查询可能返回的工单（字面量期望）
    TICKET_2 = {"id": 2, "title": "登录失败", "description": "", "status": "open"}
    TICKET_3 = {"id": 3, "title": "登录超时", "description": "", "status": "open"}

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        # 依次创建打印异常、登录失败、登录超时，描述均为空，状态均为 open
        ticket_ids = []
        for title in ("打印异常", "登录失败", "登录超时"):
            created = self.run_cli(
                "create", "--title", title, "--description", ""
            )
            self.assertEqual(created.returncode, 0, f"准备样例失败: {created.stderr}")
            self.assertEqual(created.stderr, "")
            ticket_ids.append(json.loads(created.stdout)["id"])
        self.assertEqual(ticket_ids, [1, 2, 3])

        # 编号 2 有两条命中“等待确认”的备注，编号 3 有一条
        for ticket_id, text in (
            ("2", "已联系用户，等待确认"),
            ("2", "再次等待确认结果"),
            ("3", "等待确认"),
        ):
            noted = self.run_cli("add-note", ticket_id, "--text", text)
            self.assertEqual(noted.returncode, 0, f"准备样例失败: {noted.stderr}")
            self.assertEqual(noted.stderr, "")

        # 准备完成后三张表即处于期望状态
        self.assertEqual(self.dump_storage(), self._expected_storage())

    # ---------- 辅助方法 ----------

    def run_cli(self, *args):
        """以真实命令行入口运行 main.py，准备与查询均显式指定临时 --db。"""
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def list_tickets(self, *args):
        """执行 list 命令：退出码 0、标准错误为空，输出解析为唯一 JSON 数组。"""
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
            [list(row) for row in self.EXPECTED_TICKETS],
            [list(row) for row in self.EXPECTED_NOTES],
            [list(row) for row in self.EXPECTED_HISTORY],
        )

    def assertTicketShape(self, ticket):
        """与 show 相同的四字段：id 为整数，其余为字符串；不含总数或分页信息。"""
        self.assertIsInstance(ticket, dict)
        self.assertEqual(
            set(ticket.keys()), {"id", "title", "description", "status"}
        )
        self.assertIs(type(ticket["id"]), int)
        for key in ("title", "description", "status"):
            self.assertIs(type(ticket[key]), str)

    # ---------- 需求验收主流程 ----------

    def test_acceptance_limit_1_returns_only_ticket_2(self):
        tickets = self.list_tickets(
            "--status", "open",
            "--keyword", "登录",
            "--note-keyword", "等待确认",
            "--limit", "1",
        )
        self.assertEqual(tickets, [self.TICKET_2])
        for ticket in tickets:
            self.assertTicketShape(ticket)

    def test_acceptance_limit_5_returns_tickets_2_and_3(self):
        tickets = self.list_tickets(
            "--status", "open",
            "--keyword", "登录",
            "--note-keyword", "等待确认",
            "--limit", "5",
        )
        self.assertEqual(tickets, [self.TICKET_2, self.TICKET_3])
        for ticket in tickets:
            self.assertTicketShape(ticket)

    def test_acceptance_without_limit_returns_tickets_2_and_3(self):
        tickets = self.list_tickets(
            "--status", "open",
            "--keyword", "登录",
            "--note-keyword", "等待确认",
        )
        self.assertEqual(tickets, [self.TICKET_2, self.TICKET_3])

    # ---------- 截取语义 ----------

    def test_limit_returns_lowest_ids_first(self):
        # 不带筛选条件时三张工单全部命中，--limit 2 取编号最小的两张
        tickets = self.list_tickets("--limit", "2")
        self.assertEqual(
            tickets,
            [
                {"id": 1, "title": "打印异常", "description": "", "status": "open"},
                self.TICKET_2,
            ],
        )

    def test_limit_larger_than_matches_returns_all(self):
        # 数量等于与超过匹配数时都返回全部匹配项，不补齐空位
        self.assertEqual(
            self.list_tickets("--limit", "3"),
            self.list_tickets("--limit", "100"),
        )
        self.assertEqual(len(self.list_tickets("--limit", "100")), 3)

    def test_limit_beyond_sqlite_max_int_succeeds(self):
        # 数量超过 SQLite 有符号整数上限仍按“返回全部匹配项”成功处理
        result = self.run_cli("list", "--limit", HUGE_LIMIT)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(self.parse_single_json_array(result.stdout)), 3)

    def test_limit_with_no_match_returns_empty_array(self):
        result = self.run_cli("list", "--keyword", "不存在的内容", "--limit", "2")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), "[]")

    # ---------- 数量解析：Python int() 规则 ----------

    def test_limit_accepts_plus_sign_leading_zeros_and_whitespace(self):
        for raw in ("+1", "01", " 1 ", "\t1\n", "+01"):
            with self.subTest(raw=raw):
                tickets = self.list_tickets(
                    "--status", "open",
                    "--keyword", "登录",
                    "--note-keyword", "等待确认",
                    "--limit", raw,
                )
                self.assertEqual(tickets, [self.TICKET_2])

    # ---------- 无效数量：退出码 2 ----------

    def test_invalid_limit_exits_2_with_usage(self):
        for raw in ("0", "-1", "+0", "1.5", "abc", "", "   ", "1e2"):
            with self.subTest(raw=raw):
                result = self.run_cli("list", "--limit", raw)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage", result.stderr.lower())
                self.assertIn("--limit", result.stderr)
                # 参数错误不改动任何存量数据
                self.assertEqual(self.dump_storage(), self._expected_storage())

    def test_limit_missing_value_exits_2(self):
        result = self.run_cli("list", "--limit")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assertIn("--limit", result.stderr)
        self.assertEqual(self.dump_storage(), self._expected_storage())

    # ---------- 与空白关键字的优先级 ----------

    def test_valid_limit_with_blank_keyword_exits_1(self):
        # 有效数量配合空白关键字：保留退出码 1 与固定提示
        result = self.run_cli(
            "list", "--keyword", "   ", "--limit", "1"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "关键字不能为空\n")
        self.assertEqual(self.dump_storage(), self._expected_storage())

    def test_invalid_limit_beats_blank_keyword_with_usage_error(self):
        # 数量无效时优先返回用法错误（退出码 2），而非关键字错误
        result = self.run_cli(
            "list", "--keyword", "   ", "--limit", "0"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assertIn("--limit", result.stderr)
        self.assertEqual(self.dump_storage(), self._expected_storage())

    # ---------- 只读与可重复性 ----------

    def test_limit_queries_are_repeatable_and_read_only(self):
        before = self.dump_storage()
        acceptance = (
            "--status", "open", "--keyword", "登录",
            "--note-keyword", "等待确认",
        )

        # 成功、空结果与失败查询前后，三张表都保持不变
        for argv in (
            ("list", *acceptance, "--limit", "1"),
            ("list", *acceptance, "--limit", "5"),
            ("list", *acceptance),
            ("list", "--keyword", "不存在", "--limit", "2"),
            ("list", "--limit", HUGE_LIMIT),
            ("list", "--limit", "0"),
            ("list", "--limit", "abc"),
            ("list", "--limit"),
            ("list", "--keyword", "", "--limit", "1"),
        ):
            self.run_cli(*argv)
            self.assertEqual(self.dump_storage(), before)

        # 同库重复查询得到相同数据
        self.assertEqual(
            self.list_tickets(*acceptance, "--limit", "1"), [self.TICKET_2]
        )
        self.assertEqual(
            self.list_tickets(*acceptance, "--limit", "1"), [self.TICKET_2]
        )
        self.assertEqual(
            self.list_tickets(*acceptance, "--limit", "5"),
            [self.TICKET_2, self.TICKET_3],
        )
        self.assertEqual(
            self.list_tickets(*acceptance, "--limit", "5"),
            [self.TICKET_2, self.TICKET_3],
        )


class LimitEmptyDatabaseTestCase(unittest.TestCase):
    """空库上的 --limit 查询。"""

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

    def test_empty_database_with_limit_returns_empty_array(self):
        for argv in (
            ("list", "--limit", "1"),
            ("list", "--limit", HUGE_LIMIT),
            ("list", "--status", "open", "--keyword", "登录", "--limit", "3"),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout.strip(), "[]")
                self.assertEqual(json.loads(result.stdout), [])


if __name__ == "__main__":
    unittest.main()
