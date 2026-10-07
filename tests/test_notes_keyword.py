"""notes --keyword 关键字筛选的回归测试。

通过真实命令行入口（python main.py）核对 notes 的关键字筛选语义与失败结果：
固定样例为两条合成工单（第一条 open、第二条 closed），第一条依次追加
“ 等待确认 Login 100% a_b ”、“已复现 login 100x axb” 与再次重复第一条内容
共三条备注，第二条追加与第一条相同的一条备注。覆盖：

- 带首尾空白的关键字命中序号 1、3，按序号升序保留完整原文与原编号，
  不混入另一张工单的备注；
- 匹配区分大小写（Login 命中 1、3，login 命中 2），% 与 _ 按字面字符处理；
- 只出现在标题中的词、无备注工单均返回 []；已结案工单仍可正常筛选；
- 不传 --keyword 时返回全部备注；
- 空或纯空白关键字、非法编号、不存在编号与 --keyword 缺取值的确定失败结果；
- 成功、空结果与失败查询均为只读：工单、备注与状态历史内容和数量不变，
  重复查询结果一致。

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

# 固定样例：第一条保持 open，第二条结案
TICKET_A = {"title": "登录失败", "description": "重置密码无效"}
TICKET_B = {"title": "打印异常", "description": "更换网络后无法打印"}

# 第一条工单的三条备注：首尾空白保留，第三条与第一条原文相同
NOTE_FIRST = " 等待确认 Login 100% a_b "
NOTE_SECOND = "已复现 login 100x axb"
# 第二条（已结案）工单追加与第一条相同的一条备注
NOTE_B = NOTE_FIRST

NOTE_KEYS = {"note_id", "ticket_id", "text"}


class NotesKeywordTestCase(unittest.TestCase):
    """notes --keyword 的筛选语义、失败结果与只读性；每用例独立临时库。"""

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
        closed = self.run_cli("close", str(self.id_b))
        self.assertEqual(closed.returncode, 0, f"准备样例失败: {closed.stderr}")
        self.assertEqual(closed.stderr, "")

        # 第一条工单依次追加三条备注（第三条重复第一条原文）
        for text in (NOTE_FIRST, NOTE_SECOND, NOTE_FIRST):
            self._add_note(self.id_a, text)
        # 第二条（已结案）工单追加与第一条相同的一条备注
        self._add_note(self.id_b, NOTE_B)

        self.expected_notes_a = [
            {"note_id": 1, "ticket_id": self.id_a, "text": NOTE_FIRST},
            {"note_id": 2, "ticket_id": self.id_a, "text": NOTE_SECOND},
            {"note_id": 3, "ticket_id": self.id_a, "text": NOTE_FIRST},
        ]
        self.expected_notes_b = [
            {"note_id": 1, "ticket_id": self.id_b, "text": NOTE_B},
        ]
        self.expected_ticket_rows = [
            [self.id_a, TICKET_A["title"], TICKET_A["description"], "open"],
            [self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"],
        ]
        self.expected_note_rows = [
            [self.id_a, 1, NOTE_FIRST],
            [self.id_a, 2, NOTE_SECOND],
            [self.id_a, 3, NOTE_FIRST],
            [self.id_b, 1, NOTE_B],
        ]
        # 第二条结案产生唯一一条状态历史
        self.expected_history_rows = [
            [self.id_b, 1, "open", "closed"],
        ]
        # 确认样例准备正确
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

    def _add_note(self, ticket_id, text):
        result = self.run_cli("add-note", str(ticket_id), "--text", text)
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")

    @staticmethod
    def parse_single_json(stdout):
        """标准输出必须恰好是一个 JSON 值，不允许前后夹带说明文字。"""
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        if stripped[end:].strip() != "":
            raise AssertionError(f"标准输出不是唯一 JSON 值: {stdout!r}")
        return value

    def query_notes(self, ticket_id, keyword):
        """执行 notes --keyword，要求成功并返回解析后的 JSON 数组。"""
        result = self.run_cli("notes", str(ticket_id), "--keyword", keyword)
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
        """查询不新增或修改任何工单、备注与状态历史。"""
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            self.expected_ticket_rows,
        )
        self.assertEqual(
            [list(row) for row in self.note_rows()], self.expected_note_rows
        )
        self.assertEqual(
            [list(row) for row in self.history_rows()],
            self.expected_history_rows,
        )

    # ---------- 带首尾空白的关键字：命中序号 1、3 ----------

    def test_keyword_with_surrounding_spaces_matches_notes_1_and_3(self):
        notes = self.query_notes(self.id_a, " 等待确认 ")
        # 只返回第一张工单的序号 1、3，按序号升序
        self.assertEqual([n["note_id"] for n in notes], [1, 3])
        # 保留完整原文与原编号，字段与类型沿用 notes 的公开格式
        self.assertNoteObject(notes[0], 1, self.id_a, NOTE_FIRST)
        self.assertNoteObject(notes[1], 3, self.id_a, NOTE_FIRST)
        # 不混入第二张工单的备注
        self.assertTrue(all(n["ticket_id"] == self.id_a for n in notes))
        self.assertEqual(notes, [self.expected_notes_a[0], self.expected_notes_a[2]])
        self.assert_storage_unchanged()

    # ---------- 匹配区分大小写 ----------

    def test_keyword_matching_is_case_sensitive(self):
        upper = self.query_notes(self.id_a, "Login")
        self.assertEqual([n["note_id"] for n in upper], [1, 3])
        self.assertEqual(upper, [self.expected_notes_a[0], self.expected_notes_a[2]])

        lower = self.query_notes(self.id_a, "login")
        self.assertEqual([n["note_id"] for n in lower], [2])
        self.assertNoteObject(lower[0], 2, self.id_a, NOTE_SECOND)
        self.assert_storage_unchanged()

    # ---------- % 与 _ 按字面字符处理，不作为通配符 ----------

    def test_percent_and_underscore_are_literal_characters(self):
        for keyword in ("%", "_"):
            with self.subTest(keyword=keyword):
                notes = self.query_notes(self.id_a, keyword)
                # 若被当作 LIKE 通配符会命中全部三条；字面匹配只命中 1、3
                self.assertEqual([n["note_id"] for n in notes], [1, 3])
                self.assertEqual(
                    notes,
                    [self.expected_notes_a[0], self.expected_notes_a[2]],
                )
        self.assert_storage_unchanged()

    # ---------- 只出现在标题中的词不命中备注 ----------

    def test_keyword_only_in_title_returns_empty_array(self):
        # “登录”仅出现在第一张工单标题中，不出现在任何备注正文
        result = self.run_cli("notes", str(self.id_a), "--keyword", "登录")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.parse_single_json(result.stdout), [])
        self.assert_storage_unchanged()

    # ---------- 未追加备注的工单：查询返回 [] ----------

    def test_ticket_without_notes_returns_empty_array(self):
        # 新建一张没有任何备注的工单
        new_id = self._create_ticket("网络抖动", "视频会议偶尔断线")
        result = self.run_cli("notes", str(new_id), "--keyword", "等待确认")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.parse_single_json(result.stdout), [])

    # ---------- 已结案工单仍可正常筛选 ----------

    def test_closed_ticket_remains_filterable(self):
        notes = self.query_notes(self.id_b, " 等待确认 ")
        self.assertEqual([n["note_id"] for n in notes], [1])
        self.assertNoteObject(notes[0], 1, self.id_b, NOTE_B)
        self.assertEqual(notes, self.expected_notes_b)
        # 已结案工单上不命中的关键字同样返回 []
        self.assertEqual(self.query_notes(self.id_b, "login"), [])
        self.assert_storage_unchanged()

    # ---------- 不传关键字：继续返回全部备注 ----------

    def test_without_keyword_returns_all_notes(self):
        for ticket_id, expected in (
            (self.id_a, self.expected_notes_a),
            (self.id_b, self.expected_notes_b),
        ):
            with self.subTest(ticket_id=ticket_id):
                result = self.run_cli("notes", str(ticket_id))
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                notes = self.parse_single_json(result.stdout)
                self.assertEqual(notes, expected)
                self.assertEqual(
                    [n["note_id"] for n in notes],
                    list(range(1, len(expected) + 1)),
                )
        self.assert_storage_unchanged()

    # ---------- 空或纯空白关键字：退出码 1 ----------

    def test_empty_or_blank_keyword_rejected(self):
        for keyword in ("", "   ", "\t \n"):
            with self.subTest(keyword=keyword):
                result = self.run_cli(
                    "notes", str(self.id_a), "--keyword", keyword
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "关键字不能为空\n")
        # 已结案工单同样拒绝
        result = self.run_cli("notes", str(self.id_b), "--keyword", "  ")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "关键字不能为空\n")
        self.assert_storage_unchanged()

    # ---------- 非法编号配空关键字：只报编号错误 ----------

    def test_invalid_id_with_empty_keyword_reports_id_error(self):
        for bad_id in ("abc", "0"):
            with self.subTest(bad_id=bad_id):
                result = self.run_cli(
                    "notes", bad_id, "--keyword", ""
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单编号必须为正整数\n")
        self.assert_storage_unchanged()

    # ---------- 不存在的编号配空关键字：只报工单不存在 ----------

    def test_missing_ticket_with_empty_keyword_reports_not_found(self):
        result = self.run_cli("notes", "999", "--keyword", "")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")
        self.assert_storage_unchanged()

    # ---------- --keyword 缺少取值：用法错误，退出码 2 ----------

    def test_keyword_missing_value_is_usage_error(self):
        result = self.run_cli("notes", str(self.id_a), "--keyword")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assert_storage_unchanged()

    # ---------- 只读性与重复查询一致性 ----------

    def test_queries_are_read_only_and_repeatable(self):
        # 成功查询
        first = self.query_notes(self.id_a, " 等待确认 ")
        # 空结果查询
        self.assertEqual(self.query_notes(self.id_a, "登录"), [])
        # 失败查询（空关键字）
        failed = self.run_cli("notes", str(self.id_a), "--keyword", " ")
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, "")
        self.assertEqual(failed.stderr, "关键字不能为空\n")

        # 重复查询结果一致
        for _ in range(2):
            self.assertEqual(self.query_notes(self.id_a, " 等待确认 "), first)

        # 成功、空结果与失败查询前后，工单、备注与状态历史均不变
        self.assert_storage_unchanged()


if __name__ == "__main__":
    unittest.main()
