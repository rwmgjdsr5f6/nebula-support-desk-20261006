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


class KeywordListTestCase(unittest.TestCase):
    """--keyword 关键字查询。

    固定样例即需求中的两条合成工单：
        1. 登录失败 / 重置密码无效（open）
        2. 打印异常 / 登录后无法打印（closed）
    """

    TICKETS = [
        {"title": "登录失败", "description": "重置密码无效", "status": "open"},
        {"title": "打印异常", "description": "登录后无法打印", "status": "closed"},
    ]

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.ticket_ids = []
        for ticket in self.TICKETS:
            result = self.run_cli(
                "create",
                "--title",
                ticket["title"],
                "--description",
                ticket["description"],
            )
            self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
            self.assertEqual(result.stderr, "")
            self.ticket_ids.append(json.loads(result.stdout)["id"])
        # 第二条工单结案
        closed = self.run_cli("close", str(self.ticket_ids[1]))
        self.assertEqual(closed.returncode, 0, f"准备样例失败: {closed.stderr}")
        self.assertEqual(self.ticket_ids, [1, 2])

    def run_cli(self, *args):
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
        return ListCommandTestCase.parse_single_json_array(result.stdout)

    def expected_tickets(self, indexes):
        return [
            {
                "id": self.ticket_ids[i],
                "title": self.TICKETS[i]["title"],
                "description": self.TICKETS[i]["description"],
                "status": self.TICKETS[i]["status"],
            }
            for i in indexes
        ]

    def assert_storage_unchanged(self):
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [list(row) for row in rows],
            [
                [t["id"], t["title"], t["description"], t["status"]]
                for t in self.expected_tickets([0, 1])
            ],
        )

    # ---------- 标题或描述命中 ----------

    def test_keyword_matches_title_or_description(self):
        # “登录”出现在第一条标题与第二条描述中，两条都应返回
        tickets = self.list_tickets("--keyword", "登录")
        self.assertEqual(tickets, self.expected_tickets([0, 1]))

    def test_keyword_is_stripped_before_matching(self):
        # 首尾空白被去除后再匹配；需求示例 " 登录 " 应返回两条
        tickets = self.list_tickets("--keyword", " 登录 ")
        self.assertEqual(tickets, self.expected_tickets([0, 1]))

    def test_keyword_description_only_hit(self):
        # “重置”只在第一条描述中
        tickets = self.list_tickets("--keyword", "重置")
        self.assertEqual(tickets, self.expected_tickets([0]))

    def test_keyword_title_only_hit(self):
        # “打印”只在第二条标题中
        tickets = self.list_tickets("--keyword", "打印")
        self.assertEqual(tickets, self.expected_tickets([1]))

    # ---------- 与状态筛选同时出现 ----------

    def test_keyword_and_status_are_intersected(self):
        tickets = self.list_tickets("--keyword", "登录", "--status", "closed")
        self.assertEqual(tickets, self.expected_tickets([1]))

    def test_keyword_and_status_excluding_all_returns_empty(self):
        # 文本命中两条，但只有第二条 closed；要求 open 时全部被状态排除
        tickets = self.list_tickets("--keyword", "登录", "--status", "open")
        self.assertEqual(tickets, self.expected_tickets([0]))
        # 仅在描述命中的第二条为 closed，无法通过 closed+重置 的组合
        self.assertEqual(
            self.list_tickets("--keyword", "重置", "--status", "closed"), []
        )

    # ---------- 空结果 ----------

    def test_keyword_without_hit_returns_empty_array(self):
        result = self.run_cli("list", "--keyword", "不存在的内容")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), "[]")

    # ---------- 字面子串：特殊字符不解释 ----------

    def test_keyword_special_characters_are_literal(self):
        # 另建库准备含 %、_、反斜杠、引号与内部连续空白的工单
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = str(Path(tmpdir.name) / "special.sqlite")

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, str(MAIN_PY), "--db", db_path, *args],
                capture_output=True,
                text=True,
            )

        title = '100%done_log'
        description = 'a_b\\c"q\'z  空格'
        created = run_cli(
            "create", "--title", title, "--description", description
        )
        self.assertEqual(created.returncode, 0, created.stderr)

        parse = ListCommandTestCase.parse_single_json_array
        # % 不作为通配符：100%x 不命中
        self.assertEqual(parse(run_cli("list", "--keyword", "100%").stdout)[0]["id"], 1)
        self.assertEqual(parse(run_cli("list", "--keyword", "100%x").stdout), [])
        # _、\、引号按原字符比较
        self.assertEqual(len(parse(run_cli("list", "--keyword", "a_b").stdout)), 1)
        self.assertEqual(len(parse(run_cli("list", "--keyword", "\\c").stdout)), 1)
        self.assertEqual(len(parse(run_cli("list", "--keyword", '"q').stdout)), 1)
        # 内部连续空白按原字符比较：双空格命中，单空格不命中
        self.assertEqual(len(parse(run_cli("list", "--keyword", "z  空").stdout)), 1)
        self.assertEqual(len(parse(run_cli("list", "--keyword", "z 空").stdout)), 0)

    def test_keyword_match_is_case_sensitive(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = str(Path(tmpdir.name) / "case.sqlite")

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, str(MAIN_PY), "--db", db_path, *args],
                capture_output=True,
                text=True,
            )

        self.assertEqual(
            run_cli("create", "--title", "Login", "--description", "").returncode, 0
        )
        self.assertEqual(
            run_cli("create", "--title", "other", "--description", "login").returncode,
            0,
        )
        parse = ListCommandTestCase.parse_single_json_array
        self.assertEqual(
            [t["id"] for t in parse(run_cli("list", "--keyword", "Login").stdout)],
            [1],
        )
        self.assertEqual(
            [t["id"] for t in parse(run_cli("list", "--keyword", "login").stdout)],
            [2],
        )

    def test_keyword_hit_in_both_fields_returns_row_once(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = str(Path(tmpdir.name) / "both.sqlite")

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, str(MAIN_PY), "--db", db_path, *args],
                capture_output=True,
                text=True,
            )

        self.assertEqual(
            run_cli(
                "create", "--title", "重复 重复", "--description", "重复"
            ).returncode,
            0,
        )
        result = run_cli("list", "--keyword", "重复")
        self.assertEqual(result.returncode, 0)
        tickets = ListCommandTestCase.parse_single_json_array(result.stdout)
        self.assertEqual(len(tickets), 1)

    # ---------- 空关键字 ----------

    def test_empty_keyword_exits_1_with_fixed_message(self):
        for keyword in ("", "   ", "\t \n"):
            with self.subTest(keyword=keyword):
                result = self.run_cli("list", "--keyword", keyword)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "关键字不能为空\n")
                self.assert_storage_unchanged()

    # ---------- 参数用法错误 ----------

    def test_keyword_missing_value_exits_2(self):
        result = self.run_cli("list", "--keyword")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())

    # ---------- 只读与可重复性 ----------

    def test_keyword_query_is_repeatable_and_read_only(self):
        expected = self.expected_tickets([0, 1])
        first = self.list_tickets("--keyword", " 登录 ")
        second = self.list_tickets("--keyword", " 登录 ")
        self.assertEqual(first, expected)
        self.assertEqual(second, first)
        self.assert_storage_unchanged()

        # 成功与失败的关键字查询都不改动存量数据
        for argv in (
            ("list", "--keyword", "登录"),
            ("list", "--keyword", "不存在"),
            ("list", "--keyword", ""),
            ("list", "--keyword", "登录", "--status", "closed"),
        ):
            self.run_cli(*argv)
            self.assert_storage_unchanged()


