"""list --has-draft 对旧版本数据库兼容的回归测试。

现有 list --has-draft 测试（test_list_has_draft.py）覆盖由当前版本创建的
新库；本模块手工构造两类产品升级前可能留存的旧版本 SQLite 文件（均没有
ticket_drafts 表），通过真实命令行入口
（python main.py --db <临时文件> list ...）核对首次打开旧库时的公开行为：

1. 仅有 tickets 表的最早期旧库：没有 ticket_notes、ticket_history，
   也没有 ticket_drafts。
2. 已有 tickets、ticket_notes 与 ticket_history、但没有 ticket_drafts
   的中期旧库。

两类旧库预置相同的两张工单（编号不连续，取自真实旧库可能的存量）：
    编号 7  “打印异常”，描述为空字符串，状态 closed；
    编号 12 “登录失败”，描述为“更换网络后仍无法登录”，状态 open。
第二类旧库再为编号 7 预置 note_id 1、正文为“ 已复现 ”（首尾空格按原文
保留）的备注，以及 event_id 1、从 open 到 closed 的状态历史。

首次以 --db 指向样例文件执行 list --has-draft：退出 0、标准错误为空、
标准输出只有一个空 JSON 数组（旧库没有任何草稿）；随后普通 list 按编号
升序返回这两张工单，每项与 show 的四字段、类型和原值完全一致，不附带
备注、历史或草稿字段。重复启动查询结果一致；补建的 ticket_drafts 为空，
第一类补建的 ticket_notes / ticket_history 也为空，不推断旧工单过去的
处理过程。

异常用例各自从尚未被产品打开过的旧库开始：
    list --has-draft 配合显式空字符串或纯空白 --keyword：退出 1、标准
    输出为空、标准错误仅为“关键字不能为空”及换行；同一输入再带
    --limit 0 时优先按用法错误退出 2，标准输出为空、标准错误包含用法
    提示与 --limit，且数据库保持调用前的缺表状态（用法错误发生在打开
    数据库之前）。

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

# 两类旧库共用的固定合成工单：编号 7 已结案、描述为空；编号 12 仍打开
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

# 第二类旧库中编号 7 的既有处理记录：首尾空格必须按原文保留
NOTE_TEXT_T7 = " 已复现 "

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


def insert_legacy_tickets(conn):
    conn.executemany(
        "INSERT INTO tickets (id, title, description, status) "
        "VALUES (?, ?, ?, ?)",
        [
            (
                LEGACY_TICKET_7["id"],
                LEGACY_TICKET_7["title"],
                LEGACY_TICKET_7["description"],
                LEGACY_TICKET_7["status"],
            ),
            (
                LEGACY_TICKET_12["id"],
                LEGACY_TICKET_12["title"],
                LEGACY_TICKET_12["description"],
                LEGACY_TICKET_12["status"],
            ),
        ],
    )


def build_tickets_only_db(path):
    """构造第一类旧库：文件中仅有 tickets 表，含编号 7、12 两张旧工单。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(TICKETS_DDL)
        insert_legacy_tickets(conn)
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
        insert_legacy_tickets(conn)
        # 自增主键 id 也显式固定，便于逐行核对编号不变
        conn.execute(
            "INSERT INTO ticket_notes (id, ticket_id, note_id, text) "
            "VALUES (?, ?, ?, ?)",
            (1, LEGACY_TICKET_7["id"], 1, NOTE_TEXT_T7),
        )
        conn.execute(
            "INSERT INTO ticket_history "
            "(id, ticket_id, event_id, from_status, to_status) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, LEGACY_TICKET_7["id"], 1, "open", "closed"),
        )
        conn.commit()
    finally:
        conn.close()


