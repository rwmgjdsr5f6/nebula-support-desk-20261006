"""summary 命令对旧版本数据库的兼容回归测试。

通过真实命令行入口（python main.py --db 文件路径 summary 编号）核对
两类旧库边界的公开行为：

- 旧库一：首次调用前仅有 tickets 表（无 ticket_notes、ticket_history），
  内含一张已结案的旧工单；summary 应成功返回原工单，note_count 与
  status_change_count 均为整数 0，latest_note 为 JSON null。
- 旧库二：已有 tickets 与 ticket_notes，但没有 ticket_history；工单 1
  已有两条备注（后一条正文首尾各含一个空格），工单 2 有自己的独立备注。
  summary 应统计既有备注、保留最新备注完整原文，status_change_count
  仍为 0：不根据已结案状态补记历史，也不混入另一张工单的数据。

每类旧库均核对：首次查询与另一次进程查询返回相同摘要；成功时退出码
为 0、标准错误为空、标准输出仅含现有四字段摘要对象，ticket 与 show
的四字段结构和值一致；在尚未被产品打开的同类旧库中查询不存在的编号
999，退出 1、标准输出为空、标准错误仅含"工单不存在"及换行。成功、
失败及重复查询前后，原工单与原备注的全部记录、编号和正文均不变；
允许按既有规则补建缺失的空表，但不产生占位记录或状态历史。

每个用例使用独立临时目录中的合成旧库文件，测试结束后清理，不读取或
改动工作目录下的默认库 tickets.sqlite，只使用 Python 标准库。JSON
比较以字段集合、类型和值为准，不依赖键序和排版。

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

# 旧库一样例：唯一一张已结案旧工单（描述为空字符串）
LEGACY_TICKET_1 = {"id": 1, "title": "旧登录问题", "description": "", "status": "closed"}
# 旧库二追加的第二张工单
LEGACY_TICKET_2 = {"id": 2, "title": "旧打印问题", "description": "", "status": "open"}

# 旧库二中工单 1 的两条既有备注：第二条正文首尾各含一个空格，须按原文保留
LEGACY_NOTE_1 = {"note_id": 1, "ticket_id": 1, "text": "已复现"}
LEGACY_NOTE_2 = {"note_id": 2, "ticket_id": 1, "text": " 等待确认 "}
# 工单 2 的独立备注
LEGACY_NOTE_3 = {"note_id": 1, "ticket_id": 2, "text": "独立记录"}

SUMMARY_KEYS = {"ticket", "note_count", "latest_note", "status_change_count"}
TICKET_KEYS = {"id", "title", "description", "status"}
NOTE_KEYS = {"note_id", "ticket_id", "text"}

# 旧库一建库：仅 tickets 表（列结构与现行一致，无备注表、无历史表）
LEGACY_TICKETS_DDL = """
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL
)
"""
# 旧库二在旧库一基础上已有 ticket_notes（列结构与现行一致），仍无 ticket_history
LEGACY_NOTES_DDL = """
CREATE TABLE ticket_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    note_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    UNIQUE (ticket_id, note_id)
)
"""


class LegacySummaryTestBase(unittest.TestCase):
    """旧库兼容用例的公共基类：独立临时旧库、真实进程调用与快照核对。"""

    # 子类返回 (建表语句列表, (工单行, ...), (备注行, ...))
    def legacy_rows(self):
        raise NotImplementedError

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "legacy_tickets.sqlite")
        self._build_legacy_db()
        # 产品首次打开前的原始快照，作为一切“记录不变”核对的基准
        self.original_snapshot = self.snapshot()

    # ---------- 旧库构造与快照 ----------

    def _build_legacy_db(self):
        ddls, ticket_rows, note_rows = self.legacy_rows()
        conn = sqlite3.connect(self.db_path)
        try:
            for ddl in ddls:
                conn.execute(ddl)
            conn.executemany(
                "INSERT INTO tickets (id, title, description, status) "
                "VALUES (?, ?, ?, ?)",
                ticket_rows,
            )
            if note_rows:
                conn.executemany(
                    "INSERT INTO ticket_notes (ticket_id, note_id, text) "
                    "VALUES (?, ?, ?)",
                    note_rows,
                )
            conn.commit()
        finally:
            conn.close()

    def snapshot(self):
        """导出各表全部行（含编号）；表不存在记为 None，以区分“缺失”与“空表”。"""
        conn = sqlite3.connect(self.db_path)
        try:
            existing = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            queries = {
                "tickets": "SELECT id, title, description, status "
                "FROM tickets ORDER BY id",
                "ticket_notes": "SELECT ticket_id, note_id, text "
                "FROM ticket_notes ORDER BY ticket_id, note_id",
                "ticket_history": "SELECT ticket_id, event_id, from_status, to_status "
                "FROM ticket_history ORDER BY ticket_id, event_id",
            }
            return {
                table: (conn.execute(sql).fetchall() if table in existing else None)
                for table, sql in queries.items()
            }
        finally:
            conn.close()

    def assertRecordsPreserved(self, before, after):
        """原工单与原备注的记录、编号、正文不变；允许补建空表，但不得产生记录。"""
        self.assertEqual(after["tickets"], before["tickets"])
        if before["ticket_notes"] is None:
            # 旧库一：允许补建缺失的备注空表，但不得出现占位记录
            self.assertIn(after["ticket_notes"], (None, []))
        else:
            self.assertEqual(after["ticket_notes"], before["ticket_notes"])
        # 两类旧库都不得产生状态历史（允许补建缺失的历史空表）
        self.assertIn(after["ticket_history"], (None, []))

    # ---------- 命令行与 JSON 辅助 ----------

    def run_cli(self, *args):
        """以真实命令行入口运行 main.py，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    @staticmethod
    def parse_single_json(stdout):
        """标准输出必须恰好是一个 JSON 值，不允许前后夹带说明文字。"""
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        if stripped[end:].strip() != "":
            raise AssertionError(f"标准输出不是唯一 JSON 值: {stdout!r}")
        return value

    def summary(self, raw_id):
        """执行 summary，要求成功并返回解析后的唯一摘要 JSON 对象。"""
        result = self.run_cli("summary", str(raw_id))
        self.assertEqual(result.returncode, 0, f"summary 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        summary = self.parse_single_json(result.stdout)
        self.assertIsInstance(summary, dict)
        return summary

    def show(self, raw_id):
        result = self.run_cli("show", str(raw_id))
        self.assertEqual(result.returncode, 0, f"show 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json(result.stdout)

    def assertSummaryShape(self, summary):
        """摘要恰好包含四个约定字段，计数字段为整数。"""
        self.assertEqual(set(summary), SUMMARY_KEYS)
        self.assertIs(type(summary["note_count"]), int)
        self.assertIs(type(summary["status_change_count"]), int)

    def assertSummaryTicket(self, summary, expected, raw_id):
        """ticket 字段为四字段结构，且与 show 返回的工单同结构、同数据。"""
        ticket = summary["ticket"]
        self.assertIsInstance(ticket, dict)
        self.assertEqual(set(ticket), TICKET_KEYS)
        self.assertIs(type(ticket["id"]), int)
        self.assertIs(type(ticket["title"]), str)
        self.assertIs(type(ticket["description"]), str)
        self.assertIs(type(ticket["status"]), str)
        self.assertEqual(ticket, expected)
        self.assertEqual(ticket, self.show(raw_id))

    def assertNoteObject(self, note, expected):
        """latest_note 使用既有备注对象结构：仅含 note_id/ticket_id/text。"""
        self.assertIsInstance(note, dict)
        self.assertEqual(set(note), NOTE_KEYS)
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(note["note_id"]), int)
        self.assertIs(type(note["ticket_id"]), int)
        self.assertIs(type(note["text"]), str)
        self.assertEqual(note, expected)

    # ---------- 各旧库共通的失败与只读核对 ----------

    def check_unknown_id_999_on_unopened_db(self):
        """在尚未被产品打开的旧库上查询 999：退出 1，输出约定错误，记录不变。"""
        result = self.run_cli("summary", "999")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")
        self.assertRecordsPreserved(self.original_snapshot, self.snapshot())

    def check_queries_leave_records_unchanged(self, existing_ids):
        """成功、失败及重复查询交错后，原记录不变、不产生历史或占位记录。"""
        before = self.snapshot()
        for raw_id in existing_ids:
            self.summary(raw_id)
        failed = self.run_cli("summary", "999")
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, "")
        self.assertEqual(failed.stderr, "工单不存在\n")
        for raw_id in existing_ids:
            self.summary(raw_id)
        self.assertRecordsPreserved(before, self.snapshot())