class NoteKeywordListTestCase(unittest.TestCase):
    """list --note-keyword 备注正文查询。

    固定样例即需求中的两条合成工单（标题均为“登录问题”）：
        1. open，两条备注均含“等待确认”
        2. closed，备注仅含“已解决”
    """

    TICKETS = [
        {"title": "登录问题", "description": "第一张", "status": "open"},
        {"title": "登录问题", "description": "第二张", "status": "closed"},
    ]
    NOTES = {
        1: ["已联系用户，等待确认", "再次等待确认结果"],
        2: ["已解决"],
    }

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.ticket_ids = []
        for ticket in self.TICKETS:
            result = self.run_cli(
                "create",
                "--title",
                ticket["title"],
                "--description",
                ticket["description"],
            )
            self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
            self.assertEqual(result.stderr, "")
            self.ticket_ids.append(json.loads(result.stdout)["id"])
        # 按工单编号追加备注
        for ticket_id, texts in self.NOTES.items():
            for text in texts:
                noted = self.run_cli(
                    "add-note", str(ticket_id), "--text", text
                )
                self.assertEqual(noted.returncode, 0, f"准备样例失败: {noted.stderr}")
                self.assertEqual(noted.stderr, "")
        # 第二条工单结案
        closed = self.run_cli("close", str(self.ticket_ids[1]))
        self.assertEqual(closed.returncode, 0, f"准备样例失败: {closed.stderr}")
        self.assertEqual(self.ticket_ids, [1, 2])

    def run_cli(self, *args):
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
        return ListCommandTestCase.parse_single_json_array(result.stdout)

    def expected_tickets(self, indexes):
        return [
            {
                "id": self.ticket_ids[i],
                "title": self.TICKETS[i]["title"],
                "description": self.TICKETS[i]["description"],
                "status": self.TICKETS[i]["status"],
            }
            for i in indexes
        ]

    def assert_storage_unchanged(self):
        """工单、备注与状态历史均不被查询改动。"""
        conn = sqlite3.connect(self.db_path)
        try:
            ticket_rows = conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
            note_rows = conn.execute(
                "SELECT ticket_id, note_id, text FROM ticket_notes "
                "ORDER BY ticket_id, note_id"
            ).fetchall()
            history_rows = conn.execute(
                "SELECT ticket_id, event_id, from_status, to_status "
                "FROM ticket_history ORDER BY ticket_id, event_id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [list(row) for row in ticket_rows],
            [
                [t["id"], t["title"], t["description"], t["status"]]
                for t in self.expected_tickets([0, 1])
            ],
        )
        self.assertEqual(
            [list(row) for row in note_rows],
            [[1, 1, "已联系用户，等待确认"], [1, 2, "再次等待确认结果"], [2, 1, "已解决"]],
        )
        self.assertEqual([list(row) for row in history_rows], [[2, 1, "open", "closed"]])

    # ---------- 基本命中与去重 ----------

    def test_note_keyword_matches_any_note_once(self):
        # 需求验收：两条备注都命中也只返回第一张一次
        tickets = self.list_tickets("--note-keyword", " 等待确认 ")
        self.assertEqual(tickets, self.expected_tickets([0]))

    def test_note_keyword_earlier_note_participates(self):
        # 只有较早的第一条备注命中
        self.assertEqual(
            self.list_tickets("--note-keyword", "已联系用户"),
            self.expected_tickets([0]),
        )

    def test_note_keyword_hit_in_closed_ticket(self):
        self.assertEqual(
            self.list_tickets("--note-keyword", "已解决"),
            self.expected_tickets([1]),
        )

    def test_note_keyword_output_shape_has_no_note_fields(self):
        tickets = self.list_tickets("--note-keyword", "等待确认")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(
            set(tickets[0].keys()), {"id", "title", "description", "status"}
        )

    # ---------- 与其他条件取交集 ----------

    def test_note_keyword_and_status_are_intersected(self):
        # 需求验收：命中的第一张是 open，closed 交集为空
        self.assertEqual(
            self.list_tickets("--note-keyword", "等待确认", "--status", "closed"),
            [],
        )
        self.assertEqual(
            self.list_tickets("--note-keyword", "等待确认", "--status", "open"),
            self.expected_tickets([0]),
        )
        self.assertEqual(
            self.list_tickets("--note-keyword", "已解决", "--status", "closed"),
            self.expected_tickets([1]),
        )

    def test_note_keyword_and_keyword_are_intersected(self):
        # 两条工单标题都含“登录问题”，备注条件只留下第一张
        self.assertEqual(
            self.list_tickets(
                "--keyword", "登录问题", "--note-keyword", "等待确认"
            ),
            self.expected_tickets([0]),
        )
        # --keyword 只搜索标题/描述：“已解决”只在备注中，故无命中
        self.assertEqual(self.list_tickets("--keyword", "已解决"), [])
        # 标题描述条件与备注条件互斥时为空
        self.assertEqual(
            self.list_tickets(
                "--keyword", "第二张", "--note-keyword", "等待确认"
            ),
            [],
        )

    # ---------- 空结果 ----------

    def test_note_keyword_without_hit_returns_empty_array(self):
        result = self.run_cli("list", "--note-keyword", "不存在的内容")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), "[]")

    def test_ticket_without_notes_is_excluded(self):
        # 新建一张没有备注的工单，不应因备注查询入选
        created = self.run_cli(
            "create", "--title", "登录问题", "--description", "第三张"
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        tickets = self.list_tickets("--note-keyword", "等待确认")
        self.assertEqual(tickets, self.expected_tickets([0]))

    # ---------- 字面子串与大小写 ----------

    def test_note_keyword_special_characters_are_literal(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = str(Path(tmpdir.name) / "special.sqlite")

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, str(MAIN_PY), "--db", db_path, *args],
                capture_output=True,
                text=True,
            )

        self.assertEqual(run_cli("create", "--title", "t", "--description", "").returncode, 0)
        self.assertEqual(
            run_cli("add-note", "1", "--text", '100%done a_b\\c"q\'z  空格').returncode,
            0,
        )
        parse = ListCommandTestCase.parse_single_json_array
        self.assertEqual(parse(run_cli("list", "--note-keyword", "100%x").stdout), [])
        self.assertEqual(len(parse(run_cli("list", "--note-keyword", "100%").stdout)), 1)
        self.assertEqual(len(parse(run_cli("list", "--note-keyword", "a_b").stdout)), 1)
        self.assertEqual(len(parse(run_cli("list", "--note-keyword", "\\c").stdout)), 1)
        self.assertEqual(len(parse(run_cli("list", "--note-keyword", '"q').stdout)), 1)
        # 内部连续空白按原字符比较
        self.assertEqual(len(parse(run_cli("list", "--note-keyword", "z  空").stdout)), 1)
        self.assertEqual(len(parse(run_cli("list", "--note-keyword", "z 空").stdout)), 0)

    def test_note_keyword_match_is_case_sensitive(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = str(Path(tmpdir.name) / "case.sqlite")

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, str(MAIN_PY), "--db", db_path, *args],
                capture_output=True,
                text=True,
            )

        self.assertEqual(run_cli("create", "--title", "a", "--description", "").returncode, 0)
        self.assertEqual(run_cli("create", "--title", "b", "--description", "").returncode, 0)
        self.assertEqual(run_cli("add-note", "1", "--text", "LOG").returncode, 0)
        self.assertEqual(run_cli("add-note", "2", "--text", "log").returncode, 0)
        parse = ListCommandTestCase.parse_single_json_array
        self.assertEqual(
            [t["id"] for t in parse(run_cli("list", "--note-keyword", "LOG").stdout)],
            [1],
        )
        self.assertEqual(
            [t["id"] for t in parse(run_cli("list", "--note-keyword", "log").stdout)],
            [2],
        )

    # ---------- 空关键字 ----------

    def test_empty_note_keyword_exits_1_with_fixed_message(self):
        for keyword in ("", "   ", "\t \n"):
            with self.subTest(keyword=keyword):
                result = self.run_cli("list", "--note-keyword", keyword)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "关键字不能为空\n")
                self.assert_storage_unchanged()

    # ---------- 参数用法错误 ----------

    def test_note_keyword_missing_value_exits_2(self):
        result = self.run_cli("list", "--note-keyword")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())

    # ---------- 只读与可重复性 ----------

    def test_note_keyword_query_is_repeatable_and_read_only(self):
        first = self.list_tickets("--note-keyword", " 等待确认 ")
        second = self.list_tickets("--note-keyword", " 等待确认 ")
        self.assertEqual(first, self.expected_tickets([0]))
        self.assertEqual(second, first)
        self.assert_storage_unchanged()

        for argv in (
            ("list", "--note-keyword", "等待确认"),
            ("list", "--note-keyword", "不存在"),
            ("list", "--note-keyword", ""),
            ("list", "--note-keyword", "等待确认", "--status", "closed"),
            ("list", "--note-keyword", "等待确认", "--keyword", "登录问题"),
        ):
            self.run_cli(*argv)
            self.assert_storage_unchanged()


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
            ("list", "--keyword", "登录"),
            ("list", "--keyword", "登录", "--status", "open"),
            ("list", "--note-keyword", "登录"),
            (
                "list",
                "--note-keyword",
                "登录",
                "--keyword",
                "x",
                "--status",
                "open",
            ),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                # 只有一个空 JSON 数组，无任何说明文字
                self.assertEqual(result.stdout.strip(), "[]")
                self.assertEqual(json.loads(result.stdout), [])


