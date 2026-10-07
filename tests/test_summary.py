"""summary 命令的回归测试。

通过真实命令行入口（python main.py）核对处理摘要的公开行为：
固定样例从空库创建两张合成工单，覆盖空摘要、备注与状态变更计数、
最新备注结构（含每工单独立编号与原文空格保留）、跨工单互不混入、
重复查询与跨进程一致性，以及编号格式、编号不存在与用法三类确定的
失败结果和查询的只读性。

每个用例使用独立临时目录，通过 --db 明确指定临时 SQLite 文件，
测试结束后清理，不读取或改动工作目录下的默认库 tickets.sqlite，
也不依赖网络或第三方包。比较一律基于解析后的 JSON 值、字段类型，
不依赖键顺序或排版空格；建表初始化产生的空表不计为记录变更。

运行方式（项目根目录）：
    python -m unittest discover -s tests
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = PROJECT_ROOT / "main.py"

# 超过 SQLite INTEGER 主键最大值的编号：按正整数解析但不可能命中记录
OVERSIZED_ID = "9223372036854775808"

# 固定样例：两张合成工单
TICKET_A = {"title": "登录失败", "description": "重置密码后仍无法登录"}
TICKET_B = {"title": "打印异常", "description": "更换网络后无法打印"}

# 第一张工单上的两条备注：第二条首尾空白必须按原文保留
NOTE_A_1 = "已复现"
NOTE_A_2 = " 等待确认 "
# 期间加到第二张工单上的独立备注
NOTE_B_1 = "独立记录"

SUMMARY_KEYS = {"ticket", "note_count", "latest_note", "status_change_count"}
TICKET_KEYS = {"id", "title", "description", "status"}
NOTE_KEYS = {"note_id", "ticket_id", "text"}


class SummaryWorkflowTestCase(unittest.TestCase):
    """摘要正常流程与边界；每个用例使用独立临时数据库，预置两张 open 工单。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.id_a = self._create_ticket(TICKET_A["title"], TICKET_A["description"])
        self.id_b = self._create_ticket(TICKET_B["title"], TICKET_B["description"])

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

    @staticmethod
    def parse_single_json(stdout):
        """标准输出必须恰好是一个 JSON 值，不允许前后夹带说明文字。"""
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        if stripped[end:].strip() != "":
            raise AssertionError(f"标准输出不是唯一 JSON 值: {stdout!r}")
        return value

    def expected_ticket(self, ticket_id, sample, status):
        return {
            "id": ticket_id,
            "title": sample["title"],
            "description": sample["description"],
            "status": status,
        }

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

    def add_note(self, ticket_id, text):
        result = self.run_cli("add-note", str(ticket_id), "--text", text)
        self.assertEqual(result.returncode, 0, f"add-note 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json(result.stdout)

    def assertSummaryTicket(self, summary, expected):
        """ticket 字段与 show 返回的工单同结构、同数据。"""
        ticket = summary["ticket"]
        self.assertIsInstance(ticket, dict)
        self.assertEqual(set(ticket), TICKET_KEYS)
        self.assertIs(type(ticket["id"]), int)
        self.assertIs(type(ticket["title"]), str)
        self.assertIs(type(ticket["description"]), str)
        self.assertIs(type(ticket["status"]), str)
        self.assertEqual(ticket, expected)

    def assertSummaryShape(self, summary):
        """摘要恰好包含四个约定字段，计数字段为整数。"""
        self.assertEqual(set(summary), SUMMARY_KEYS)
        self.assertIs(type(summary["note_count"]), int)
        self.assertIs(type(summary["status_change_count"]), int)

    def assertNoteObject(self, note, note_id, ticket_id, text):
        """latest_note 使用既有备注对象结构：仅含 note_id/ticket_id/text。"""
        self.assertIsInstance(note, dict)
        self.assertEqual(set(note), NOTE_KEYS)
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(note["note_id"]), int)
        self.assertIs(type(note["ticket_id"]), int)
        self.assertIs(type(note["text"]), str)
        self.assertEqual(
            note,
            {"note_id": note_id, "ticket_id": ticket_id, "text": text},
        )

    def snapshot(self):
        """导出三张表的全部行（含编号），用于核对查询前后无任何记录变更。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return {
                "tickets": conn.execute(
                    "SELECT id, title, description, status "
                    "FROM tickets ORDER BY id"
                ).fetchall(),
                "notes": conn.execute(
                    "SELECT ticket_id, note_id, text "
                    "FROM ticket_notes ORDER BY ticket_id, note_id"
                ).fetchall(),
                "history": conn.execute(
                    "SELECT ticket_id, event_id, from_status, to_status "
                    "FROM ticket_history ORDER BY ticket_id, event_id"
                ).fetchall(),
            }
        finally:
            conn.close()

    # ---------- 空摘要 ----------

    def test_fresh_ticket_summary_is_empty_and_matches_show(self):
        # 新建工单没有备注与状态切换：摘要中的 ticket 与 show 完全相同
        summary = self.summary(self.id_a)
        self.assertSummaryShape(summary)
        self.assertSummaryTicket(
            summary, self.expected_ticket(self.id_a, TICKET_A, "open")
        )
        self.assertEqual(summary["ticket"], self.show(self.id_a))

        # 两个计数均为整数 0（创建产生的初始 open 不算状态变化）
        self.assertEqual(summary["note_count"], 0)
        self.assertEqual(summary["status_change_count"], 0)
        # JSON null 解析为 Python None
        self.assertIsNone(summary["latest_note"])

    # ---------- 完整语义验收 ----------

    def test_acceptance_two_tickets_full_summary_semantics(self):
        # 第一张工单先有两条备注；期间第二张工单留下自己的独立记录
        self.add_note(self.id_a, NOTE_A_1)
        self.add_note(self.id_b, NOTE_B_1)
        self.add_note(self.id_a, NOTE_A_2)

        # 第一张依次结案、重复结案、重开：仅两次实际状态切换
        first_close = self.run_cli("close", str(self.id_a))
        self.assertEqual(first_close.returncode, 0)
        repeated_close = self.run_cli("close", str(self.id_a))
        self.assertEqual(repeated_close.returncode, 0)
        reopen = self.run_cli("reopen", str(self.id_a))
        self.assertEqual(reopen.returncode, 0)
        self.assertEqual(reopen.stderr, "")

        summary_a = self.summary(self.id_a)
        self.assertSummaryShape(summary_a)

        # 工单当前为 open，且与 show 返回的对象一致
        self.assertSummaryTicket(
            summary_a, self.expected_ticket(self.id_a, TICKET_A, "open")
        )
        self.assertEqual(summary_a["ticket"], self.show(self.id_a))

        # 两条备注，两次状态变化
        self.assertEqual(summary_a["note_count"], 2)
        self.assertEqual(summary_a["status_change_count"], 2)

        # 最新备注使用既有备注对象结构，编号为 2、归属第一张工单，
        # 第二条备注首尾空格完整保留
        self.assertNoteObject(summary_a["latest_note"], 2, self.id_a, NOTE_A_2)

        # notes 同样返回两条原文，顺序与编号在该工单内递增
        notes_a = self.parse_single_json(self.run_cli("notes", str(self.id_a)).stdout)
        self.assertEqual(
            notes_a,
            [
                {"note_id": 1, "ticket_id": self.id_a, "text": NOTE_A_1},
                {"note_id": 2, "ticket_id": self.id_a, "text": NOTE_A_2},
            ],
        )

        # 第二张工单的摘要只含自己的备注：编号从 1 开始，状态变化仍为 0
        summary_b = self.summary(self.id_b)
        self.assertSummaryShape(summary_b)
        self.assertSummaryTicket(
            summary_b, self.expected_ticket(self.id_b, TICKET_B, "open")
        )
        self.assertEqual(summary_b["note_count"], 1)
        self.assertEqual(summary_b["status_change_count"], 0)
        self.assertNoteObject(summary_b["latest_note"], 1, self.id_b, NOTE_B_1)

        # 第一张的历史只有两次实际切换，重复结案不占序号
        history_a = self.parse_single_json(
            self.run_cli("history", str(self.id_a)).stdout
        )
        self.assertEqual(
            [(e["event_id"], e["from_status"], e["to_status"]) for e in history_a],
            [(1, "open", "closed"), (2, "closed", "open")],
        )
        # 第二张没有任何状态变化
        history_b = self.parse_single_json(
            self.run_cli("history", str(self.id_b)).stdout
        )
        self.assertEqual(history_b, [])

    # ---------- 重复查询与跨进程一致 ----------

    def test_repeated_queries_in_separate_processes_return_identical_result(self):
        self.add_note(self.id_a, NOTE_A_1)
        self.run_cli("close", str(self.id_a))

        # run_cli 每次都是全新进程：同进程内重复读取与“另一次进程”
        # 读取同一数据库得到的摘要在字段、类型与数据上完全一致
        results = [self.summary(self.id_a) for _ in range(3)]
        for other in results[1:]:
            self.assertEqual(other, results[0])

        # 空摘要同样可重复
        empty_results = [self.summary(self.id_b) for _ in range(2)]
        self.assertEqual(empty_results[1], empty_results[0])
        self.assertIsNone(empty_results[0]["latest_note"])

    # ---------- 编号写法 ----------

    def test_id_spellings_with_plus_zeros_and_padding_read_same_ticket(self):
        self.run_cli("close", str(self.id_a))
        expected = self.summary(self.id_a)

        # 正号、前导零、首尾空白（含组合写法）解析为同一编号
        for raw in (
            f"+{self.id_a}",
            f"0{self.id_a}",
            f"  {self.id_a}  ",
            f" +0{self.id_a} ",
        ):
            with self.subTest(raw=raw):
                result = self.run_cli("summary", raw)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.parse_single_json(result.stdout), expected)

    # ---------- 编号格式错误 ----------

    def test_non_positive_integer_ids_exit_1_with_format_error(self):
        for bad_id in ("abc", "0", "-1"):
            with self.subTest(bad_id=bad_id):
                before = self.snapshot()
                result = self.run_cli("summary", bad_id)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单编号必须为正整数\n")
                self.assertEqual(self.snapshot(), before)

    # ---------- 编号不存在 / 超界 ----------

    def test_unknown_ids_exit_1_with_not_found_error(self):
        for raw_id in ("999", OVERSIZED_ID):
            with self.subTest(raw_id=raw_id):
                before = self.snapshot()
                result = self.run_cli("summary", raw_id)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单不存在\n")
                self.assertEqual(self.snapshot(), before)

    # ---------- 参数用法错误 ----------

    def test_usage_errors_exit_2_with_usage_message(self):
        for argv in (("summary",), ("summary", str(self.id_a), "--unknown")):
            with self.subTest(argv=argv):
                before = self.snapshot()
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage", result.stderr.lower())
                self.assertEqual(self.snapshot(), before)

    # ---------- 只读性 ----------

    def test_successful_and_failed_summaries_leave_all_records_unchanged(self):
        # 准备含备注与历史的存量数据
        self.add_note(self.id_a, NOTE_A_1)
        self.add_note(self.id_a, NOTE_A_2)
        self.add_note(self.id_b, NOTE_B_1)
        self.run_cli("close", str(self.id_a))
        self.run_cli("close", str(self.id_a))
        self.run_cli("reopen", str(self.id_a))

        before = self.snapshot()

        # 成功查询（含重复）与各类失败查询交错进行
        self.summary(self.id_a)
        self.summary(self.id_b)
        for raw_id in ("abc", "0", "-1", "999", OVERSIZED_ID):
            failed = self.run_cli("summary", raw_id)
            self.assertEqual(failed.returncode, 1)
        for argv in (("summary",), ("summary", str(self.id_a), "--unknown")):
            self.assertEqual(self.run_cli(*argv).returncode, 2)
        self.summary(self.id_a)

        # 工单、备注、历史的内容与编号前后完全相同
        self.assertEqual(self.snapshot(), before)


class SummaryEmptyDatabaseTestCase(unittest.TestCase):
    """空库上的失败查询：建表初始化不产生任何记录。"""

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

    def test_usage_error_before_connect_does_not_create_database(self):
        # 缺少编号时 argparse 在打开数据库前退出，不应留下数据库文件
        result = self.run_cli("summary")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertFalse(os.path.exists(self.db_path))

    def test_failed_lookup_initializes_only_empty_tables(self):
        # 查询不存在的编号会初始化建表，但三张表都必须是空表：
        # 建表初始化不是记录变更
        result = self.run_cli("summary", "1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")

        conn = sqlite3.connect(self.db_path)
        try:
            for table in ("tickets", "ticket_notes", "ticket_history"):
                self.assertEqual(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0,
                )
        finally:
            conn.close()

        # 再次失败查询后仍无记录
        repeated = self.run_cli("summary", "1")
        self.assertEqual(repeated.returncode, 1)
        self.assertEqual(repeated.stderr, "工单不存在\n")
        conn = sqlite3.connect(self.db_path)
        try:
            for table in ("tickets", "ticket_notes", "ticket_history"):
                self.assertEqual(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0,
                )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
