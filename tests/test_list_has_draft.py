"""list --has-draft 草稿存在性筛选的回归测试。

通过真实命令行入口（python main.py）核对退出码、标准输出与标准错误。
每个用例在独立临时数据库中准备合成工单，测试结束后清理，
不读取或改动工作目录下的默认库 tickets.sqlite。

固定样例即需求验收的三张工单（描述均为空字符串）：
    1. 登录失败（open，一条“等待确认”备注，无草稿）
    2. 登录超时（open，一条“等待确认”备注，保存草稿）
    3. 打印异常（closed，无备注，保存不同正文的草稿）

因此：
    list --has-draft 按编号升序只返回编号 2、3；
    list（不带选项）仍返回全部三张。

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

# 第二、第三张工单保存不同正文的草稿
DRAFT_TEXT_2 = "第二张工单草稿：已指导清除浏览器缓存后重试"
DRAFT_TEXT_3 = "第三张工单草稿：建议重新安装打印驱动后回访"

# 样例准备完成后四张表的完整存量（字面量期望）
EXPECTED_TICKETS = [
    [1, "登录失败", "", "open"],
    [2, "登录超时", "", "open"],
    [3, "打印异常", "", "closed"],
]
EXPECTED_NOTES = [
    [1, 1, "等待确认"],
    [2, 1, "等待确认"],
]
EXPECTED_HISTORY = [
    [3, 1, "open", "closed"],
]
EXPECTED_DRAFTS = [
    [2, DRAFT_TEXT_2],
    [3, DRAFT_TEXT_3],
]

TICKET_1 = {"id": 1, "title": "登录失败", "description": "", "status": "open"}
TICKET_2 = {"id": 2, "title": "登录超时", "description": "", "status": "open"}
TICKET_3 = {"id": 3, "title": "打印异常", "description": "", "status": "closed"}

COMBINED_QUERY = (
    "--has-draft",
    "--status",
    "open",
    "--keyword",
    "登录",
    "--note-keyword",
    "等待确认",
)


class ListHasDraftTestCase(unittest.TestCase):
    """固定三张样例工单上的 list --has-draft 行为。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        ticket_ids = []
        for title in ("登录失败", "登录超时", "打印异常"):
            created = self.run_cli(
                "create", "--title", title, "--description", ""
            )
            self.assertEqual(
                created.returncode, 0, f"准备样例失败: {created.stderr}"
            )
            self.assertEqual(created.stderr, "")
            ticket_ids.append(json.loads(created.stdout)["id"])
        self.assertEqual(ticket_ids, [1, 2, 3])

        # 第三张结案；前两张保持 open
        closed = self.run_cli("close", "3")
        self.assertEqual(closed.returncode, 0, f"准备样例失败: {closed.stderr}")
        self.assertEqual(closed.stderr, "")

        # 前两张各有一条“等待确认”备注
        for ticket_id in (1, 2):
            noted = self.run_cli(
                "add-note", str(ticket_id), "--text", "等待确认"
            )
            self.assertEqual(noted.returncode, 0, f"准备样例失败: {noted.stderr}")
            self.assertEqual(noted.stderr, "")

        # 只有第二、第三张保存草稿，正文不同
        for ticket_id, text in ((2, DRAFT_TEXT_2), (3, DRAFT_TEXT_3)):
            drafted = self.run_cli(
                "set-draft", str(ticket_id), "--text", text
            )
            self.assertEqual(drafted.returncode, 0, f"准备样例失败: {drafted.stderr}")
            self.assertEqual(drafted.stderr, "")

        # 准备完成后四张表即处于期望状态
        self.assertEqual(self.snapshot(), self.expected_snapshot())

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

    def show_ticket(self, ticket_id):
        """执行 show 命令，返回解析后的工单对象。"""
        result = self.run_cli("show", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"show 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        value = json.loads(result.stdout)
        self.assertIsInstance(value, dict)
        return value

    def snapshot(self):
        """直接读取 SQLite，返回工单、备注、状态历史、草稿四张表的全部行。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return {
                "tickets": [
                    list(row)
                    for row in conn.execute(
                        "SELECT id, title, description, status FROM tickets ORDER BY id"
                    )
                ],
                "notes": [
                    list(row)
                    for row in conn.execute(
                        "SELECT ticket_id, note_id, text FROM ticket_notes "
                        "ORDER BY ticket_id, note_id"
                    )
                ],
                "history": [
                    list(row)
                    for row in conn.execute(
                        "SELECT ticket_id, event_id, from_status, to_status "
                        "FROM ticket_history ORDER BY ticket_id, event_id"
                    )
                ],
                "drafts": [
                    list(row)
                    for row in conn.execute(
                        "SELECT ticket_id, text FROM ticket_drafts ORDER BY ticket_id"
                    )
                ],
            }
        finally:
            conn.close()

    @staticmethod
    def expected_snapshot():
        return {
            "tickets": [list(row) for row in EXPECTED_TICKETS],
            "notes": [list(row) for row in EXPECTED_NOTES],
            "history": [list(row) for row in EXPECTED_HISTORY],
            "drafts": [list(row) for row in EXPECTED_DRAFTS],
        }

    def assertTicketShape(self, ticket):
        """与 show 相同的四字段：id 为整数，其余为字符串，不附带草稿正文。"""
        self.assertIsInstance(ticket, dict)
        self.assertEqual(
            set(ticket.keys()), {"id", "title", "description", "status"}
        )
        # bool 虽是 int 子类但不合法
        self.assertIs(type(ticket["id"]), int)
        for key in ("title", "description", "status"):
            self.assertIs(type(ticket[key]), str)

    def assert_usage_error(self, argv):
        """用法错误：退出 2、标准输出为空、标准错误含用法提示。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 2, f"argv={argv!r}: {result.stderr}")
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        return result

    # ---------- 需求验收主流程 ----------

    def test_has_draft_returns_tickets_2_and_3_in_id_order(self):
        result = self.run_cli("list", "--has-draft")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        tickets = self.parse_single_json_array(result.stdout)

        # 编号 1 无草稿被排除；编号 2、3 按整数编号升序返回
        self.assertEqual(tickets, [TICKET_2, TICKET_3])
        self.assertEqual([t["id"] for t in tickets], [2, 3])
        for ticket in tickets:
            self.assertTicketShape(ticket)

    def test_list_without_flag_still_returns_all_three(self):
        # 不带 --has-draft 时草稿不影响列表：仍返回全部三张
        tickets = self.list_tickets()
        self.assertEqual(tickets, [TICKET_1, TICKET_2, TICKET_3])
        self.assertEqual([t["id"] for t in tickets], [1, 2, 3])
        for ticket in tickets:
            self.assertTicketShape(ticket)

    def test_has_draft_items_match_show_and_carry_no_draft_text(self):
        tickets = self.list_tickets("--has-draft")
        self.assertEqual([t["id"] for t in tickets], [2, 3])
        for ticket in tickets:
            # 每项的字段、类型及内容与同编号 show 结果完全一致
            self.assertEqual(ticket, self.show_ticket(ticket["id"]))
            self.assertTicketShape(ticket)
            # 不附加草稿正文字段，也不在任何字段中夹带草稿内容
            self.assertNotIn("draft", ticket)
            self.assertNotIn("text", ticket)
            for draft_text in (DRAFT_TEXT_2, DRAFT_TEXT_3):
                self.assertNotIn(draft_text, json.dumps(ticket, ensure_ascii=False))

    def test_combined_conditions_with_limit_one_returns_only_ticket_2(self):
        # has-draft ∩ open ∩ 标题/描述含“登录” ∩ 备注含“等待确认”，
        # 再截取前 1 张：编号 1 被草稿条件排除、编号 3 被状态与关键字排除
        result = self.run_cli("list", *COMBINED_QUERY, "--limit", "1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        tickets = self.parse_single_json_array(result.stdout)
        self.assertEqual(tickets, [TICKET_2])
        self.assertTicketShape(tickets[0])

    def test_after_id_boundary_before_limit_returns_only_ticket_3(self):
        # 编号边界先于数量截取：has-draft 集合 2、3 中只留严格大于 2 的，
        # 再截取 1 张，得到编号 3 而非编号 2
        result = self.run_cli(
            "list", "--has-draft", "--after-id", "2", "--limit", "1"
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        tickets = self.parse_single_json_array(result.stdout)
        self.assertEqual(tickets, [TICKET_3])
        self.assertTicketShape(tickets[0])

    def test_has_draft_intersects_other_conditions(self):
        # 与状态取交集：只有第三张有草稿且已结案
        self.assertEqual(self.list_tickets("--has-draft", "--status", "closed"), [TICKET_3])
        self.assertEqual(
            self.list_tickets("--has-draft", "--status", "open"), [TICKET_2]
        )
        # 与标题/描述关键字取交集
        self.assertEqual(self.list_tickets("--has-draft", "--keyword", "登录"), [TICKET_2])
        self.assertEqual(self.list_tickets("--has-draft", "--keyword", "打印"), [TICKET_3])
        # 与备注关键字取交集：编号 1 虽有命中备注但无草稿，不入选
        self.assertEqual(
            self.list_tickets("--has-draft", "--note-keyword", "等待确认"), [TICKET_2]
        )
        # 条件交集为空时成功返回 []
        self.assertEqual(
            self.list_tickets(
                "--has-draft", "--status", "closed", "--keyword", "登录"
            ),
            [],
        )

    # ---------- 覆盖、清除、重新保存草稿对列表的影响 ----------

    def test_overwriting_draft_keeps_ticket_listed_once(self):
        # 覆盖保存第二张的草稿：工单不会重复出现，正文被整篇替换
        overwrite = self.run_cli(
            "set-draft", "2", "--text", "第二张工单更新后的草稿正文"
        )
        self.assertEqual(overwrite.returncode, 0)
        self.assertEqual(overwrite.stderr, "")

        tickets = self.list_tickets("--has-draft")
        ids = [t["id"] for t in tickets]
        self.assertEqual(ids, [2, 3])
        self.assertEqual(len(ids), len(set(ids)))
        # 列表项仍只有工单四字段，不随草稿覆盖而变化
        self.assertEqual(tickets[0], TICKET_2)

        drafts = self.snapshot()["drafts"]
        self.assertEqual(
            drafts,
            [[2, "第二张工单更新后的草稿正文"], [3, DRAFT_TEXT_3]],
        )

    def test_clear_then_resave_draft_changes_result_and_is_repeatable(self):
        # 清除第二张草稿后筛选只剩第三张
        clear = self.run_cli("clear-draft", "2")
        self.assertEqual(clear.returncode, 0)
        self.assertEqual(clear.stderr, "")
        self.assertEqual(self.list_tickets("--has-draft"), [TICKET_3])
        self.assertEqual(
            self.snapshot()["drafts"], [[3, DRAFT_TEXT_3]]
        )

        # 重新保存后恢复编号 2、3
        resave = self.run_cli(
            "set-draft", "2", "--text", DRAFT_TEXT_2
        )
        self.assertEqual(resave.returncode, 0)
        self.assertEqual(resave.stderr, "")
        first = self.list_tickets("--has-draft")
        self.assertEqual(first, [TICKET_2, TICKET_3])
        # 重新调用命令仍得到相同结果（跨进程可重复）
        self.assertEqual(self.list_tickets("--has-draft"), first)
        self.assertEqual(self.list_tickets("--has-draft"), [TICKET_2, TICKET_3])
        self.assertEqual(self.snapshot(), self.expected_snapshot())

    # ---------- 空/纯空白关键字：退出 1 ----------

    def test_blank_keyword_with_has_draft_exits_1(self):
        for blank in ("", "   ", "\t \n"):
            for option in ("--keyword", "--note-keyword"):
                with self.subTest(blank=blank, option=option):
                    before = self.snapshot()
                    result = self.run_cli("list", "--has-draft", option, blank)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "关键字不能为空\n")
                    # 失败查询不改动任何存量记录
                    self.assertEqual(self.snapshot(), before)

    # ---------- 用法错误：退出 2 ----------

    def test_has_draft_takes_no_value(self):
        # --has-draft 是开关：附带“true”按未知位置参数处理，退出 2
        result = self.assert_usage_error(("list", "--has-draft", "true"))
        self.assertEqual(result.stdout, "")

    def test_zero_limit_with_has_draft_exits_2(self):
        # 数量为零按用法错误处理
        self.assert_usage_error(("list", "--has-draft", "--limit", "0"))

    def test_invalid_limit_with_blank_keyword_still_exits_2(self):
        # 无效数量与空关键字并存：argparse 先拒绝数量，仍退出 2 而非 1
        for option in ("--keyword", "--note-keyword"):
            with self.subTest(option=option):
                self.assert_usage_error(
                    ("list", "--has-draft", option, "   ", "--limit", "0")
                )
                self.assert_usage_error(
                    ("list", "--has-draft", "--limit", "0", option, "")
                )

    # ---------- 只读与可重复性 ----------

    def test_queries_are_read_only_and_repeatable(self):
        before = self.snapshot()

        # 成功、空结果与失败查询交错进行
        self.list_tickets("--has-draft")
        self.list_tickets(*COMBINED_QUERY, "--limit", "1")
        self.list_tickets("--has-draft", "--after-id", "2", "--limit", "1")
        # 空结果查询
        self.assertEqual(self.list_tickets("--has-draft", "--keyword", "不存在的内容"), [])
        # 退出 1 的失败查询
        failed_keyword = self.run_cli("list", "--has-draft", "--keyword", "")
        self.assertEqual(failed_keyword.returncode, 1)
        # 退出 2 的失败查询
        self.assertEqual(self.run_cli("list", "--has-draft", "true").returncode, 2)
        self.assertEqual(self.run_cli("list", "--has-draft", "--limit", "0").returncode, 2)
        # 不带选项的全量列表
        self.list_tickets()

        # 工单、备注、状态历史与草稿记录前后完全相同
        self.assertEqual(self.snapshot(), before)

        # 重新运行相同查询结果一致
        self.assertEqual(self.list_tickets("--has-draft"), [TICKET_2, TICKET_3])
        self.assertEqual(
            self.list_tickets(*COMBINED_QUERY, "--limit", "1"), [TICKET_2]
        )
        self.assertEqual(
            self.list_tickets("--has-draft", "--after-id", "2", "--limit", "1"),
            [TICKET_3],
        )


class ListHasDraftWithoutDraftsTestCase(unittest.TestCase):
    """有工单但均无草稿：--has-draft 成功返回 []，普通列表不受影响。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "no_draft_tickets.sqlite")

        ticket_ids = []
        for title in ("登录失败", "登录超时", "打印异常"):
            created = subprocess.run(
                [
                    sys.executable,
                    str(MAIN_PY),
                    "--db",
                    self.db_path,
                    "create",
                    "--title",
                    title,
                    "--description",
                    "",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(created.returncode, 0, f"准备样例失败: {created.stderr}")
            ticket_ids.append(json.loads(created.stdout)["id"])
        self.assertEqual(ticket_ids, [1, 2, 3])

        # 结案第三张、前两张加备注，但一律不保存草稿
        for ticket_id in (1, 2):
            noted = subprocess.run(
                [
                    sys.executable,
                    str(MAIN_PY),
                    "--db",
                    self.db_path,
                    "add-note",
                    str(ticket_id),
                    "--text",
                    "等待确认",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(noted.returncode, 0, f"准备样例失败: {noted.stderr}")
        closed = subprocess.run(
            [
                sys.executable,
                str(MAIN_PY),
                "--db",
                self.db_path,
                "close",
                "3",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(closed.returncode, 0, f"准备样例失败: {closed.stderr}")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def test_has_draft_returns_empty_array_when_no_draft_exists(self):
        for argv in (
            ("list", "--has-draft"),
            ("list", "--has-draft", "--status", "open"),
            ("list", "--has-draft", "--keyword", "登录"),
            ("list", "--has-draft", "--note-keyword", "等待确认"),
            ("list", "--has-draft", "--after-id", "1", "--limit", "1"),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout.strip(), "[]")
                self.assertEqual(
                    ListHasDraftTestCase.parse_single_json_array(result.stdout), []
                )

        # 不带选项时三张工单仍正常返回
        plain = self.run_cli("list")
        self.assertEqual(plain.returncode, 0)
        self.assertEqual(plain.stderr, "")
        tickets = ListHasDraftTestCase.parse_single_json_array(plain.stdout)
        self.assertEqual([t["id"] for t in tickets], [1, 2, 3])


class ListHasDraftEmptyDatabaseTestCase(unittest.TestCase):
    """空库上的 list --has-draft：建表初始化不产生记录，返回 []。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "empty_tickets.sqlite")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def test_has_draft_on_empty_database_returns_empty_array(self):
        for argv in (
            ("list", "--has-draft"),
            ("list", "--has-draft", "--status", "open"),
            ("list", "--has-draft", "--keyword", "登录"),
            ("list", "--has-draft", "--after-id", "1", "--limit", "1"),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout.strip(), "[]")

        # 查询后空库仍只有空表：四张表均无记录
        conn = sqlite3.connect(self.db_path)
        try:
            for table in (
                "tickets",
                "ticket_notes",
                "ticket_history",
                "ticket_drafts",
            ):
                self.assertEqual(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