class ThreeConditionIntersectionListTestCase(unittest.TestCase):
    """有数据时 --status、--keyword、--note-keyword 三条件交集的命令行回归。

    固定样例依次建立编号 1 至 4 的工单，描述均为空字符串：
        1. 登录失败（open）   备注：等待确认 / 再次等待确认 / 已复现
        2. 打印异常（open）   备注：等待确认
        3. 登录超时（closed） 备注：等待确认
        4. 登录受限（open）   无备注

    三条件查询 list --status open --keyword 登录 --note-keyword " 等待确认 "
    只应返回编号 1：
        - 编号 3 同样文本与备注均命中但为 closed，被状态条件排除；
        - 编号 2 状态与备注均命中但标题/描述不含“登录”，被关键字条件排除；
        - 编号 4 状态与关键字均命中但没有备注，被备注条件排除；
        - 编号 1 的前两条备注都命中“等待确认”（含最早的第一条），工单只出现一次。
    期望结果全部以固定样例字面量给出，不调用产品筛选函数生成。
    """

    # 样例准备完成后三张表的完整存量（字面量期望）
    EXPECTED_TICKETS = [
        [1, "登录失败", "", "open"],
        [2, "打印异常", "", "open"],
        [3, "登录超时", "", "closed"],
        [4, "登录受限", "", "open"],
    ]
    EXPECTED_NOTES = [
        [1, 1, "等待确认"],
        [1, 2, "再次等待确认"],
        [1, 3, "已复现"],
        [2, 1, "等待确认"],
        [3, 1, "等待确认"],
    ]
    EXPECTED_HISTORY = [[3, 1, "open", "closed"]]

    # 三条件查询可能返回的工单（字面量期望）
    TICKET_1 = {"id": 1, "title": "登录失败", "description": "", "status": "open"}
    TICKET_3 = {"id": 3, "title": "登录超时", "description": "", "status": "closed"}

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        # 依次建立编号 1 至 4 的工单，描述均为空字符串
        ticket_ids = []
        for title in ("登录失败", "打印异常", "登录超时", "登录受限"):
            created = self.run_cli(
                "create", "--title", title, "--description", ""
            )
            self.assertEqual(created.returncode, 0, f"准备样例失败: {created.stderr}")
            self.assertEqual(created.stderr, "")
            ticket_ids.append(json.loads(created.stdout)["id"])
        self.assertEqual(ticket_ids, [1, 2, 3, 4])

        # 编号 1 依次追加三条备注（前两条均含“等待确认”）
        for text in ("等待确认", "再次等待确认", "已复现"):
            noted = self.run_cli("add-note", "1", "--text", text)
            self.assertEqual(noted.returncode, 0, f"准备样例失败: {noted.stderr}")
            self.assertEqual(noted.stderr, "")
        # 编号 2、3 各有一条“等待确认”备注
        for ticket_id in ("2", "3"):
            noted = self.run_cli("add-note", ticket_id, "--text", "等待确认")
            self.assertEqual(noted.returncode, 0, f"准备样例失败: {noted.stderr}")
            self.assertEqual(noted.stderr, "")
        # 仅编号 3 结案
        closed = self.run_cli("close", "3")
        self.assertEqual(closed.returncode, 0, f"准备样例失败: {closed.stderr}")
        self.assertEqual(closed.stderr, "")

        # 准备完成后三张表即处于期望状态
        self.assertEqual(self.dump_storage(), self._expected_storage())

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
        return ListCommandTestCase.parse_single_json_array(result.stdout)

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
        """与 show 相同的四字段：id 为整数，其余为字符串；不含备注或摘要字段。"""
        self.assertIsInstance(ticket, dict)
        self.assertEqual(
            set(ticket.keys()), {"id", "title", "description", "status"}
        )
        self.assertIs(type(ticket["id"]), int)
        for key in ("title", "description", "status"):
            self.assertIs(type(ticket[key]), str)

    # ---------- 三条件交集主流程 ----------

    def test_open_intersection_returns_only_ticket_1(self):
        tickets = self.list_tickets(
            "--status", "open",
            "--keyword", "登录",
            "--note-keyword", " 等待确认 ",
        )
        # 期望结果直接取自固定样例：只有编号 1
        self.assertEqual(tickets, [self.TICKET_1])
        # 标题与空字符串描述保留原值
        self.assertEqual(tickets[0]["title"], "登录失败")
        self.assertEqual(tickets[0]["description"], "")
        self.assertEqual(tickets[0]["status"], "open")
        for ticket in tickets:
            self.assertTicketShape(ticket)

    def test_closed_status_returns_only_ticket_3(self):
        # 只把状态条件改为 closed：编号 3 是唯一关闭且文本、备注均命中的工单
        tickets = self.list_tickets(
            "--status", "closed",
            "--keyword", "登录",
            "--note-keyword", " 等待确认 ",
        )
        self.assertEqual(tickets, [self.TICKET_3])
        for ticket in tickets:
            self.assertTicketShape(ticket)

    def test_nonexistent_note_keyword_returns_empty_array(self):
        # 状态与标题条件命中编号 1、4，但备注关键字不存在时交集为空
        result = self.run_cli(
            "list",
            "--status", "open",
            "--keyword", "登录",
            "--note-keyword", "肯定不存在的备注文字",
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        # 标准输出只有一个空 JSON 数组，无额外说明文字
        self.assertEqual(
            ListCommandTestCase.parse_single_json_array(result.stdout), []
        )

    # ---------- 空备注关键字：退出码 1 ----------

    def test_empty_note_keyword_in_three_condition_query_exits_1(self):
        for keyword in ("", "   ", "\t \n"):
            with self.subTest(keyword=keyword):
                result = self.run_cli(
                    "list",
                    "--status", "open",
                    "--keyword", "登录",
                    "--note-keyword", keyword,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                # 标准错误恰为固定提示加一个换行
                self.assertEqual(result.stderr, "关键字不能为空\n")
                # 使用错误同样不改动任何存量数据
                self.assertEqual(self.dump_storage(), self._expected_storage())

    # ---------- 缺少 --note-keyword 的值：退出码 2 ----------

    def test_missing_note_keyword_value_in_three_condition_query_exits_2(self):
        result = self.run_cli(
            "list",
            "--status", "open",
            "--keyword", "登录",
            "--note-keyword",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assertEqual(self.dump_storage(), self._expected_storage())

    # ---------- 只读与可重复性 ----------

    def test_three_condition_queries_are_repeatable_and_read_only(self):
        before = self.dump_storage()
        open_query = (
            "--status", "open", "--keyword", "登录",
            "--note-keyword", " 等待确认 ",
        )
        closed_query = (
            "--status", "closed", "--keyword", "登录",
            "--note-keyword", " 等待确认 ",
        )
        no_match_query = (
            "--status", "open", "--keyword", "登录",
            "--note-keyword", "肯定不存在的备注文字",
        )

        # 正常查询、无匹配查询与各种失败查询前后，
        # 工单、备注、状态历史三张表都保持不变
        for argv in (
            ("list", *open_query),
            ("list", *closed_query),
            ("list", *no_match_query),
            ("list", "--status", "open", "--keyword", "登录", "--note-keyword", ""),
            ("list", "--status", "open", "--keyword", "登录", "--note-keyword", "  "),
            ("list", "--status", "open", "--keyword", "登录", "--note-keyword"),
        ):
            self.run_cli(*argv)
            self.assertEqual(self.dump_storage(), before)

        # 同库重复查询得到相同数据
        self.assertEqual(self.list_tickets(*open_query), [self.TICKET_1])
        self.assertEqual(self.list_tickets(*open_query), [self.TICKET_1])
        self.assertEqual(self.list_tickets(*closed_query), [self.TICKET_3])
        self.assertEqual(self.list_tickets(*closed_query), [self.TICKET_3])
        self.assertEqual(self.list_tickets(*no_match_query), [])
        self.assertEqual(self.list_tickets(*no_match_query), [])


if __name__ == "__main__":
    unittest.main()
