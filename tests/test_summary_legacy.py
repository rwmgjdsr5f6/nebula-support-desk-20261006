"""summary 命令对旧版本数据库兼容的回归测试。

现有 summary 测试（test_summary.py）覆盖由当前版本创建的新库；本模块
手工构造两类产品升级前可能留存的旧版本 SQLite 文件，通过真实命令行
入口（python main.py --db <临时文件> summary <编号>）核对摘要在旧库
上的公开行为：

1. 仅有 tickets 表的最早期旧库：工单 1 为一条过去已结案、标题为
   “旧登录问题”、描述为空字符串的旧工单，没有任何备注，也没有
   ticket_notes、ticket_history 表。summary 1 必须原样返回该工单，
   note_count 与 status_change_count 为整数 0，latest_note 为 null。
2. 已有 tickets 与 ticket_notes、但没有 ticket_history 的中期旧库：
   工单 1 同样是已结案的“旧登录问题”，带有 note_id 1“已复现”与
   note_id 2“ 等待确认 ”（首尾空格按原文保留）两条备注；工单 2 为
   标题“旧打印问题”、描述为空的 open 工单，只有一条“独立记录”备注。
   summary 1 的 note_count 为 2、latest_note 为工单 1 的 note_id 2
   完整原文，status_change_count 仍为 0（不因当前已结案而补记历史），
   且不混入工单 2 的任何数据。

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

SUMMARY_KEYS = {"ticket", "note_count", "latest_note", "status_change_count"}
TICKET_KEYS = {"id", "title", "description", "status"}
NOTE_KEYS = {"note_id", "ticket_id", "text"}

# 两类旧库共用的固定合成工单
LEGACY_TICKET_1 = {
    "id": 1,
    "title": "旧登录问题",
    "description": "",
    "status": "closed",
}
# 仅第二类旧库存在的第二张工单
LEGACY_TICKET_2 = {
    "id": 2,
    "title": "旧打印问题",
    "description": "",
    "status": "open",
}

# 工单 1 的两条旧备注：第二条首尾各含一个空格，必须按原文保留
NOTE_T1_1 = "已复现"
NOTE_T1_2 = " 等待确认 "
# 工单 2 自己的唯一备注，用于核对摘要不跨工单混入
NOTE_T2_1 = "独立记录"

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


def build_tickets_only_db(path):
    """构造第一类旧库：文件中仅有 tickets 表，含编号 1 的已结案旧工单。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(TICKETS_DDL)
        conn.execute(
            "INSERT INTO tickets (id, title, description, status) "
            "VALUES (?, ?, ?, ?)",
            (
                LEGACY_TICKET_1["id"],
                LEGACY_TICKET_1["title"],
                LEGACY_TICKET_1["description"],
                LEGACY_TICKET_1["status"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def build_tickets_with_notes_db(path):
    """构造第二类旧库：有 tickets 与 ticket_notes，没有 ticket_history。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(TICKETS_DDL)
        conn.execute(TICKET_NOTES_DDL)
        conn.executemany(
            "INSERT INTO tickets (id, title, description, status) "
            "VALUES (?, ?, ?, ?)",
            [
                (
                    LEGACY_TICKET_1["id"],
                    LEGACY_TICKET_1["title"],
                    LEGACY_TICKET_1["description"],
                    LEGACY_TICKET_1["status"],
                ),
                (
                    LEGACY_TICKET_2["id"],
                    LEGACY_TICKET_2["title"],
                    LEGACY_TICKET_2["description"],
                    LEGACY_TICKET_2["status"],
                ),
            ],
        )
        # 自增主键 id 也显式固定，便于逐行核对编号不变
        conn.executemany(
            "INSERT INTO ticket_notes (id, ticket_id, note_id, text) "
            "VALUES (?, ?, ?, ?)",
            [
                (1, 1, 1, NOTE_T1_1),
                (2, 1, 2, NOTE_T1_2),
                (3, 2, 1, NOTE_T2_1),
            ],
        )
        conn.commit()
    finally:
        conn.close()


class LegacySummaryCompatMixin:
    """两类旧库共用的测试流程；具体旧库形态与期望摘要由子类提供。

    子类需设置 ``DB_FILENAME``、``build_database``（静态构造函数），
    并实现 ``expected_summary_1`` 与 ``assert_variant_specifics``。
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
    def parse_single_json(stdout):
        """标准输出必须恰好是一个 JSON 值，不允许前后夹带说明文字。"""
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        if stripped[end:].strip() != "":
            raise AssertionError(f"标准输出不是唯一 JSON 值: {stdout!r}")
        return value

    def run_summary(self, db_path, raw_id):
        """执行 summary，要求成功并返回 (解析后的摘要 JSON, CompletedProcess)。"""
        result = self.run_cli(db_path, "summary", str(raw_id))
        self.assertEqual(result.returncode, 0, f"summary 失败: {result.stderr!r}")
        self.assertEqual(result.stderr, "")
        summary = self.parse_single_json(result.stdout)
        self.assertIsInstance(summary, dict)
        return summary, result

    def run_show(self, db_path, raw_id):
        """执行 show，要求成功并返回解析后的唯一工单 JSON 对象。"""
        result = self.run_cli(db_path, "show", str(raw_id))
        self.assertEqual(result.returncode, 0, f"show 失败: {result.stderr!r}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json(result.stdout)

    def assert_summary_envelope(self, summary):
        """摘要恰好含四个约定字段；计数与 ticket 各字段类型固定。"""
        self.assertEqual(set(summary), SUMMARY_KEYS)
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(summary["note_count"]), int)
        self.assertIs(type(summary["status_change_count"]), int)
        ticket = summary["ticket"]
        self.assertIsInstance(ticket, dict)
        self.assertEqual(set(ticket), TICKET_KEYS)
        self.assertIs(type(ticket["id"]), int)
        self.assertIs(type(ticket["title"]), str)
        self.assertIs(type(ticket["description"]), str)
        self.assertIs(type(ticket["status"]), str)

    @staticmethod
    def snapshot(db_path):
        """导出当时已存在各表的全部行（含自增编号），用于逐行比对。"""
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
            return data
        finally:
            conn.close()

    def assert_records_preserved(self, before, db_path):
        """原工单与原备注全部记录（编号、正文）不变；历史不得出现任何行。

        允许按既有规则补建缺失的 ticket_notes / ticket_history 空表，
        但补建出的表必须存在且为空，不产生占位记录或状态历史。
        """
        after = self.snapshot(db_path)
        for table in ("tickets", "ticket_notes", "ticket_history"):
            self.assertIn(table, after, f"缺少应已补建的表: {table}")
        self.assertEqual(after["tickets"], before["tickets"])
        # 第一类旧库最初没有 ticket_notes：允许补建成空表，但不得有记录
        self.assertEqual(after["ticket_notes"], before.get("ticket_notes", []))
        # 旧库没有任何状态历史：补建后的 ticket_history 必须为空表
        self.assertEqual(after["ticket_history"], [])

    # ---------- 共用测试流程 ----------

    def test_first_open_summary_returns_legacy_ticket(self):
        # 旧库首次被当前版本打开：summary 1 必须成功
        before = self.snapshot(self.db_path)
        result = self.run_cli(self.db_path, "summary", "1")
        self.assertEqual(result.returncode, 0, f"summary 失败: {result.stderr!r}")
        self.assertEqual(result.stderr, "")

        summary = self.parse_single_json(result.stdout)
        self.assert_summary_envelope(summary)
        self.assertEqual(summary, self.expected_summary_1())

        # ticket 与 show 的四字段结构和值完全一致，且就是原工单
        shown = self.run_show(self.db_path, 1)
        self.assertEqual(set(shown), TICKET_KEYS)
        self.assertEqual(summary["ticket"], shown)
        self.assertEqual(summary["ticket"], LEGACY_TICKET_1)

        # 各旧库形态特有的摘要要求（空备注 / 备注原文 / 跨工单隔离）
        self.assert_variant_specifics(summary)

        self.assert_records_preserved(before, self.db_path)

    def test_summary_in_another_process_is_identical(self):
        first, _ = self.run_summary(self.db_path, 1)
        first_show = self.run_show(self.db_path, 1)

        # 另一次全新进程查询同一个旧库文件：结果逐字段一致
        other_result = self.run_cli(self.db_path, "summary", "1")
        self.assertEqual(
            other_result.returncode, 0, f"summary 失败: {other_result.stderr!r}"
        )
        self.assertEqual(other_result.stderr, "")
        other = self.parse_single_json(other_result.stdout)
        self.assertEqual(other, first)
        self.assertEqual(other, self.expected_summary_1())
        self.assertEqual(self.run_show(self.db_path, 1), first_show)

    def test_unknown_id_on_never_opened_legacy_db(self):
        # 在尚未被产品打开过的同类旧库上直接查询不存在的编号
        pristine = self.make_pristine_db()
        before = self.snapshot(pristine)

        result = self.run_cli(pristine, "summary", "999")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")

        # 失败查询不得改动任何原始记录，也不得留下占位记录/状态历史
        self.assert_records_preserved(before, pristine)

        # 失败之后再查既有工单：仍是原摘要，证明失败没有污染旧库
        summary, _ = self.run_summary(pristine, 1)
        self.assert_summary_envelope(summary)
        self.assertEqual(summary, self.expected_summary_1())
        self.assert_records_preserved(before, pristine)

    def test_successful_failed_and_repeated_queries_leave_data_intact(self):
        before = self.snapshot(self.db_path)

        # 成功查询与重复查询
        first, _ = self.run_summary(self.db_path, 1)
        self.assertEqual(first, self.expected_summary_1())
        repeated, _ = self.run_summary(self.db_path, 1)
        self.assertEqual(repeated, first)

        # 子类补充的只读查询（第二类旧库含工单 2 的摘要与 show）
        self.assert_extra_readonly_queries()

        # 失败查询：退出码 1、标准输出为空、标准错误仅一行“工单不存在”
        failed = self.run_cli(self.db_path, "summary", "999")
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, "")
        self.assertEqual(failed.stderr, "工单不存在\n")

        # 失败后再次成功查询，摘要仍与首次一致
        again, _ = self.run_summary(self.db_path, 1)
        self.assertEqual(again, first)

        # 全部过程后：原工单、原备注的编号与正文逐行不变，历史仍为空
        self.assert_records_preserved(before, self.db_path)

    # ---------- 子类钩子 ----------

    def expected_summary_1(self):  # pragma: no cover - 由子类覆盖
        raise NotImplementedError

    def assert_variant_specifics(self, summary):  # pragma: no cover - 子类覆盖
        raise NotImplementedError

    def assert_extra_readonly_queries(self):
        """只读性用例中子类可追加的查询；默认仅核对 show 1。"""
        self.assertEqual(self.run_show(self.db_path, 1), LEGACY_TICKET_1)


class SummaryTicketsOnlyLegacyTestCase(
    LegacySummaryCompatMixin, unittest.TestCase
):
    """第一类旧库：文件首次调用前仅有 tickets 表。"""

    DB_FILENAME = "legacy_tickets_only.sqlite"
    build_database = staticmethod(build_tickets_only_db)

    def expected_summary_1(self):
        return {
            "ticket": dict(LEGACY_TICKET_1),
            "note_count": 0,
            "latest_note": None,
            "status_change_count": 0,
        }

    def assert_variant_specifics(self, summary):
        # 无备注、无历史：两个计数都是整数 0，最新备注为 JSON null
        self.assertEqual(summary["note_count"], 0)
        self.assertIs(type(summary["note_count"]), int)
        self.assertEqual(summary["status_change_count"], 0)
        self.assertIs(type(summary["status_change_count"]), int)
        self.assertIsNone(summary["latest_note"])


class SummaryTicketsNotesWithoutHistoryLegacyTestCase(
    LegacySummaryCompatMixin, unittest.TestCase
):
    """第二类旧库：有 tickets 与 ticket_notes，但没有 ticket_history。"""

    DB_FILENAME = "legacy_tickets_notes.sqlite"
    build_database = staticmethod(build_tickets_with_notes_db)

    def expected_summary_1(self):
        return {
            "ticket": dict(LEGACY_TICKET_1),
            "note_count": 2,
            "latest_note": {
                "note_id": 2,
                "ticket_id": 1,
                "text": NOTE_T1_2,
            },
            "status_change_count": 0,
        }

    def expected_summary_2(self):
        return {
            "ticket": dict(LEGACY_TICKET_2),
            "note_count": 1,
            "latest_note": {
                "note_id": 1,
                "ticket_id": 2,
                "text": NOTE_T2_1,
            },
            "status_change_count": 0,
        }

    def assert_variant_specifics(self, summary):
        # 工单 1 有两条备注；不因当前已结案而补记任何状态历史
        self.assertEqual(summary["note_count"], 2)
        self.assertIs(type(summary["note_count"]), int)
        self.assertEqual(summary["status_change_count"], 0)
        self.assertIs(type(summary["status_change_count"]), int)

        note = summary["latest_note"]
        self.assertIsInstance(note, dict)
        self.assertEqual(set(note), NOTE_KEYS)
        self.assertIs(type(note["note_id"]), int)
        self.assertIs(type(note["ticket_id"]), int)
        self.assertIs(type(note["text"]), str)
        # 最新备注为 note_id 2、ticket_id 1，首尾空格完整保留
        self.assertEqual(
            note,
            {"note_id": 2, "ticket_id": 1, "text": NOTE_T1_2},
        )

        # 工单 2 的摘要只含它自己的数据：编号、备注均不与工单 1 混入
        summary_2, _ = self.run_summary(self.db_path, 2)
        self.assert_summary_envelope(summary_2)
        self.assertEqual(summary_2, self.expected_summary_2())
        self.assertEqual(summary_2["ticket"], self.run_show(self.db_path, 2))
        self.assertEqual(summary_2["ticket"], LEGACY_TICKET_2)

    def assert_extra_readonly_queries(self):
        super().assert_extra_readonly_queries()
        # 工单 2 在交错查询中同样保持自己的独立摘要
        summary_2, _ = self.run_summary(self.db_path, 2)
        self.assertEqual(summary_2, self.expected_summary_2())


if __name__ == "__main__":
    unittest.main()