class SummaryLegacyTicketsOnlyTestCase(LegacySummaryTestBase):
    """旧库一：首次调用前仅有 tickets 表，含一张已结案旧工单。"""

    def legacy_rows(self):
        ticket = LEGACY_TICKET_1
        rows = ((ticket["id"], ticket["title"], ticket["description"], ticket["status"]),)
        return [LEGACY_TICKETS_DDL], rows, ()

    def test_first_summary_returns_ticket_with_zero_counts_and_null_note(self):
        # 产品首次打开该旧库即查询：摘要成功且不含任何备注与状态历史
        summary = self.summary(LEGACY_TICKET_1["id"])
        self.assertSummaryShape(summary)
        self.assertSummaryTicket(summary, LEGACY_TICKET_1, LEGACY_TICKET_1["id"])
        self.assertEqual(summary["note_count"], 0)
        self.assertEqual(summary["status_change_count"], 0)
        # JSON null 解析为 Python None
        self.assertIsNone(summary["latest_note"])

    def test_repeated_query_in_another_process_returns_identical_summary(self):
        first = self.summary(LEGACY_TICKET_1["id"])
        second = self.summary(LEGACY_TICKET_1["id"])
        self.assertEqual(second, first)

    def test_unknown_id_999_on_unopened_db(self):
        self.check_unknown_id_999_on_unopened_db()

    def test_queries_leave_records_unchanged(self):
        self.check_queries_leave_records_unchanged([LEGACY_TICKET_1["id"]])