class LegacyListHasDraftCompatMixin:
    """两类旧库共用的测试流程；具体旧库形态由子类提供。

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

    def make_pristine_db(self, name):
        """另建一个从未被当前版本产品打开过的同类旧库，返回其路径。"""
        path = str(Path(self._tmpdir.name) / name)
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
        """执行 list 命令，要求成功并返回解析后的唯一 JSON 数组。"""
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
        self.assertEqual(stripped[end:].strip(), "")
        self.assertIsInstance(value, dict)
        return value

    def assertTicketShape(self, ticket):
        """与 show 相同的四字段：id 为整数，其余为字符串，无备注/历史/草稿字段。"""
        self.assertIsInstance(ticket, dict)
        self.assertEqual(set(ticket.keys()), TICKET_KEYS)
        # bool 虽是 int 子类但不合法
        self.assertIs(type(ticket["id"]), int)
        for key in ("title", "description", "status"):
            self.assertIs(type(ticket[key]), str)

    @staticmethod
    def snapshot(db_path):
        """导出当时已存在各表的全部行（含自增编号）及表清单，用于逐行比对。"""
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            data = {"tables": tables}
            if "tickets" in tables:
                data["tickets"] = conn.execute(
                    "SELECT id, title, description, status "
                    "FROM tickets ORDER BY id"
                ).fetchall()
            if "ticket_notes" in tables:
                data["ticket_notes"] = conn.execute(
                    "SELECT id, ticket_id, note_id, text "
                    "FROM ticket_notes ORDER BY ticket_id, note_id, id"
                ).fetchall()
            if "ticket_history" in tables:
                data["ticket_history"] = conn.execute(
                    "SELECT id, ticket_id, event_id, from_status, to_status "
                    "FROM ticket_history ORDER BY ticket_id, event_id"
                ).fetchall()
            if "ticket_drafts" in tables:
                data["ticket_drafts"] = conn.execute(
                    "SELECT id, ticket_id, text "
                    "FROM ticket_drafts ORDER BY ticket_id"
                ).fetchall()
            return data
        finally:
            conn.close()

    def assert_records_preserved(self, before, db_path):
        """原工单、原备注与原历史全部记录（编号、正文）不变；草稿不得出现任何行。

        允许按既有规则补建缺失的 ticket_notes / ticket_history /
        ticket_drafts 空表，但补建出的表必须存在且为空，不产生占位记录，
        也不推断旧工单过去的处理过程。
        """
        after = self.snapshot(db_path)
        for table in ("tickets", "ticket_notes", "ticket_history", "ticket_drafts"):
            self.assertIn(table, after["tables"], f"缺少应已补建的表: {table}")
        self.assertEqual(after["tickets"], before["tickets"])
        # 第一类旧库最初没有 ticket_notes / ticket_history：
        # 允许补建成空表，但不得有记录
        self.assertEqual(after["ticket_notes"], before.get("ticket_notes", []))
        self.assertEqual(after["ticket_history"], before.get("ticket_history", []))
        # 旧库没有任何草稿：补建后的 ticket_drafts 必须为空表
        self.assertEqual(after["ticket_drafts"], [])

    # ---------- 需求验收主流程 ----------

    def test_first_open_has_draft_empty_then_plain_list(self):
        before = self.snapshot(self.db_path)

        # 旧库首次被当前版本打开：list --has-draft 成功返回空数组
        result = self.run_cli(self.db_path, "list", "--has-draft")
        self.assertEqual(result.returncode, 0, f"list 失败: {result.stderr!r}")
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), "[]")
        self.assertEqual(self.parse_single_json_array(result.stdout), [])

        # 随后普通 list：按编号升序返回编号 7、12 两张工单
        tickets = self.run_list(self.db_path)
        self.assertEqual(tickets, [LEGACY_TICKET_7, LEGACY_TICKET_12])
        self.assertEqual([t["id"] for t in tickets], [7, 12])
        for ticket in tickets:
            self.assertTicketShape(ticket)
            # 每项的字段、类型及内容与同编号 show 结果完全一致
            self.assertEqual(ticket, self.run_show(self.db_path, ticket["id"]))
            # 不附带备注、历史或草稿字段
            for extra in ("notes", "history", "draft", "text"):
                self.assertNotIn(extra, ticket)

        # 重复启动查询仍得到同样结果（跨进程可重复）
        repeated = self.run_cli(self.db_path, "list", "--has-draft")
        self.assertEqual(repeated.returncode, 0)
        self.assertEqual(repeated.stderr, "")
        self.assertEqual(self.parse_single_json_array(repeated.stdout), [])
        self.assertEqual(
            self.run_list(self.db_path), [LEGACY_TICKET_7, LEGACY_TICKET_12]
        )

        # 全部过程后：原工单、原备注与原历史逐行不变；
        # 补建的草稿表为空，第一类补建的备注和历史表也为空
        self.assert_records_preserved(before, self.db_path)

    # ---------- 空/纯空白关键字：退出 1 ----------

    def test_blank_keyword_exits_1_on_pristine_db(self):
        for index, blank in enumerate(("", "   ", "\t \n")):
            with self.subTest(blank=blank):
                # 每个输入各自从尚未被产品打开过的旧库开始
                pristine = self.make_pristine_db(f"pristine_keyword_{index}.sqlite")
                before = self.snapshot(pristine)

                result = self.run_cli(
                    pristine, "list", "--has-draft", "--keyword", blank
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "关键字不能为空\n")

                # 允许正常连接时补建缺表，但不新增占位记录
                self.assert_records_preserved(before, pristine)

    # ---------- 无效数量优先：退出 2 ----------

    def test_zero_limit_exits_2_and_keeps_missing_tables(self):
        for index, blank in enumerate(("", "   ")):
            with self.subTest(blank=blank):
                pristine = self.make_pristine_db(f"pristine_limit_{index}.sqlite")
                before = self.snapshot(pristine)

                # 同一输入再带 --limit 0：argparse 先拒绝数量，退出 2 而非 1
                result = self.run_cli(
                    pristine,
                    "list",
                    "--has-draft",
                    "--keyword",
                    blank,
                    "--limit",
                    "0",
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage", result.stderr.lower())
                self.assertIn("--limit", result.stderr)

                # 用法错误发生在打开数据库之前：缺表状态与全部记录保持调用前原样
                self.assertEqual(self.snapshot(pristine), before)


class ListHasDraftTicketsOnlyLegacyTestCase(
    LegacyListHasDraftCompatMixin, unittest.TestCase
):
    """第一类旧库：文件首次调用前仅有 tickets 表。"""

    DB_FILENAME = "legacy_tickets_only.sqlite"
    build_database = staticmethod(build_tickets_only_db)


class ListHasDraftTicketsNotesHistoryLegacyTestCase(
    LegacyListHasDraftCompatMixin, unittest.TestCase
):
    """第二类旧库：有 tickets、ticket_notes、ticket_history，没有 ticket_drafts。"""

    DB_FILENAME = "legacy_tickets_notes_history.sqlite"
    build_database = staticmethod(build_tickets_notes_history_db)


if __name__ == "__main__":
    unittest.main()
