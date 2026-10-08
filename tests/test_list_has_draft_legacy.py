"""list --has-draft 对缺 ticket_drafts 旧库的兼容性回归测试。

现有 list --has-draft 测试（test_list_has_draft.py）覆盖由当前版本创建的
新库；本模块手工构造两类产品升级前可能留存的旧版本 SQLite 文件（均没有
ticket_drafts 表），通过真实命令行入口
（python main.py --db <临时文件> list ...）核对首次打开旧库时的公开行为：

1. 仅有 tickets 表的最早期旧库：编号 7 的“打印异常”工单（描述为空字符串、
   状态 closed）与编号 12 的“登录失败”工单（描述为“更换网络后仍无法登录”、
   状态 open），没有 ticket_notes、ticket_history、ticket_drafts。
2. 另有 ticket_notes 与 ticket_history、但仍没有 ticket_drafts 的中期旧库：
   同样两张工单；编号 7 另有序号 1、正文为“ 已复现 ”（首尾空格按原文保留）
   的备注，以及序号 1、从 open 到 closed 的状态历史。

两类旧库上，首次执行 list --has-draft 必须退出 0、标准错误为空、标准输出
只有一个空 JSON 数组；随后普通 list 按编号升序返回这两张工单，每项沿用
show 的四字段、类型和原值，不带备注、历史或草稿字段。重复启动查询结果
一致；补建的 ticket_drafts 没有记录，第一类补建的备注和历史表也为空，
不推断旧工单过去的处理过程。

异常用例各自从尚未被产品打开的旧库开始：--keyword 为显式空字符串或纯
空白时退出 1；同一输入再带 --limit 0 时优先按用法错误退出 2，且数据库
保持调用前的缺表状态（参数解析发生在打开数据库之前）。

每个用例使用独立临时目录与独立的合成数据库，测试结束后清理，
不读取或改动工作目录下的 tickets.sqlite，也不依赖网络或第三方包。
JSON 比较一律基于解析后的字段集合、类型与值，不依赖键序或排版；
允许按既有规则补建缺失的空表，但不得产生占位记录或状态历史。

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

TICKET_KEYS = {"id", "title", "description", "status"}

# 两类旧库共用的固定合成工单：编号 7 已结案、描述为空；编号 12 未结案
LEGACY_TICKET_7 = {
    "id": 7,
    "title": "打印异常",
    "description": "",
    "status": "closed",
}
LEGACY_TICKET_12 = {
    "id": 12,
    "title": "登录失败",
    "description": "更换网络后仍无法登录",
    "status": "open",
}
EXPECTED_TICKET_ROWS = [
    [7, "打印异常", "", "closed"],
    [12, "登录失败", "更换网络后仍无法登录", "open"],
]

# 仅第二类旧库存在的处理记录：备注与历史的首尾空格、状态走向按原文保留
NOTE_TEXT_7 = " 已复现 "
EXPECTED_NOTE_ROWS = [[1, 7, 1, NOTE_TEXT_7]]  # id, ticket_id, note_id, text
EXPECTED_HISTORY_ROWS = [[1, 7, 1, "open", "closed"]]  # id, ticket_id, event_id, from, to

# 与当前版本相同的表结构：旧库只是“缺表”，已有表的列结构保持一致
TICKETS_DDL = """
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL
)
"""

TICKET_NOTES_DDL = """
CREATE TABLE ticket_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    note_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    UNIQUE (ticket_id, note_id)
)
"""

TICKET_HISTORY_DDL = """
CREATE TABLE ticket_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    UNIQUE (ticket_id, event_id)
)
"""


def _insert_tickets(conn):
    conn.executemany(
        "INSERT INTO tickets (id, title, description, status) "
        "VALUES (?, ?, ?, ?)",
        [tuple(row) for row in EXPECTED_TICKET_ROWS],
    )


def build_tickets_only_db(path):
    """构造第一类旧库：文件中仅有 tickets 表，含编号 7、12 两张旧工单。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(TICKETS_DDL)
        _insert_tickets(conn)
        conn.commit()
    finally:
        conn.close()


