"""notes --keyword 命令的回归测试。

通过真实命令行入口（python main.py）核对备注关键字筛选语义：
固定样例从空库创建两张合成工单，第一张“登录失败 / 重置密码无效”保持 open，
第二张结案为 closed；第一张依次追加三条备注（第三条与第一条原文完全相同），
第二张追加一条与第一条相同的备注：

    备注 1/3：" 等待确认 Login 100% a_b "
    备注 2  ："已复现 login 100x axb"

覆盖区分大小写的字面子串匹配（内部空白、%、_ 均按原字符比较）、
按 note_id 升序保留完整原文与原编号、不跨工单混入、标题/描述不参与匹配、
空库与已结案工单筛选、不传关键字返回全部，以及空关键字、编号错误、
工单不存在与用法错误四类确定的失败结果，并核对查询前后工单、备注与
状态历史的内容和数量不变、重复查询结果一致。

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

# 固定样例：两张合成工单，仅第二张结案
TICKET_A = {"title": "登录失败", "description": "重置密码无效"}
TICKET_B = {"title": "打印异常", "description": "更换网络后无法打印"}

# 第一张工单的第 1、3 条与第二张工单唯一一条备注的原文：
# 首尾空白必须保留，%、_ 为普通字符
SHARED_NOTE_TEXT = " 等待确认 Login 100% a_b "
# 第一张工单的第 2 条备注：login 为小写，100x/axb 不含 % 与 _
MIDDLE_NOTE_TEXT = "已复现 login 100x axb"

NOTE_KEYS = {"note_id", "ticket_id", "text"}


class NotesKeywordTestCase(unittest.TestCase):
    """带完整备注样例的 notes --keyword 筛选；每个用例使用独立临时数据库。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        # 从空库创建两张固定样例工单
        self.id_a = self._create_ticket(
            TICKET_A["title"], TICKET_A["description"]
        )
        self.id_b = self._create_ticket(
            TICKET_B["title"], TICKET_B["description"]
        )
        # 仅将第二张结案
        result = self.run_cli("close", str(self.id_b))
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")

        # 第一张依次追加三条备注，第三条重复第一条原文
        for text in (SHARED_NOTE_TEXT, MIDDLE_NOTE_TEXT, SHARED_NOTE_TEXT):
            self.add_note(self.id_a, text)
        # 第二张追加一条与第一条相同的备注
        self.add_note(self.id_b, SHARED_NOTE_TEXT)

        self.expected_ticket_rows = [
            [self.id_a, TICKET_A["title"], TICKET_A["description"], "open"],
            [self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"],
        ]
        self.expected_note_rows = [
            [self.id_a, 1, SHARED_NOTE_TEXT],
            [self.id_a, 2, MIDDLE_NOTE_TEXT],
            [self.id_a, 3, SHARED_NOTE_TEXT],
            [self.id_b, 1, SHARED_NOTE_TEXT],
        ]
        # 仅第二张工单有一次 open → closed 的状态变更
        self.expected_history_rows = [
            [self.id_b, 1, "open", "closed"],
        ]
        self.assert_storage_unchanged()

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
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        note = self.parse_single_json(result.stdout)
        self.assertIsInstance(note, dict)
        return note

    def query_notes(self, ticket_id, keyword=...):
        """执行 notes（可带 --keyword），要求成功并返回解析后的 JSON 数组。"""
        argv = ["notes", str(ticket_id)]
        if keyword is not ...:
            argv.extend(["--keyword", keyword])
        result = self.run_cli(*argv)
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

    def history_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT ticket_id, event_id, from_status, to_status "
                "FROM ticket_history ORDER BY ticket_id, event_id"
            ).fetchall()
        finally:
            conn.close()

    def assert_storage_unchanged(self):
        """工单、备注与状态历史的内容和数量必须与样例完全一致。"""
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            self.expected_ticket_rows,
        )
        self.assertEqual(
            [list(row) for row in self.note_rows()],
            self.expected_note_rows,
        )
        self.assertEqual(
            [list(row) for row in self.history_rows()],
            self.expected_history_rows,
        )

    def assert_business_error(self, argv, message):
        """退出码 1、标准输出为空、标准错误仅为提示语及换行。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 1, f"argv={argv!r}")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, message + "\n")

    # ---------- 验收主流程：首尾带空白的关键字只命中 1、3 ----------

    def test_keyword_returns_matching_notes_ascending_with_full_text(self):
        notes = self.query_notes(self.id_a, " 等待确认 ")
        # 只返回序号 1、3，按序号升序，保留完整原文与原编号
        self.assertEqual([n["note_id"] for n in notes], [1, 3])
        self.assertNoteObject(notes[0], 1, self.id_a, SHARED_NOTE_TEXT)
        self.assertNoteObject(notes[1], 3, self.id_a, SHARED_NOTE_TEXT)
        # 不混入第二张工单的备注
        self.assertTrue(all(n["ticket_id"] == self.id_a for n in notes))

    # ---------- 区分大小写 ----------

    def test_keyword_is_case_sensitive(self):
        # 大写 Login 仅出现在第 1、3 条
        upper = self.query_notes(self.id_a, "Login")
        self.assertEqual([n["note_id"] for n in upper], [1, 3])
        self.assertTrue(all(n["ticket_id"] == self.id_a for n in upper))
        # 小写 login 仅出现在第 2 条
        lower = self.query_notes(self.id_a, "login")
        self.assertEqual(len(lower), 1)
        self.assertNoteObject(lower[0], 2, self.id_a, MIDDLE_NOTE_TEXT)

    # ---------- % 与 _ 为普通字符，不是通配符 ----------

    def test_percent_and_underscore_match_literally(self):
        # "100%" 只在第 1、3 条；若 % 被当作通配符则会误中第 2 条 "100x"
        percent = self.query_notes(self.id_a, "%")
        self.assertEqual([n["note_id"] for n in percent], [1, 3])
        # "a_b" 只在第 1、3 条；若 _ 被当作通配符则会误中第 2 条 "axb"
        underscore = self.query_notes(self.id_a, "_")
        self.assertEqual([n["note_id"] for n in underscore], [1, 3])
        for notes in (percent, underscore):
            self.assertTrue(all(n["ticket_id"] == self.id_a for n in notes))
            self.assertTrue(
                all(n["text"] == SHARED_NOTE_TEXT for n in notes)
            )

    # ---------- 只搜备注正文：标题与描述不参与 ----------

    def test_keyword_does_not_search_title_or_description(self):
        # “登录”只出现在第一张工单标题，备注正文均不含，返回空数组
        self.assertEqual(self.query_notes(self.id_a, "登录"), [])
        # “重置”只出现在第一张工单描述，同样不命中
        self.assertEqual(self.query_notes(self.id_a, "重置"), [])

    # ---------- 已结案工单仍可正常筛选 ----------

    def test_keyword_works_on_closed_ticket(self):
        notes = self.query_notes(self.id_b, " 等待确认 Login")
        self.assertEqual(len(notes), 1)
        self.assertNoteObject(notes[0], 1, self.id_b, SHARED_NOTE_TEXT)
        # 第一张工单的备注不混入第二张
        self.assertTrue(all(n["ticket_id"] == self.id_b for n in notes))
        # 第二张工单上不存在的字面子串同样成功返回 []
        self.assertEqual(self.query_notes(self.id_b, "login"), [])

    # ---------- 不传关键字继续返回全部备注 ----------

    def test_notes_without_keyword_returns_all_notes(self):
        notes_a = self.query_notes(self.id_a)
        self.assertEqual([n["note_id"] for n in notes_a], [1, 2, 3])
        self.assertNoteObject(notes_a[0], 1, self.id_a, SHARED_NOTE_TEXT)
        self.assertNoteObject(notes_a[1], 2, self.id_a, MIDDLE_NOTE_TEXT)
        self.assertNoteObject(notes_a[2], 3, self.id_a, SHARED_NOTE_TEXT)

        notes_b = self.query_notes(self.id_b)
        self.assertEqual(len(notes_b), 1)
        self.assertNoteObject(notes_b[0], 1, self.id_b, SHARED_NOTE_TEXT)

    # ---------- 显式空字符串或纯空白关键字 ----------

    def test_empty_or_blank_keyword_rejected_for_existing_ticket(self):
        for keyword in ("", "   ", "\t\n  \t"):
            for ticket_id in (self.id_a, self.id_b):
                with self.subTest(keyword=keyword, ticket_id=ticket_id):
                    result = self.run_cli(
                        "notes", str(ticket_id), "--keyword", keyword
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "关键字不能为空\n")
        self.assert_storage_unchanged()

    # ---------- 编号校验先于关键字校验 ----------

    def test_bad_id_with_empty_keyword_reports_id_error(self):
        for bad_id in ("abc", "0"):
            for keyword in ("", "   "):
                with self.subTest(bad_id=bad_id, keyword=keyword):
                    self.assert_business_error(
                        ("notes", bad_id, "--keyword", keyword),
                        "工单编号必须为正整数",
                    )
        self.assert_storage_unchanged()

    def test_missing_ticket_with_empty_keyword_reports_not_found(self):
        for keyword in ("", "   "):
            with self.subTest(keyword=keyword):
                self.assert_business_error(
                    ("notes", "999", "--keyword", keyword),
                    "工单不存在",
                )
        self.assert_storage_unchanged()

    # ---------- --keyword 缺少取值：用法错误 ----------

    def test_keyword_without_value_is_usage_error(self):
        result = self.run_cli("notes", str(self.id_a), "--keyword")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assert_storage_unchanged()

    # ---------- 只读与可重复性 ----------

    def test_queries_are_read_only_and_repeatable(self):
        # 成功命中、成功空结果、业务失败与用法失败各执行一遍
        success_cases = [
            (self.id_a, " 等待确认 "),
            (self.id_a, "Login"),
            (self.id_a, "login"),
            (self.id_a, "%"),
            (self.id_a, "_"),
            (self.id_a, "登录"),
            (self.id_b, "等待确认"),
        ]
        first_results = {}
        for ticket_id, keyword in success_cases:
            result = self.run_cli(
                "notes", str(ticket_id), "--keyword", keyword
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "")
            first_results[(ticket_id, keyword)] = result.stdout

        # 失败查询不影响存量数据
        failure_argv = [
            ("notes", str(self.id_a), "--keyword", ""),
            ("notes", "abc", "--keyword", ""),
            ("notes", "0", "--keyword", " "),
            ("notes", "999", "--keyword", ""),
            ("notes", str(self.id_a), "--keyword"),
        ]
        for argv in failure_argv:
            self.run_cli(*argv)

        # 工单、备注与状态历史的内容和数量均不变
        self.assert_storage_unchanged()

        # 重复查询结果逐字节一致（输出本身即确定的 JSON）
        for ticket_id, keyword in success_cases:
            result = self.run_cli(
                "notes", str(ticket_id), "--keyword", keyword
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                result.stdout, first_results[(ticket_id, keyword)]
            )


class NotesKeywordWithoutNotesTestCase(unittest.TestCase):
    """两张工单均未追加备注时的关键字查询；每个用例使用独立临时数据库。"""

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

        self.expected_ticket_rows = [
            [self.id_a, TICKET_A["title"], TICKET_A["description"], "open"],
            [self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"],
        ]
        self.expected_history_rows = [
            [self.id_b, 1, "open", "closed"],
        ]

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

    def test_keyword_query_without_notes_returns_empty_array(self):
        # open 与已结案工单在无备注时均成功返回 []，关键字不会报错或建占位数据
        for ticket_id in (self.id_a, self.id_b):
            with self.subTest(ticket_id=ticket_id):
                result = self.run_cli(
                    "notes", str(ticket_id), "--keyword", "等待确认"
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(
                    NotesKeywordTestCase.parse_single_json(result.stdout), []
                )
                # 不传关键字同样为 []
                result_all = self.run_cli("notes", str(ticket_id))
                self.assertEqual(result_all.returncode, 0)
                self.assertEqual(result_all.stderr, "")
                self.assertEqual(
                    NotesKeywordTestCase.parse_single_json(result_all.stdout),
                    [],
                )

        # 没有写入任何备注；工单与结案历史保持准备时的内容和数量
        conn = sqlite3.connect(self.db_path)
        try:
            note_count = conn.execute(
                "SELECT COUNT(*) FROM ticket_notes"
            ).fetchone()[0]
            ticket_rows = conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
            history_rows = conn.execute(
                "SELECT ticket_id, event_id, from_status, to_status "
                "FROM ticket_history ORDER BY ticket_id, event_id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(note_count, 0)
        self.assertEqual(
            [list(row) for row in ticket_rows], self.expected_ticket_rows
        )
        self.assertEqual(
            [list(row) for row in history_rows], self.expected_history_rows
        )


if __name__ == "__main__":
    unittest.main()
