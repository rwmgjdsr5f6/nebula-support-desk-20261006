"""add-note 与 notes 命令的回归测试。

通过真实命令行入口（python main.py）核对内部备注的追加与读取流程：
固定样例从空库创建“登录失败”和“打印异常”两条合成工单，仅将第二条结案；
覆盖空备注列表、连续追加（编号在每张工单内独立递增、原文保留、不去重）、
结案工单追加、跨进程按 note_id 升序读取与只读性，以及编号、内容与用法
三类确定的失败结果。

每个用例使用独立临时目录，通过 --db 明确指定临时 SQLite 文件，
测试结束后清理，不读取或改动工作目录下的默认库 tickets.sqlite。
比较一律基于解析后的 JSON 值，不依赖键顺序或排版空格。

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

# 固定样例：两条合成工单，仅第二条结案
TICKET_A = {"title": "登录失败", "description": "重置密码后仍无法登录"}
TICKET_B = {"title": "打印异常", "description": "更换网络后无法打印"}

# 第一条工单上连续两次追加的原文：首尾空白必须保留，重复内容不合并
FIRST_NOTE_TEXT = " 已复现 "
# 第二条（已结案）工单追加的文本：含中文、换行与引号，按原文保存
SECOND_NOTE_TEXT = '客户反馈："打印异常"\n已转交二线跟进'

NOTE_KEYS = {"note_id", "ticket_id", "text"}


class NotesWorkflowTestCase(unittest.TestCase):
    """空库起步的备注追加与读取正常流程；每个用例使用独立临时数据库。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        # 从空库创建两条固定样例工单
        self.id_a = self._create_ticket(
            TICKET_A["title"], TICKET_A["description"]
        )
        self.id_b = self._create_ticket(
            TICKET_B["title"], TICKET_B["description"]
        )
        # 仅将第二条结案
        result = self.run_cli("close", str(self.id_b))
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")

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

    def add_note(self, ticket_id, text):
        """执行 add-note，要求成功并返回解析后的唯一备注 JSON 对象。"""
        result = self.run_cli("add-note", str(ticket_id), "--text", text)
        self.assertEqual(result.returncode, 0, f"add-note 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        note = self.parse_single_json(result.stdout)
        self.assertIsInstance(note, dict)
        return note

    def list_notes(self, ticket_id):
        """执行 notes，要求成功并返回解析后的 JSON 数组。"""
        result = self.run_cli("notes", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"notes 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        notes = self.parse_single_json(result.stdout)
        self.assertIsInstance(notes, list)
        return notes

    def assertNoteObject(self, note, note_id, ticket_id, text):
        """仅含 note_id/ticket_id/text；前两项为整数，text 为字符串，值精确相等。"""
        self.assertIsInstance(note, dict)
        self.assertEqual(set(note.keys()), NOTE_KEYS)
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(note["note_id"]), int)
        self.assertIs(type(note["ticket_id"]), int)
        self.assertIs(type(note["text"]), str)
        self.assertEqual(
            note,
            {"note_id": note_id, "ticket_id": ticket_id, "text": text},
        )

    def note_rows(self):
        """直接读取 SQLite 中全部备注，按工单与备注编号排序。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT ticket_id, note_id, text FROM ticket_notes "
                "ORDER BY ticket_id, note_id"
            ).fetchall()
        finally:
            conn.close()

    def ticket_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def expected_ticket_rows(self):
        return [
            (self.id_a, TICKET_A["title"], TICKET_A["description"], "open"),
            (self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"),
        ]

    # ---------- 最初两条工单都没有备注 ----------

    def test_notes_initially_returns_empty_array_for_both_tickets(self):
        for ticket_id in (self.id_a, self.id_b):
            with self.subTest(ticket_id=ticket_id):
                # 每次都是独立进程调用
                result = self.run_cli("notes", str(ticket_id))
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                # 标准输出只含一个空 JSON 数组，没有额外说明文字
                self.assertEqual(self.parse_single_json(result.stdout), [])

        # 只读：确认没有生成任何备注或占位工单
        self.assertEqual(self.note_rows(), [])
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            [list(row) for row in self.expected_ticket_rows()],
        )

    # ---------- 第一条工单连续两次追加 ----------

    def test_repeated_add_note_sequences_keeps_spaces_and_does_not_merge(self):
        first = self.add_note(self.id_a, FIRST_NOTE_TEXT)
        # note_id 从 1 开始，ticket_id 为第一条工单的实际编号
        self.assertNoteObject(first, 1, self.id_a, FIRST_NOTE_TEXT)
        # 首尾空格按原文保留，中文直接可读
        self.assertTrue(first["text"].startswith(" "))
        self.assertTrue(first["text"].endswith(" "))
        self.assertIn("已复现", json.dumps(first, ensure_ascii=False))

        # 相同原文再次追加：不合并，编号递增为 2
        second = self.add_note(self.id_a, FIRST_NOTE_TEXT)
        self.assertNoteObject(second, 2, self.id_a, FIRST_NOTE_TEXT)

        # 落库两行，内容与返回对象一致
        self.assertEqual(
            [list(row) for row in self.note_rows()],
            [
                [self.id_a, 1, FIRST_NOTE_TEXT],
                [self.id_a, 2, FIRST_NOTE_TEXT],
            ],
        )
        # 工单本身不被追加操作改变
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            [list(row) for row in self.expected_ticket_rows()],
        )

    # ---------- 已结案工单追加：编号独立计数 ----------

    def test_add_note_to_closed_ticket_starts_separate_sequence(self):
        note = self.add_note(self.id_b, SECOND_NOTE_TEXT)
        # 不同工单各自计数：第二条工单的 note_id 独立从 1 开始
        self.assertNoteObject(note, 1, self.id_b, SECOND_NOTE_TEXT)
        # 含中文、换行、引号的内容与输入完全相同
        self.assertIn("\n", note["text"])
        self.assertIn('"', note["text"])

        # 第一条工单仍无备注，结案状态也不因追加而改变
        self.assertEqual(self.list_notes(self.id_a), [])
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            [list(row) for row in self.expected_ticket_rows()],
        )

    # ---------- 跨进程读取、隔离性与只读 ----------

    def test_notes_returns_full_array_sorted_without_cross_ticket_mixing(self):
        # 第一条两条重复原文，第二条一条含换行引号的文本
        added_a = [
            self.add_note(self.id_a, FIRST_NOTE_TEXT),
            self.add_note(self.id_a, FIRST_NOTE_TEXT),
        ]
        added_b = [self.add_note(self.id_b, SECOND_NOTE_TEXT)]

        expected_notes_a = [
            {"note_id": 1, "ticket_id": self.id_a, "text": FIRST_NOTE_TEXT},
            {"note_id": 2, "ticket_id": self.id_a, "text": FIRST_NOTE_TEXT},
        ]
        expected_notes_b = [
            {"note_id": 1, "ticket_id": self.id_b, "text": SECOND_NOTE_TEXT},
        ]
        self.assertEqual(added_a, expected_notes_a)
        self.assertEqual(added_b, expected_notes_b)

        expected_note_rows = [
            [self.id_a, 1, FIRST_NOTE_TEXT],
            [self.id_a, 2, FIRST_NOTE_TEXT],
            [self.id_b, 1, SECOND_NOTE_TEXT],
        ]

        # 另一次独立进程调用 notes：按 note_id 升序返回完整数组
        notes_a = self.list_notes(self.id_a)
        self.assertEqual(notes_a, expected_notes_a)
        self.assertEqual([n["note_id"] for n in notes_a], [1, 2])
        # 第二条工单的备注不混入第一条
        self.assertTrue(all(n["ticket_id"] == self.id_a for n in notes_a))

        notes_b = self.list_notes(self.id_b)
        self.assertEqual(notes_b, expected_notes_b)

        # 重复读取结果相同，且不增加或修改工单与备注
        for _ in range(2):
            self.assertEqual(self.list_notes(self.id_a), expected_notes_a)
            self.assertEqual(self.list_notes(self.id_b), expected_notes_b)
        self.assertEqual(
            [list(row) for row in self.note_rows()], expected_note_rows
        )

        # 工单标题、描述、状态保持原值（一 open、一 closed）
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            [list(row) for row in self.expected_ticket_rows()],
        )


class NotesErrorTestCase(unittest.TestCase):
    """失败结果：编号非法、工单不存在、内容为空与用法错误。

    样例库中已有两条工单（第二条已结案）与三条备注，
    每次失败调用前后工单与备注的数量和内容都必须完全一致。
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.id_a = self._create_ticket(
            TICKET_A["title"], TICKET_A["description"]
        )
        self.id_b = self._create_ticket(
            TICKET_B["title"], TICKET_B["description"]
        )
        closed = self.run_cli("close", str(self.id_b))
        self.assertEqual(closed.returncode, 0, f"准备样例失败: {closed.stderr}")
        self.assertEqual(closed.stderr, "")

        # 预置备注：第一条两条、第二条一条
        self.run_cli("add-note", str(self.id_a), "--text", FIRST_NOTE_TEXT)
        self.run_cli("add-note", str(self.id_a), "--text", FIRST_NOTE_TEXT)
        self.run_cli("add-note", str(self.id_b), "--text", SECOND_NOTE_TEXT)

        self.expected_ticket_rows = [
            [self.id_a, TICKET_A["title"], TICKET_A["description"], "open"],
            [self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"],
        ]
        self.expected_note_rows = [
            [self.id_a, 1, FIRST_NOTE_TEXT],
            [self.id_a, 2, FIRST_NOTE_TEXT],
            [self.id_b, 1, SECOND_NOTE_TEXT],
        ]
        # 确认样例准备正确
        self.assert_storage_unchanged()

    # ---------- 辅助方法 ----------

    def run_cli(self, *args):
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
        """失败调用不新增或修改任何工单与备注。"""
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            self.expected_ticket_rows,
        )
        self.assertEqual(
            [list(row) for row in self.note_rows()], self.expected_note_rows
        )

    def assert_business_error(self, argv, message):
        """退出码 1、标准输出为空、标准错误仅为提示语及换行。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 1, f"argv={argv!r}")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, message + "\n")
        self.assert_storage_unchanged()

    def assert_usage_error(self, argv):
        """退出码 2、标准输出为空、标准错误包含用法提示。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 2, f"argv={argv!r}: {result.stderr}")
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assert_storage_unchanged()

    # ---------- 编号不是正整数 ----------

    def test_non_positive_integer_ids_rejected_for_both_commands(self):
        for command in ("add-note", "notes"):
            for bad_id in ("abc", "0", "-1"):
                with self.subTest(command=command, bad_id=bad_id):
                    if command == "add-note":
                        argv = (command, bad_id, "--text", "任意内容")
                    else:
                        argv = (command, bad_id)
                    self.assert_business_error(argv, "工单编号必须为正整数")

    # ---------- 编号为正整数但工单不存在 ----------

    def test_missing_ticket_reported_for_both_commands(self):
        self.assert_business_error(
            ("notes", "999"), "工单不存在"
        )
        self.assert_business_error(
            ("add-note", "999", "--text", "任意内容"), "工单不存在"
        )

    # ---------- 备注内容为空或纯空白 ----------

    def test_add_note_rejects_empty_or_blank_text(self):
        for text in ("", "   ", "\t\n  \t"):
            with self.subTest(text=text):
                self.assert_business_error(
                    ("add-note", str(self.id_a), "--text", text),
                    "备注不能为空",
                )
        # 对已结案工单同样拒绝，且不改动存量数据
        self.assert_business_error(
            ("add-note", str(self.id_b), "--text", "   "),
            "备注不能为空",
        )

    # ---------- 用法错误：退出码 2 ----------

    def test_usage_errors_exit_with_code_2(self):
        usage_cases = [
            # notes 缺少工单编号
            ("notes",),
            # notes 携带未知选项
            ("notes", str(self.id_a), "--unknown"),
            # add-note 缺少工单编号与 --text
            ("add-note",),
            # add-note 缺少工单编号
            ("add-note", "--text", "任意内容"),
            # add-note 缺少 --text
            ("add-note", str(self.id_a)),
            # --text 缺少取值
            ("add-note", str(self.id_a), "--text"),
            # add-note 携带未知选项
            ("add-note", str(self.id_a), "--text", "任意内容", "--unknown"),
        ]
        for argv in usage_cases:
            with self.subTest(argv=argv):
                self.assert_usage_error(argv)


if __name__ == "__main__":
    unittest.main()