def build_tickets_notes_history_db(path):
    """构造第二类旧库：有 tickets、ticket_notes、ticket_history，没有 ticket_drafts。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(TICKETS_DDL)
        conn.execute(TICKET_NOTES_DDL)
        conn.execute(TICKET_HISTORY_DDL)
        _insert_tickets(conn)
        # 自增主键 id 也显式固定，便于逐行核对编号不变
        conn.executemany(
            "INSERT INTO ticket_notes (id, ticket_id, note_id, text) "
            "VALUES (?, ?, ?, ?)",
            [tuple(row) for row in EXPECTED_NOTE_ROWS],
        )
        conn.executemany(
            "INSERT INTO ticket_history (id, ticket_id, event_id, from_status, to_status) "
            "VALUES (?, ?, ?, ?, ?)",
            [tuple(row) for row in EXPECTED_HISTORY_ROWS],
        )
        conn.commit()
    finally:
        conn.close()


class LegacyListHasDraftCompatMixin:
    """两类旧库共用的测试流程；具体旧库形态由子类的 build_database 提供。

    子类需设置 ``DB_FILENAME`` 与 ``build_database``（静态构造函数）。
    本类不继承 TestCase，避免被 unittest 当作独立用例集收集。
    """

    DB_FILENAME = "legacy.sqlite"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / self.DB_FILENAME)
        self.build_database(self.db_path)

    # ---------- 辅助方法 ----------

    def make_pristine_db(self):
        """另建一个从未被当前版本产品打开过的同类旧库，返回其路径。"""
        path = str(Path(self._tmpdir.name) / "pristine.sqlite")
        self.build_database(path)
        return path

    def run_cli(self, db_path, *args):
        """以真实命令行入口在全新进程中运行 main.py。"""
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", db_path, *args],
            capture_output=True,
            text=True,
        )

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

    def run_list(self, db_path, *args):
        """执行 list，要求成功并返回解析后的唯一 JSON 数组。"""
        result = self.run_cli(db_path, "list", *args)
        self.assertEqual(result.returncode, 0, f"list 失败: {result.stderr!r}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json_array(result.stdout)

    def run_show(self, db_path, raw_id):
        """执行 show，要求成功并返回解析后的唯一工单 JSON 对象。"""
        result = self.run_cli(db_path, "show", str(raw_id))
        self.assertEqual(result.returncode, 0, f"show 失败: {result.stderr!r}")
        self.assertEqual(result.stderr, "")
        decoder = json.JSONDecoder()
        stripped = result.stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        self.assertIsInstance(value, dict)
        self.assertEqual(stripped[end:].strip(), "")
        return value

    @staticmethod
    def snapshot(db_path):
        """导出当时已存在各表的全部行（含自增编号），用于逐行比对。

        返回 dict 的键集合同时反映当时存在哪些表：缺表状态本身也是
        比对对象（用法错误不得触发补建）。
        """
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            data = {}
            if "tickets" in tables:
                data["tickets"] = [
                    list(row)
                    for row in conn.execute(
                        "SELECT id, title, description, status "
                        "FROM tickets ORDER BY id"
                    )
                ]
            if "ticket_notes" in tables:
                data["ticket_notes"] = [
                    list(row)
                    for row in conn.execute(
                        "SELECT id, ticket_id, note_id, text "
                        "FROM ticket_notes ORDER BY ticket_id, note_id, id"
                    )
                ]
            if "ticket_history" in tables:
                data["ticket_history"] = [
                    list(row)
                    for row in conn.execute(
                        "SELECT id, ticket_id, event_id, from_status, to_status "
                        "FROM ticket_history ORDER BY ticket_id, event_id"
                    )
                ]
            if "ticket_drafts" in tables:
                data["ticket_drafts"] = [
                    list(row)
                    for row in conn.execute(
                        "SELECT id, ticket_id, text "
                        "FROM ticket_drafts ORDER BY ticket_id"
                    )
                ]
            return data
        finally:
            conn.close()

    def assert_records_preserved(self, before, db_path):
        """原工单、原备注与原历史全部记录（编号、正文）不变；草稿表为空。

        允许按既有规则补建缺失的空表，但补建出的表必须存在且为空，
        不产生占位记录，也不为旧工单补记任何状态历史。
        """
        after = self.snapshot(db_path)
        for table in (
            "tickets",
            "ticket_notes",
            "ticket_history",
            "ticket_drafts",
        ):
            self.assertIn(table, after, f"缺少应已补建的表: {table}")
        self.assertEqual(after["tickets"], before["tickets"])
        self.assertEqual(after["tickets"], EXPECTED_TICKET_ROWS)
        # 第一类旧库最初没有备注/历史表：允许补建成空表，但不得有记录
        self.assertEqual(after["ticket_notes"], before.get("ticket_notes", []))
        self.assertEqual(after["ticket_history"], before.get("ticket_history", []))
        # 补建的草稿表必须为空：旧工单一律没有草稿
        self.assertEqual(after["ticket_drafts"], [])

    def assert_ticket_shape(self, ticket):
        """与 show 相同的四字段：id 为整数，其余为字符串，不带其他字段。"""
        self.assertIsInstance(ticket, dict)
        self.assertEqual(set(ticket.keys()), TICKET_KEYS)
        # bool 虽是 int 子类但不合法
        self.assertIs(type(ticket["id"]), int)
        for key in ("title", "description", "status"):
            self.assertIs(type(ticket[key]), str)

    # ---------- 需求验收主流程 ----------

    def test_first_has_draft_query_returns_empty_array(self):
        # 旧库首次被当前版本打开：list --has-draft 必须成功返回空数组
        before = self.snapshot(self.db_path)
        result = self.run_cli(self.db_path, "list", "--has-draft")
        self.assertEqual(result.returncode, 0, f"list 失败: {result.stderr!r}")
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), "[]")
        self.assertEqual(self.parse_single_json_array(result.stdout), [])

        # 原记录逐行不变；补建的草稿表为空，不推断旧工单的处理过程
        self.assert_records_preserved(before, self.db_path)

    def test_plain_list_returns_both_tickets_in_id_order(self):
        # 首次 --has-draft 查询之后再执行普通 list
        self.assertEqual(self.run_list(self.db_path, "--has-draft"), [])

        tickets = self.run_list(self.db_path)
        # 按编号整数升序返回编号 7、12，字段与值即原工单
        self.assertEqual(tickets, [LEGACY_TICKET_7, LEGACY_TICKET_12])
        self.assertEqual([t["id"] for t in tickets], [7, 12])
        for ticket in tickets:
            self.assert_ticket_shape(ticket)
            # 每项与同编号 show 结果完全一致，不带备注、历史或草稿字段
            self.assertEqual(ticket, self.run_show(self.db_path, ticket["id"]))
            for extra in ("notes", "history", "draft", "text"):
                self.assertNotIn(extra, ticket)

    def test_repeated_queries_in_new_processes_are_identical(self):
        first_empty = self.run_list(self.db_path, "--has-draft")
        first_list = self.run_list(self.db_path)
        before = self.snapshot(self.db_path)

        # 重复启动查询（每次都是全新进程）：结果逐字段一致
        self.assertEqual(self.run_list(self.db_path, "--has-draft"), first_empty)
        self.assertEqual(self.run_list(self.db_path, "--has-draft"), [])
        self.assertEqual(self.run_list(self.db_path), first_list)
        self.assertEqual(
            self.run_list(self.db_path), [LEGACY_TICKET_7, LEGACY_TICKET_12]
        )

        # 全部查询只读：记录与首次查询后完全一致
        self.assertEqual(self.snapshot(self.db_path), before)

    # ---------- 空/纯空白关键字：退出 1 ----------

    def test_blank_keyword_on_never_opened_legacy_db_exits_1(self):
        # 在尚未被产品打开过的同类旧库上直接发起失败查询
        pristine = self.make_pristine_db()
        before = self.snapshot(pristine)

        for blank in ("", "   ", "\t \n"):
            with self.subTest(blank=blank):
                result = self.run_cli(
                    pristine, "list", "--has-draft", "--keyword", blank
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "关键字不能为空\n")

        # 失败查询不改动任何原始记录；允许补建空表，但不得有占位记录
        self.assert_records_preserved(before, pristine)

        # 失败之后首次成功查询：仍返回空数组与原始两张工单
        self.assertEqual(self.run_list(pristine, "--has-draft"), [])
        self.assertEqual(
            self.run_list(pristine), [LEGACY_TICKET_7, LEGACY_TICKET_12]
        )
        self.assert_records_preserved(before, pristine)

    # ---------- 无效数量优先：退出 2 且保持缺表状态 ----------

    def test_zero_limit_with_blank_keyword_exits_2_before_opening_db(self):
        pristine = self.make_pristine_db()
        before = self.snapshot(pristine)

        for blank in ("", "   "):
            with self.subTest(blank=blank):
                result = self.run_cli(
                    pristine,
                    "list",
                    "--has-draft",
                    "--keyword",
                    blank,
                    "--limit",
                    "0",
                )
                # 用法错误优先于关键字校验：退出 2 而非 1
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage", result.stderr.lower())
                self.assertIn("--limit", result.stderr)

        # 参数解析发生在打开数据库之前：库保持调用前的缺表状态，
        # 连缺失的表也未补建，快照逐字节等价
        self.assertEqual(self.snapshot(pristine), before)


class ListHasDraftTicketsOnlyLegacyTestCase(
    LegacyListHasDraftCompatMixin, unittest.TestCase
):
    """第一类旧库：文件首次调用前仅有 tickets 表。"""

    DB_FILENAME = "legacy_tickets_only.sqlite"
    build_database = staticmethod(build_tickets_only_db)

    def test_notes_and_history_tables_created_empty(self):
        # 首次查询后补建的备注与历史表必须为空：不推断旧工单过去的处理过程
        self.assertEqual(self.run_list(self.db_path, "--has-draft"), [])
        after = self.snapshot(self.db_path)
        self.assertEqual(after["ticket_notes"], [])
        self.assertEqual(after["ticket_history"], [])
        self.assertEqual(after["ticket_drafts"], [])


class ListHasDraftNotesHistoryLegacyTestCase(
    LegacyListHasDraftCompatMixin, unittest.TestCase
):
    """第二类旧库：有 tickets、ticket_notes、ticket_history，没有 ticket_drafts。"""

    DB_FILENAME = "legacy_tickets_notes_history.sqlite"
    build_database = staticmethod(build_tickets_notes_history_db)

    def test_existing_notes_and_history_rows_preserved(self):
        before = self.snapshot(self.db_path)
        # 预置的备注与历史（含自增编号、序号、首尾空格）逐行核对
        self.assertEqual(before["ticket_notes"], EXPECTED_NOTE_ROWS)
        self.assertEqual(before["ticket_history"], EXPECTED_HISTORY_ROWS)
        self.assertNotIn("ticket_drafts", before)

        self.assertEqual(self.run_list(self.db_path, "--has-draft"), [])
        self.assertEqual(
            self.run_list(self.db_path), [LEGACY_TICKET_7, LEGACY_TICKET_12]
        )

        after = self.snapshot(self.db_path)
        # 查询后原备注、原历史的完整记录及序号不变；草稿表补建为空
        self.assertEqual(after["ticket_notes"], EXPECTED_NOTE_ROWS)
        self.assertEqual(after["ticket_history"], EXPECTED_HISTORY_ROWS)
        self.assertEqual(after["ticket_drafts"], [])


if __name__ == "__main__":
    unittest.main()
