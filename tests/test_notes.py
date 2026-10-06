"""add-note 与 notes 命令的回归测试。

通过真实命令行入口（python main.py）核对“追加内部备注、按工单读取备注”这条
流程的退出码、标准输出与标准错误，并覆盖编号校验、内容校验与用法错误的边界。
各次调用分属独立进程，共用同一个通过 --db 指定的临时数据库。
每个用例使用独立临时目录，测试结束后清理，不读取或改动工作目录下的默认库。

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

# 固定样例：空库起步创建两条合成工单，仅将第二条结案
TICKET_A = {"title": "登录失败", "description": "重置密码后仍无法登录"}
TICKET_B = {"title": "打印异常", "description": "更换网络后无法打印"}

# 第一条工单上连续两次追加的原文：首尾空格必须原样保留，重复内容不合并
DUPLICATE_TEXT = " 已复现 "

# 第二条（已结案）工单上追加的备注：包含中文、换行与两种引号，必须逐字保存
CLOSED_TEXT = "结案后补充：\n他说\"先这样\"，\n她说'再试试'"

NOTE_KEYS = {"note_id", "ticket_id", "text"}


class NotesWorkflowTestCase(unittest.TestCase):
    """空库起步：两条工单，仅第二条结案，覆盖备注的正常追加与读取流程。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.id_a = self.create_ticket(TICKET_A["title"], TICKET_A["description"])
        self.id_b = self.create_ticket(TICKET_B["title"], TICKET_B["description"])
        # 仅将第二条结案
        self.assertEqual(self.close_ticket(self.id_b)["status"], "closed")

        # 工单在整个备注流程中应保持的原值（编号、标题、描述、状态）
        self.expected_tickets = [
            [self.id_a, TICKET_A["title"], TICKET_A["description"], "open"],
            [self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"],
        ]

    # ---------- 辅助方法 ----------

    def run_cli(self, *args):
        """以真实命令行入口运行 main.py，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def create_ticket(self, title, description):
        result = self.run_cli(
            "create", "--title", title, "--description", description
        )
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json_value(result.stdout)["id"]

    def close_ticket(self, ticket_id):
        result = self.run_cli("close", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json_value(result.stdout)

    def add_note(self, ticket_id, text):
        """执行 add-note，要求成功，返回 (CompletedProcess, 解析后的备注对象)。"""
        result = self.run_cli("add-note", str(ticket_id), "--text", text)
        self.assertEqual(result.returncode, 0, f"add-note 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        note = self.parse_single_json_value(result.stdout)
        return result, note

    def read_notes(self, ticket_id):
        """在一次全新的进程调用中执行 notes，返回 (CompletedProcess, 备注数组)。"""
        result = self.run_cli("notes", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"notes 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        notes = self.parse_single_json_value(result.stdout)
        self.assertIsInstance(notes, list)
        return result, notes

    @staticmethod
    def parse_single_json_value(stdout):
        """标准输出必须恰好是一个 JSON 值（对象或数组），不夹带说明文字。"""
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        if stripped[end:].strip() != "":
            raise AssertionError(f"标准输出不是唯一 JSON 值: {stdout!r}")
        return value

    def assertNoteShape(self, note):
        """仅含 note_id/ticket_id/text；前两项为整数，text 为字符串。"""
        self.assertIsInstance(note, dict)
        self.assertEqual(set(note.keys()), NOTE_KEYS)
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(note["note_id"]), int)
        self.assertIs(type(note["ticket_id"]), int)
        self.assertIs(type(note["text"]), str)

    def ticket_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def note_rows(self):
        """按工单、备注编号排序返回全部备注明细，用于核对落库内容。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT ticket_id, note_id, text FROM ticket_notes "
                "ORDER BY ticket_id, note_id"
            ).fetchall()
        finally:
            conn.close()

    def note_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM ticket_notes").fetchone()[0]
        finally:
            conn.close()

    def assertTicketsUnchanged(self):
        """两条工单的编号、标题、描述、状态保持原值。"""
        self.assertEqual(
            [list(row) for row in self.ticket_rows()], self.expected_tickets
        )

    # ---------- 最初无备注 ----------

    def test_notes_initially_returns_empty_list_for_both_tickets(self):
        for ticket_id in (self.id_a, self.id_b):
            with self.subTest(ticket_id=ticket_id):
                result, notes = self.read_notes(ticket_id)
                self.assertEqual(notes, [])
                # 标准输出就是空数组本身，不附加说明文字
                self.assertEqual(result.stdout.strip(), "[]")

        # 只读操作不落任何备注，工单也保持原值
        self.assertEqual(self.note_count(), 0)
        self.assertTicketsUnchanged()

    # ---------- 第一条工单连续追加 ----------

    def test_add_note_twice_preserves_spaces_and_keeps_duplicates(self):
        result_1, note_1 = self.add_note(self.id_a, DUPLICATE_TEXT)
        result_2, note_2 = self.add_note(self.id_a, DUPLICATE_TEXT)

        # note_id 在该工单内依次为 1、2；ticket_id 为第一条的实际编号
        self.assertNoteShape(note_1)
        self.assertEqual(
            note_1, {"note_id": 1, "ticket_id": self.id_a, "text": DUPLICATE_TEXT}
        )
        self.assertNoteShape(note_2)
        self.assertEqual(
            note_2, {"note_id": 2, "ticket_id": self.id_a, "text": DUPLICATE_TEXT}
        )
        # 重复内容不合并：两次返回各自独立的对象
        self.assertNotEqual(note_1["note_id"], note_2["note_id"])
        # 首尾空格在标准输出的 JSON 中按原文保留（解析后已比较，这里再核对落库）
        self.assertTrue(note_1["text"].startswith(" "))
        self.assertTrue(note_1["text"].endswith(" "))
        # 中文正文直接可读，不做 \\u 转义
        self.assertIn("已复现", result_1.stdout)
        self.assertIn("已复现", result_2.stdout)

        # 另一次独立进程调用读取：按 note_id 升序返回完整数组
        _, notes = self.read_notes(self.id_a)
        self.assertEqual(
            notes,
            [
                {"note_id": 1, "ticket_id": self.id_a, "text": DUPLICATE_TEXT},
                {"note_id": 2, "ticket_id": self.id_a, "text": DUPLICATE_TEXT},
            ],
        )
        self.assertEqual(
            [list(row) for row in self.note_rows()],
            [[self.id_a, 1, DUPLICATE_TEXT], [self.id_a, 2, DUPLICATE_TEXT]],
        )
        self.assertTicketsUnchanged()

    # ---------- 已结案工单追加 ----------

    def test_add_note_to_closed_ticket_uses_independent_sequence_and_exact_text(self):
        result, note = self.add_note(self.id_b, CLOSED_TEXT)

        # 备注编号在每张工单内独立从 1 开始，与第一条工单无关
        self.assertNoteShape(note)
        self.assertEqual(
            note, {"note_id": 1, "ticket_id": self.id_b, "text": CLOSED_TEXT}
        )
        # 中文、换行、引号逐字保留
        self.assertIn("\n", note["text"])
        self.assertIn('"', note["text"])
        self.assertIn("'", note["text"])
        self.assertIn("结案后补充", result.stdout)

        # 第二条的备注不混入第一条；第一条仍为空
        _, notes_b = self.read_notes(self.id_b)
        self.assertEqual(
            notes_b,
            [{"note_id": 1, "ticket_id": self.id_b, "text": CLOSED_TEXT}],
        )
        _, notes_a = self.read_notes(self.id_a)
        self.assertEqual(notes_a, [])

        self.assertEqual(
            [list(row) for row in self.note_rows()],
            [[self.id_b, 1, CLOSED_TEXT]],
        )
        # 结案状态及工单其他字段不受备注影响
        self.assertTicketsUnchanged()

    # ---------- 两条工单各自备注互不混入 ----------

    def test_notes_returns_full_sorted_array_without_cross_ticket_mixing(self):
        self.add_note(self.id_a, DUPLICATE_TEXT)
        self.add_note(self.id_a, DUPLICATE_TEXT)
        self.add_note(self.id_b, CLOSED_TEXT)

        _, notes_a = self.read_notes(self.id_a)
        _, notes_b = self.read_notes(self.id_b)

        # 每张工单只返回自己的备注，且按 note_id 升序
        self.assertEqual([note["note_id"] for note in notes_a], [1, 2])
        self.assertTrue(
            all(note["ticket_id"] == self.id_a for note in notes_a)
        )
        self.assertEqual([note["note_id"] for note in notes_b], [1])
        self.assertTrue(
            all(note["ticket_id"] == self.id_b for note in notes_b)
        )
        self.assertEqual(
            notes_a,
            [
                {"note_id": 1, "ticket_id": self.id_a, "text": DUPLICATE_TEXT},
                {"note_id": 2, "ticket_id": self.id_a, "text": DUPLICATE_TEXT},
            ],
        )
        self.assertEqual(
            notes_b,
            [{"note_id": 1, "ticket_id": self.id_b, "text": CLOSED_TEXT}],
        )
        self.assertEqual(self.note_count(), 3)

    # ---------- 重复读取不改动任何数据 ----------

    def test_repeated_notes_reads_are_stable_and_leave_records_untouched(self):
        self.add_note(self.id_a, DUPLICATE_TEXT)
        self.add_note(self.id_a, DUPLICATE_TEXT)
        self.add_note(self.id_b, CLOSED_TEXT)

        tickets_before = [list(row) for row in self.ticket_rows()]
        notes_before = [list(row) for row in self.note_rows()]

        # 连续独立进程重复读取，结果完全相同，不增加或修改工单与备注
        seen_a = None
        seen_b = None
        for _ in range(3):
            _, notes_a = self.read_notes(self.id_a)
            _, notes_b = self.read_notes(self.id_b)
            if seen_a is None:
                seen_a, seen_b = notes_a, notes_b
            else:
                self.assertEqual(notes_a, seen_a)
                self.assertEqual(notes_b, seen_b)

        self.assertEqual([list(row) for row in self.ticket_rows()], tickets_before)
        self.assertEqual([list(row) for row in self.note_rows()], notes_before)
        self.assertEqual(self.note_count(), 3)
        # 工单标题、描述、状态保持原值
        self.assertTicketsUnchanged()


