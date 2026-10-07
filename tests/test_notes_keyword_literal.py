"""notes --keyword 特殊字符字面子串匹配的回归测试。

通过真实命令行入口（python main.py）核对 notes 的关键字筛选对引号、
反斜杠、内部空白与换行的连续字面子串匹配语义。固定样例为两条合成工单
（编号 1 保持 open、编号 2 结案），工单 1 依次追加四条备注，按追加顺序
用 JSON 数组表示为：

    ["  引号\\"A\\" 路径x\\\\y  ",
     "引号A 路径x/y",
     "等待  确认\\n客户补充",
     "  引号\\"A\\" 路径x\\\\y  "]

工单 2 只有与工单 1 第一条相同的一条备注。覆盖：

- 含引号的关键字 "\\"A\\"" 与含反斜杠的关键字 "x\\\\y" 均只命中序号 1、4；
- 含内部连续空白的关键字 "等待  确认" 与含真实换行的关键字 "确认\\n客户"
  均只命中序号 3（内部空白不折叠，\\n 表示真实换行）；
- 折叠空白的 "等待 确认"、去掉换行的 "确认客户" 与跨备注拼接的 "y等待"
  均返回 []（不跨备注拼接命中）；
- 重复正文分别保留，不混入另一张工单；用 "\\"A\\"" 查询工单 2 只返回
  它自己的序号 1；
- 有效关键字首尾加空白后结果相同；
- 空或纯空白关键字退出 1（标准错误仅为“关键字不能为空”加换行），
  --keyword 缺取值退出 2（标准错误含用法提示）；
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

# 固定样例：编号 1 保持 open，编号 2 结案
TICKET_A = {"title": "引号路径问题", "description": "客户反馈路径解析失败"}
TICKET_B = {"title": "备份异常", "description": "例行备份未生成"}

# 工单 1 的四条备注：第一条首尾各两个空格、含引号与单反斜杠，
# 第三条含两个连续内部空格与真实换行，第四条与第一条原文相同
NOTE_FIRST = '  引号"A" 路径x\\y  '
NOTE_SECOND = "引号A 路径x/y"
NOTE_THIRD = "等待  确认\n客户补充"
# 工单 2（已结案）追加与工单 1 第一条相同的一条备注
NOTE_B = NOTE_FIRST

# 关键字（均按 JSON 字符串表示理解，\n 为真实换行）
KW_QUOTED = '"A"'
KW_BACKSLASH = "x\\y"
KW_DOUBLE_SPACE = "等待  确认"
KW_NEWLINE = "确认\n客户"

NOTE_KEYS = {"note_id", "ticket_id", "text"}


class NotesKeywordLiteralTestCase(unittest.TestCase):
    """notes --keyword 对引号、反斜杠、内部空白与换行的字面匹配；每用例独立临时库。"""

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

        # 工单 1 依次追加四条备注（第四条重复第一条原文）
        for text in (NOTE_FIRST, NOTE_SECOND, NOTE_THIRD, NOTE_FIRST):
            self._add_note(self.id_a, text)
        # 工单 2（已结案）追加与工单 1 第一条相同的一条备注
        self._add_note(self.id_b, NOTE_B)

        self.expected_notes_a = [
            {"note_id": 1, "ticket_id": self.id_a, "text": NOTE_FIRST},
            {"note_id": 2, "ticket_id": self.id_a, "text": NOTE_SECOND},
            {"note_id": 3, "ticket_id": self.id_a, "text": NOTE_THIRD},
            {"note_id": 4, "ticket_id": self.id_a, "text": NOTE_FIRST},
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
            [self.id_a, 3, NOTE_THIRD],
            [self.id_a, 4, NOTE_FIRST],
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

    # ---------- 含引号的关键字：命中序号 1、4 ----------

    def test_quoted_keyword_matches_notes_1_and_4(self):
        notes = self.query_notes(self.id_a, KW_QUOTED)
        # 只返回工单 1 的序号 1、4，按序号升序
        self.assertEqual([n["note_id"] for n in notes], [1, 4])
        # 重复正文分别保留完整原文与原编号，不混入工单 2 的备注
        self.assertNoteObject(notes[0], 1, self.id_a, NOTE_FIRST)
        self.assertNoteObject(notes[1], 4, self.id_a, NOTE_FIRST)
        self.assertTrue(all(n["ticket_id"] == self.id_a for n in notes))
        self.assertEqual(
            notes, [self.expected_notes_a[0], self.expected_notes_a[3]]
        )
        self.assert_storage_unchanged()

    # ---------- 含反斜杠的关键字：命中序号 1、4 ----------

    def test_backslash_keyword_matches_notes_1_and_4(self):
        notes = self.query_notes(self.id_a, KW_BACKSLASH)
        # 反斜杠按字面字符处理：只命中含 x\y 的序号 1、4，不命中 x/y
        self.assertEqual([n["note_id"] for n in notes], [1, 4])
        self.assertEqual(
            notes, [self.expected_notes_a[0], self.expected_notes_a[3]]
        )
        self.assert_storage_unchanged()

    # ---------- 含内部连续空白的关键字：命中序号 3 ----------

    def test_double_space_keyword_matches_note_3(self):
        notes = self.query_notes(self.id_a, KW_DOUBLE_SPACE)
        self.assertEqual([n["note_id"] for n in notes], [3])
        self.assertNoteObject(notes[0], 3, self.id_a, NOTE_THIRD)
        self.assertEqual(notes, [self.expected_notes_a[2]])
        self.assert_storage_unchanged()

    # ---------- 含真实换行的关键字：命中序号 3 ----------

    def test_newline_keyword_matches_note_3(self):
        notes = self.query_notes(self.id_a, KW_NEWLINE)
        self.assertEqual([n["note_id"] for n in notes], [3])
        self.assertNoteObject(notes[0], 3, self.id_a, NOTE_THIRD)
        self.assertEqual(notes, [self.expected_notes_a[2]])
        self.assert_storage_unchanged()

    # ---------- 内部空白不折叠、不跨备注拼接：均返回 [] ----------

    def test_collapsed_or_joined_keywords_return_empty_array(self):
        for keyword in ("等待 确认", "确认客户", "y等待"):
            with self.subTest(keyword=keyword):
                result = self.run_cli(
                    "notes", str(self.id_a), "--keyword", keyword
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.parse_single_json(result.stdout), [])
        self.assert_storage_unchanged()

    # ---------- 已结案工单：只返回自己的序号 1 ----------

    def test_closed_ticket_quoted_keyword_matches_only_its_own_note(self):
        notes = self.query_notes(self.id_b, KW_QUOTED)
        self.assertEqual([n["note_id"] for n in notes], [1])
        self.assertNoteObject(notes[0], 1, self.id_b, NOTE_B)
        self.assertEqual(notes, self.expected_notes_b)
        # 反斜杠关键字同样只命中它自己的序号 1
        self.assertEqual(
            self.query_notes(self.id_b, KW_BACKSLASH), self.expected_notes_b
        )
        # 已结案工单上不命中的关键字返回 []
        self.assertEqual(self.query_notes(self.id_b, KW_NEWLINE), [])
        self.assert_storage_unchanged()

    # ---------- 有效关键字首尾加空白后结果相同 ----------

    def test_surrounding_whitespace_does_not_change_result(self):
        for keyword in (KW_QUOTED, KW_BACKSLASH, KW_DOUBLE_SPACE, KW_NEWLINE):
            with self.subTest(keyword=keyword):
                bare = self.query_notes(self.id_a, keyword)
                padded = self.query_notes(self.id_a, "  " + keyword + "\t ")
                self.assertEqual(padded, bare)
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
        first = self.query_notes(self.id_a, KW_QUOTED)
        # 空结果查询
        self.assertEqual(self.query_notes(self.id_a, "y等待"), [])
        # 失败查询（空关键字）
        failed = self.run_cli("notes", str(self.id_a), "--keyword", " ")
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, "")
        self.assertEqual(failed.stderr, "关键字不能为空\n")

        # 重复查询结果一致
        for _ in range(2):
            self.assertEqual(self.query_notes(self.id_a, KW_QUOTED), first)

        # 成功、空结果与失败查询前后，工单、备注与状态历史均不变
        self.assert_storage_unchanged()


if __name__ == "__main__":
    unittest.main()
