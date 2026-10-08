"""list --has-draft 草稿存在性筛选的回归测试。

通过真实命令行入口（python main.py）核对退出码、标准输出与标准错误。
每个用例在独立临时数据库中准备合成工单，测试结束后清理，
不读取或改动工作目录下的默认库。

固定样例依次创建三张工单（描述均为空字符串）：
    1. 登录失败（open）   备注：等待确认；无草稿
    2. 登录超时（open）   备注：等待确认；有草稿
    3. 打印异常（closed） 无备注；有草稿

因此 list --has-draft 的匹配集合固定为编号 2、3：
    - 编号 1 没有草稿，被草稿存在性条件排除；
    - 编号 2、3 各保存了一份正文不同的草稿，与草稿正文内容无关。

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
    {"title": "登录失败", "description": "", "status": "open"},
    {"title": "登录超时", "description": "", "status": "open"},
    {"title": "打印异常", "description": "", "status": "closed"},
]
# 前两张工单各有一条“等待确认”备注
NOTES = {
    1: ["等待确认"],
    2: ["等待确认"],
}
# 仅第二、第三张保存草稿，正文不同
DRAFTS = {
    2: "您好，正在为您核实登录状态",
    3: "打印问题已记录，稍后安排回复",
}
# 覆盖保存到第二张的新草稿正文
REWRITTEN_DRAFT_2 = "已为您重置密码，请查收短信"

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
EXPECTED_HISTORY = [[3, 1, "open", "closed"]]
EXPECTED_DRAFTS = [
    [2, DRAFTS[2]],
    [3, DRAFTS[3]],
]

TICKET_1 = {"id": 1, "title": "登录失败", "description": "", "status": "open"}
TICKET_2 = {"id": 2, "title": "登录超时", "description": "", "status": "open"}
TICKET_3 = {"id": 3, "title": "打印异常", "description": "", "status": "closed"}


class HasDraftListTestCase(unittest.TestCase):
    """固定三张样例工单上的 list --has-draft 行为。"""

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

        # 仅第三张结案
        closed = self.run_cli("close", "3")
        self.assertEqual(closed.returncode, 0, f"准备样例失败: {closed.stderr}")
        self.assertEqual(closed.stderr, "")

        # 仅第二、第三张保存草稿
        for ticket_id, text in DRAFTS.items():
            drafted = self.run_cli(
                "set-draft", str(ticket_id), "--text", text
            )
            self.assertEqual(
                drafted.returncode, 0, f"准备样例失败: {drafted.stderr}"
            )
            self.assertEqual(drafted.stderr, "")

        # 准备完成后四张表即处于期望状态
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

    def show_ticket(self, ticket_id):
        """执行 show 命令并返回解析后的唯一工单 JSON 对象。"""
        result = self.run_cli("show", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"show 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        decoder = json.JSONDecoder()
        stripped = result.stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        self.assertEqual(stripped[end:].strip(), "")
        return value

    def dump_storage(self):
        """直接读取 SQLite，返回工单、备注、状态历史与草稿四张表的全部行。"""
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
            drafts = [
                list(row)
                for row in conn.execute(
                    "SELECT ticket_id, text FROM ticket_drafts ORDER BY ticket_id"
                )
            ]
        finally:
            conn.close()
        return tickets, notes, history, drafts

    def _expected_storage(self, drafts=EXPECTED_DRAFTS):
        return (
            [list(row) for row in EXPECTED_TICKETS],
            [list(row) for row in EXPECTED_NOTES],
            [list(row) for row in EXPECTED_HISTORY],
            [list(row) for row in drafts],
        )

    def assertTicketShape(self, ticket):
        """与 show 相同的四字段：id 为整数，其余为字符串；不附加草稿正文。"""
        self.assertIsInstance(ticket, dict)
        self.assertEqual(
            set(ticket.keys()), {"id", "title", "description", "status"}
        )
        # bool 虽是 int 子类但不合法
        self.assertIs(type(ticket["id"]), int)
        for key in ("title", "description", "status"):
            self.assertIs(type(ticket[key]), str)

    # ---------- 基本筛选 ----------

    def test_has_draft_returns_tickets_2_and_3_in_id_order(self):
        result = self.run_cli("list", "--has-draft")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

        tickets = self.parse_single_json_array(result.stdout)
        self.assertEqual(tickets, [TICKET_2, TICKET_3])
        # 编号按整数升序
        self.assertEqual([t["id"] for t in tickets], [2, 3])
        for ticket in tickets:
            self.assertTicketShape(ticket)
        # 标准输出不附加任何草稿正文
        self.assertNotIn(DRAFTS[2], result.stdout)
        self.assertNotIn(DRAFTS[3], result.stdout)

    def test_list_without_has_draft_returns_all_three_tickets(self):
        tickets = self.list_tickets()
        self.assertEqual(tickets, [TICKET_1, TICKET_2, TICKET_3])
        self.assertEqual([t["id"] for t in tickets], [1, 2, 3])
        for ticket in tickets:
            self.assertTicketShape(ticket)

    def test_has_draft_items_match_show_output(self):
        tickets = self.list_tickets("--has-draft")
        self.assertEqual(len(tickets), 2)
        # 每项的字段、类型及内容与同编号的 show 输出一致
        for ticket in tickets:
            self.assertEqual(ticket, self.show_ticket(ticket["id"]))

    # ---------- 与其他条件取交集，编号边界与数量截取 ----------

    def test_has_draft_with_status_keyword_note_keyword_and_limit(self):
        # 四条件交集后只剩编号 2（编号 3 为 closed 且无备注），limit 1 原样返回
        tickets = self.list_tickets(
            "--has-draft",
            "--status", "open",
            "--keyword", "登录",
            "--note-keyword", "等待确认",
            "--limit", "1",
        )
        self.assertEqual(tickets, [TICKET_2])
        self.assertTicketShape(tickets[0])

    def test_has_draft_with_after_id_and_limit(self):
        # 编号边界先于数量截取：匹配集合 2、3 中严格大于 2 的只有 3
        tickets = self.list_tickets("--has-draft", "--after-id", "2", "--limit", "1")
        self.assertEqual(tickets, [TICKET_3])
        self.assertTicketShape(tickets[0])

    # ---------- 空结果 ----------

    def test_empty_database_returns_empty_array(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = str(Path(tmpdir.name) / "empty.sqlite")

        result = subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", db_path, "list", "--has-draft"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), "[]")
        self.assertEqual(self.parse_single_json_array(result.stdout), [])

    def test_tickets_without_any_draft_return_empty_array(self):
        # 另建一个只有工单与备注、没有任何草稿的数据库
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = str(Path(tmpdir.name) / "no_drafts.sqlite")

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, str(MAIN_PY), "--db", db_path, *args],
                capture_output=True,
                text=True,
            )

        for title in ("登录失败", "打印异常"):
            created = run_cli("create", "--title", title, "--description", "")
            self.assertEqual(created.returncode, 0, created.stderr)
        noted = run_cli("add-note", "1", "--text", "等待确认")
        self.assertEqual(noted.returncode, 0, noted.stderr)

        result = run_cli("list", "--has-draft")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), "[]")
        self.assertEqual(self.parse_single_json_array(result.stdout), [])

    # ---------- 覆盖、清除与恢复 ----------

    def test_overwrite_clear_and_resave_draft(self):
        # 覆盖保存第二张的草稿：工单不重复出现，匹配集合不变
        rewritten = self.run_cli(
            "set-draft", "2", "--text", REWRITTEN_DRAFT_2
        )
        self.assertEqual(rewritten.returncode, 0, rewritten.stderr)
        self.assertEqual(rewritten.stderr, "")
        tickets = self.list_tickets("--has-draft")
        self.assertEqual(tickets, [TICKET_2, TICKET_3])
        self.assertEqual([t["id"] for t in tickets], [2, 3])
        self.assertEqual(
            self.dump_storage(),
            self._expected_storage(drafts=[[2, REWRITTEN_DRAFT_2], [3, DRAFTS[3]]]),
        )

        # 清除第二张的草稿后：筛选只剩第三张
        cleared = self.run_cli("clear-draft", "2")
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertEqual(cleared.stderr, "")
        self.assertEqual(self.list_tickets("--has-draft"), [TICKET_3])
        self.assertEqual(
            self.dump_storage(),
            self._expected_storage(drafts=[[3, DRAFTS[3]]]),
        )

        # 重新保存后恢复编号 2、3，重复调用结果相同
        restored = self.run_cli("set-draft", "2", "--text", DRAFTS[2])
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(restored.stderr, "")
        first = self.list_tickets("--has-draft")
        second = self.list_tickets("--has-draft")
        self.assertEqual(first, [TICKET_2, TICKET_3])
        self.assertEqual(second, first)
        self.assertEqual(self.dump_storage(), self._expected_storage())

    # ---------- 空关键字：退出码 1 ----------

    def test_blank_keyword_with_has_draft_exits_1(self):
        for option in ("--keyword", "--note-keyword"):
            for keyword in ("", "   ", "\t \n"):
                with self.subTest(option=option, keyword=keyword):
                    result = self.run_cli(
                        "list", "--has-draft", option, keyword
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    # 标准错误恰为固定提示加一个换行
                    self.assertEqual(result.stderr, "关键字不能为空\n")
                    # 使用错误不改动任何存量数据
                    self.assertEqual(self.dump_storage(), self._expected_storage())

    # ---------- 用法错误：退出码 2 ----------

    def assert_usage_error(self, argv):
        """用法错误：退出 2、标准输出为空、标准错误含用法提示。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 2, f"argv={argv!r}: {result.stderr}")
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        # 用法错误不改动任何存量数据
        self.assertEqual(self.dump_storage(), self._expected_storage())

    def test_has_draft_with_positional_value_exits_2(self):
        # --has-draft 不接收参数值：多余的 "true" 按未知参数处理
        self.assert_usage_error(("list", "--has-draft", "true"))

    def test_zero_limit_with_has_draft_exits_2(self):
        result = self.run_cli("list", "--has-draft", "--limit", "0")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assertIn("--limit", result.stderr)
        self.assertEqual(self.dump_storage(), self._expected_storage())

    def test_invalid_limit_and_blank_keyword_still_exits_2(self):
        # 无效数量与空关键字并存时，argparse 的用法错误优先（退出 2）
        self.assert_usage_error(
            ("list", "--has-draft", "--keyword", "", "--limit", "0")
        )
        self.assert_usage_error(
            ("list", "--has-draft", "--note-keyword", "   ", "--limit", "abc")
        )

    # ---------- 只读与可重复性 ----------

    def test_queries_are_repeatable_and_read_only(self):
        before = self.dump_storage()

        first = self.list_tickets("--has-draft")
        second = self.list_tickets("--has-draft")
        self.assertEqual(first, [TICKET_2, TICKET_3])
        self.assertEqual(second, first)

        # 成功、空结果与失败查询前后，工单、备注、状态历史与草稿均保持不变
        for argv in (
            ("list", "--has-draft"),
            ("list", "--has-draft", "--status", "closed"),
            ("list", "--has-draft", "--keyword", "不存在的内容"),
            ("list", "--has-draft", "--keyword", ""),
            ("list", "--has-draft", "--note-keyword", "  "),
            ("list", "--has-draft", "--limit", "0"),
            ("list", "--has-draft", "true"),
        ):
            self.run_cli(*argv)
            self.assertEqual(self.dump_storage(), before)


if __name__ == "__main__":
    unittest.main()