class NotesErrorTestCase(unittest.TestCase):
    """已有两条工单、三条备注的样例库上的确定失败结果。

    第一条工单 open（两条重复备注），第二条 closed（一条含换行引号的备注）。
    每个失败调用前后，工单与备注的数量和内容必须完全一致。
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.id_a = self.create_ticket(TICKET_A["title"], TICKET_A["description"])
        self.id_b = self.create_ticket(TICKET_B["title"], TICKET_B["description"])
        self.assertEqual(self.close_ticket(self.id_b)["status"], "closed")

        self.run_cli("add-note", str(self.id_a), "--text", DUPLICATE_TEXT)
        self.run_cli("add-note", str(self.id_a), "--text", DUPLICATE_TEXT)
        self.run_cli("add-note", str(self.id_b), "--text", CLOSED_TEXT)

        # 失败前后应保持不变的存量快照
        self.expected_tickets = [
            [self.id_a, TICKET_A["title"], TICKET_A["description"], "open"],
            [self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"],
        ]
        self.expected_notes = [
            [self.id_a, 1, DUPLICATE_TEXT],
            [self.id_a, 2, DUPLICATE_TEXT],
            [self.id_b, 1, CLOSED_TEXT],
        ]

    # ---------- 辅助方法 ----------

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def create_ticket(self, title, description):
        result = self.run_cli(
            "create", "--title", title, "--description", description
        )
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)["id"]

    def close_ticket(self, ticket_id):
        result = self.run_cli("close", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def ticket_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def note_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT ticket_id, note_id, text FROM ticket_notes "
                "ORDER BY ticket_id, note_id"
            ).fetchall()
        finally:
            conn.close()

    def assert_storage_unchanged(self):
        """失败调用不改变工单与备注的数量和内容。"""
        self.assertEqual(
            [list(row) for row in self.ticket_rows()], self.expected_tickets
        )
        self.assertEqual(
            [list(row) for row in self.note_rows()], self.expected_notes
        )

    def assert_domain_error(self, argv, message):
        """退出码 1：标准输出为空，标准错误仅为提示语加换行，存量不变。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, message + "\n")
        self.assert_storage_unchanged()

    def assert_usage_error(self, argv):
        """退出码 2：标准输出为空，标准错误包含用法提示，存量不变。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assert_storage_unchanged()

    # ---------- 编号格式错误 ----------

    def test_both_commands_reject_non_positive_integer_ids(self):
        for bad_id in ("abc", "0", "-1"):
            with self.subTest(command="add-note", bad_id=bad_id):
                self.assert_domain_error(
                    ("add-note", bad_id, "--text", "任意内容"),
                    "工单编号必须为正整数",
                )
            with self.subTest(command="notes", bad_id=bad_id):
                self.assert_domain_error(
                    ("notes", bad_id), "工单编号必须为正整数"
                )

    # ---------- 编号不存在 ----------

    def test_both_commands_report_missing_ticket(self):
        # 999 为合法正整数但样例库中不存在
        self.assert_domain_error(
            ("add-note", "999", "--text", "任意内容"), "工单不存在"
        )
        self.assert_domain_error(("notes", "999"), "工单不存在")

    # ---------- 备注内容为空 ----------

    def test_add_note_rejects_empty_or_blank_text(self):
        for text in ("", "   ", "\t\n  \t"):
            with self.subTest(text=text):
                # 工单存在但内容为空字符串或纯空白：退出码与输出规则同其他域错误
                self.assert_domain_error(
                    ("add-note", str(self.id_a), "--text", text), "备注不能为空"
                )
            with self.subTest(text=text, ticket="closed"):
                # 已结案工单上同样拒绝，且不写入备注
                self.assert_domain_error(
                    ("add-note", str(self.id_b), "--text", text), "备注不能为空"
                )

    # ---------- 参数用法错误 ----------

    def test_usage_errors_exit_with_code_2(self):
        usage_cases = [
            # 缺少工单编号
            ("notes",),
            ("add-note",),
            ("add-note", "--text", "任意内容"),
            # add-note 缺少 --text 或其值
            ("add-note", str(self.id_a)),
            ("add-note", str(self.id_a), "--text"),
            # 任一备注命令携带未知选项
            ("notes", str(self.id_a), "--unknown"),
            ("add-note", str(self.id_a), "--text", "任意内容", "--unknown"),
        ]
        for argv in usage_cases:
            with self.subTest(argv=argv):
                self.assert_usage_error(argv)


if __name__ == "__main__":
    unittest.main()