class SummaryLegacyNotesWithoutHistoryTestCase(LegacySummaryTestBase):
    """旧库二：已有 tickets 与 ticket_notes，但没有 ticket_history。"""

    def legacy_rows(self):
        tickets = tuple(
            (t["id"], t["title"], t["description"], t["status"])
            for t in (LEGACY_TICKET_1, LEGACY_TICKET_2)
        )
        notes = tuple(
            (n["ticket_id"], n["note_id"], n["text"])
            for n in (LEGACY_NOTE_1, LEGACY_NOTE_2, LEGACY_NOTE_3)
        )
        return [LEGACY_TICKETS_DDL, LEGACY_NOTES_DDL], tickets, notes

    def test_first_summary_counts_notes_without_backfilling_history(self):
        # 产品首次打开该旧库即查询工单 1：统计既有备注，不补记状态历史
        summary = self.summary(LEGACY_TICKET_1["id"])
        self.assertSummaryShape(summary)
        self.assertSummaryTicket(summary, LEGACY_TICKET_1, LEGACY_TICKET_1["id"])
        self.assertEqual(summary["note_count"], 2)
        # 工单虽已结案，旧库没有历史表：不根据状态补记，计数仍为 0
        self.assertEqual(summary["status_change_count"], 0)
        # 最新备注为 note_id 2，归属工单 1，首尾空格完整保留
        self.assertNoteObject(summary["latest_note"], LEGACY_NOTE_2)

    def test_summary_does_not_mix_notes_across_tickets(self):
        # 工单 2 只统计自己的独立备注，编号从 1 开始，同样无状态历史
        summary = self.summary(LEGACY_TICKET_2["id"])
        self.assertSummaryShape(summary)
        self.assertSummaryTicket(summary, LEGACY_TICKET_2, LEGACY_TICKET_2["id"])
        self.assertEqual(summary["note_count"], 1)
        self.assertEqual(summary["status_change_count"], 0)
        self.assertNoteObject(summary["latest_note"], LEGACY_NOTE_3)

    def test_repeated_query_in_another_process_returns_identical_summary(self):
        first = self.summary(LEGACY_TICKET_1["id"])
        second = self.summary(LEGACY_TICKET_1["id"])
        self.assertEqual(second, first)

    def test_unknown_id_999_on_unopened_db(self):
        self.check_unknown_id_999_on_unopened_db()

    def test_queries_leave_records_unchanged(self):
        self.check_queries_leave_records_unchanged(
            [LEGACY_TICKET_1["id"], LEGACY_TICKET_2["id"]]
        )


if __name__ == "__main__":
    unittest.main()
