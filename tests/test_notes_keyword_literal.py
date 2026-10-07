r"""notes --keyword 对引号、反斜杠、内部空白与换行的字面子串匹配回归测试。

通过真实命令行入口（python main.py）核对 notes --keyword 的连续字面
子串语义。固定样例为独立临时库中的两张工单（编号 1 为 open、编号 2 为
closed）。工单 1 的四条备注按追加顺序用 JSON 数组表示为：

    ["  引号\"A\" 路径x\\y  ",
     "引号A 路径x/y",
     "等待  确认\n客户补充",
     "  引号\"A\" 路径x\\y  "]

工单 2 只有一条与第一条相同的备注。备注正文与关键字均直接由 JSON 文本
解析得到，其中 \n 表示真实换行。覆盖：

- 关键字 "\"A\"" 与 "x\\y" 均只命中工单 1 的序号 1、4：引号与反斜杠按
  原字符比较，正斜杠不等同于反斜杠，两条重复正文分别保留；
- 关键字 "等待  确认"（两个内部空格）与 "确认\n客户"（跨行）均只命中
  序号 3；"等待 确认"、"确认客户" 与 "y等待" 均返回 []：内部空白与
  换行不折叠，也不跨备注拼接（"y等待" 恰为备注 2 结尾与备注 3 开头的
  拼接点）；
- 在工单 2 上以 "\"A\"" 查询只返回它自己的序号 1，不混入工单 1 的备注；
- 有效关键字首尾加任意空白后结果相同（只去除首尾空白），内部空白保留；
- 成功结果退出码 0、标准错误为空、标准输出仅为一个 JSON 数组，各项只含
  note_id、ticket_id、text，前两项为整数，正文逐字符保留（含首尾空白、
  引号、反斜杠与换行）；
- 空或纯空白关键字退出 1、标准输出为空、标准错误恰为“关键字不能为空”
  加换行；--keyword 缺少取值退出 2 且标准错误含用法提示；
- 重复查询结果一致；查询前后工单、备注与状态历史均不变。

每个用例使用独立临时目录并通过 --db 明确指定临时 SQLite 文件，测试结束
后清理，不读取或改动工作目录下的默认库 tickets.sqlite，也不遗留样例库。
比较一律基于解析后的 JSON 值，不依赖键顺序或排版空格。

运行方式（项目根目录）：
    python -m unittest discover -s tests
"""

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = PROJECT_ROOT / "main.py"

# 使用者默认库的相对路径（main.py 的 DEFAULT_DB）；本组测试一律用 --db
# 指向临时文件，模块装载与结束时各核对一次，确保默认库既不被改动也不被创建
DEFAULT_DB_NAME = "tickets.sqlite"

# 工单 1 的四条备注：直接解析验收说明中的 JSON 数组文本，保证逐字符一致
NOTES_TICKET1 = json.loads(
    r'["  引号\"A\" 路径x\\y  ","引号A 路径x/y",'
    r'"等待  确认\n客户补充","  引号\"A\" 路径x\\y  "]'
)
# 工单 2 只有与第一条相同的一条备注
NOTE_TICKET2 = NOTES_TICKET1[0]

NOTE_DUP = NOTES_TICKET1[0]        # '  引号"A" 路径x\\y  '
NOTE_SLASH = NOTES_TICKET1[1]      # '引号A 路径x/y'
NOTE_MULTILINE = NOTES_TICKET1[2]  # '等待  确认\n客户补充'

# 验收关键字同样由其 JSON 字符串表示解析得到
KW_QUOTE = json.loads(r'"\"A\""')          # JSON: "\"A\""
KW_BACKSLASH = json.loads(r'"x\\y"')       # JSON: "x\\y"
KW_DOUBLE_SPACE = "等待  确认"              # 两个内部空格
KW_NEWLINE = json.loads(r'"确认\n客户"')    # JSON: "确认\n客户"，含真实换行
KW_SINGLE_SPACE = "等待 确认"               # 仅一个内部空格
KW_NO_NEWLINE = "确认客户"                  # 折叠掉换行后的写法
KW_CROSS_NOTE = "y等待"                     # 备注 2 结尾 + 备注 3 开头的拼接点

# 防止常量在书写时走样：以下关系是全部用例成立的前提
assert KW_QUOTE == '"A"', repr(KW_QUOTE)
assert KW_BACKSLASH == "x\\y", repr(KW_BACKSLASH)
assert KW_NEWLINE == "确认\n客户", repr(KW_NEWLINE)
assert KW_QUOTE in NOTE_DUP and KW_BACKSLASH in NOTE_DUP
assert KW_DOUBLE_SPACE in NOTE_MULTILINE and KW_NEWLINE in NOTE_MULTILINE
assert "\\" not in NOTE_SLASH and "/" in NOTE_SLASH
assert KW_SINGLE_SPACE not in NOTE_MULTILINE
assert KW_NO_NEWLINE not in NOTE_MULTILINE
assert all(KW_CROSS_NOTE not in note for note in NOTES_TICKET1)
assert NOTE_TICKET2 == NOTE_DUP

NOTE_KEYS = {"note_id", "ticket_id", "text"}

# 固定样例工单：标题与描述刻意不包含任何验收关键字
TICKET1 = {"title": "样例工单一", "description": "引号反斜杠核对"}
TICKET2 = {"title": "样例工单二", "description": "空白换行核对"}

_default_db_snapshot = None


def _snapshot_default_db():
    path = Path.cwd() / DEFAULT_DB_NAME
    if not path.exists():
        return (False, None)
    return (True, hashlib.sha256(path.read_bytes()).hexdigest())


def setUpModule():
    """记录使用者默认库的存在状态与内容摘要，tearDownModule 时原样核对。"""
    global _default_db_snapshot
    _default_db_snapshot = _snapshot_default_db()


def tearDownModule():
    """本组测试不得创建或改动默认库 tickets.sqlite。"""
    after = _snapshot_default_db()
    assert after == _default_db_snapshot, (
        f"默认库 {DEFAULT_DB_NAME} 在测试期间被创建或改动: "
        f"before={_default_db_snapshot!r} after={after!r}"
    )


class NotesKeywordLiteralTestCase(unittest.TestCase):
    """引号、反斜杠、内部空白与换行的字面子串匹配；每用例独立临时库。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        # 先登记样例库残留检查，再登记临时目录清理：cleanup 按登记逆序执行，
        # 因此临时目录先被删除，随后断言样例库文件确已不残留
        self.addCleanup(self._assert_sample_db_removed)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        # 从空库创建两张工单：空库编号必然为 1、2，与验收编号一致
        self.id_a = self._create_ticket(TICKET1["title"], TICKET1["description"])
        self.id_b = self._create_ticket(TICKET2["title"], TICKET2["description"])
        self.assertEqual(self.id_a, 1)
        self.assertEqual(self.id_b, 2)

        # 仅将工单 2 结案，工单 1 保持 open
        closed = self.run_cli("close", str(self.id_b))
        self.assertEqual(closed.returncode, 0, f"准备样例失败: {closed.stderr}")
        self.assertEqual(closed.stderr, "")

        # 工单 1 按验收顺序追加四条备注
        for text in NOTES_TICKET1:
            self._add_note(self.id_a, text)
        # 工单 2 只有一条与工单 1 第一条相同的备注
        self._add_note(self.id_b, NOTE_TICKET2)

        self.expected_notes_a = [
            {"note_id": index, "ticket_id": self.id_a, "text": text}
            for index, text in enumerate(NOTES_TICKET1, start=1)
        ]
        self.expected_notes_b = [
            {"note_id": 1, "ticket_id": self.id_b, "text": NOTE_TICKET2},
        ]
        self.expected_ticket_rows = [
            [self.id_a, TICKET1["title"], TICKET1["description"], "open"],
            [self.id_b, TICKET2["title"], TICKET2["description"], "closed"],
        ]
        self.expected_note_rows = [
            [self.id_a, 1, NOTE_DUP],
            [self.id_a, 2, NOTE_SLASH],
            [self.id_a, 3, NOTE_MULTILINE],
            [self.id_a, 4, NOTE_DUP],
            [self.id_b, 1, NOTE_TICKET2],
        ]
        # 只有工单 2 的结案产生唯一一条状态历史
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

    def _assert_sample_db_removed(self):
        """临时目录清理后，样例库文件不得残留在磁盘上。"""
        self.assertFalse(
            Path(self.db_path).exists(), f"样例库未清理: {self.db_path}"
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

    def query_notes(self, ticket_id, keyword):
        """执行 notes --keyword，要求成功并返回解析后的 JSON 数组。"""
        result = self.run_cli("notes", str(ticket_id), "--keyword", keyword)
        self.assertEqual(result.returncode, 0, f"notes 失败: {result.stderr}")
        # 每个成功结果标准错误必须为空
        self.assertEqual(result.stderr, "")
        notes = self.parse_single_json(result.stdout)
        # 标准输出仅为一个 JSON 数组
        self.assertIsInstance(notes, list)
        return notes

    def assertNoteObject(self, note, note_id, ticket_id, text):
        """仅含 note_id/ticket_id/text；前两项为整数，text 逐字符精确相等。"""
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
        # 逐字符核对，防止首尾空白、引号、反斜杠或换行在输出中走样
        self.assertEqual([list(note["text"])], [list(text)])

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

    # ---------- 引号 "\"A\"" 只命中重复正文的序号 1、4 ----------

    def test_quote_keyword_matches_notes_1_and_4(self):
        notes = self.query_notes(self.id_a, KW_QUOTE)
        self.assertEqual([n["note_id"] for n in notes], [1, 4])
        self.assertNoteObject(notes[0], 1, self.id_a, NOTE_DUP)
        self.assertNoteObject(notes[1], 4, self.id_a, NOTE_DUP)
        # 两条重复正文分别保留为独立结果，不合并去重
        self.assertEqual(
            notes, [self.expected_notes_a[0], self.expected_notes_a[3]]
        )
        # 不混入另一张工单的备注
        self.assertTrue(all(n["ticket_id"] == self.id_a for n in notes))
        self.assert_storage_unchanged()

    # ---------- 反斜杠 "x\\y" 按字面匹配，不等同于正斜杠 ----------

    def test_backslash_keyword_matches_notes_1_and_4(self):
        notes = self.query_notes(self.id_a, KW_BACKSLASH)
        self.assertEqual([n["note_id"] for n in notes], [1, 4])
        self.assertEqual(
            notes, [self.expected_notes_a[0], self.expected_notes_a[3]]
        )
        # 备注 2 含 x/y（正斜杠），不得被 x\y 命中
        self.assertEqual(
            self.query_notes(self.id_a, "x/y"), [self.expected_notes_a[1]]
        )
        self.assert_storage_unchanged()

    # ---------- 内部双空格 "等待  确认" 只命中序号 3 ----------

    def test_internal_double_space_keyword_matches_note_3(self):
        notes = self.query_notes(self.id_a, KW_DOUBLE_SPACE)
        self.assertEqual([n["note_id"] for n in notes], [3])
        self.assertNoteObject(notes[0], 3, self.id_a, NOTE_MULTILINE)
        self.assertEqual(notes, [self.expected_notes_a[2]])
        self.assert_storage_unchanged()

    # ---------- 跨行关键字 "确认\n客户" 只命中序号 3 ----------

    def test_newline_keyword_matches_note_3(self):
        # 关键字中的 \n 是真实换行符，必须与备注正文中的换行逐字符对上
        self.assertIn("\n", KW_NEWLINE)
        notes = self.query_notes(self.id_a, KW_NEWLINE)
        self.assertEqual([n["note_id"] for n in notes], [3])
        self.assertNoteObject(notes[0], 3, self.id_a, NOTE_MULTILINE)
        self.assertEqual(notes, [self.expected_notes_a[2]])
        self.assert_storage_unchanged()

    # ---------- 内部空白与换行不折叠，不跨备注拼接 ----------

    def test_internal_whitespace_newline_and_note_boundaries_not_collapsed(self):
        for keyword in (KW_SINGLE_SPACE, KW_NO_NEWLINE, KW_CROSS_NOTE):
            with self.subTest(keyword=keyword):
                # 三者均为成功查询：退出码 0、标准错误为空、输出仅为 []
                result = self.run_cli(
                    "notes", str(self.id_a), "--keyword", keyword
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout.strip(), "[]")
                self.assertEqual(self.parse_single_json(result.stdout), [])
        # 对照：未折叠的原始关键字都能命中序号 3，证明差异仅在空白/换行本身
        self.assertEqual(
            [n["note_id"] for n in self.query_notes(self.id_a, KW_DOUBLE_SPACE)],
            [3],
        )
        self.assertEqual(
            [n["note_id"] for n in self.query_notes(self.id_a, KW_NEWLINE)],
            [3],
        )
        self.assert_storage_unchanged()

    # ---------- 工单 2：只返回它自己的序号 1，不混入工单 1 ----------

    def test_closed_ticket_query_scoped_to_its_own_note(self):
        notes = self.query_notes(self.id_b, KW_QUOTE)
        self.assertEqual([n["note_id"] for n in notes], [1])
        self.assertNoteObject(notes[0], 1, self.id_b, NOTE_TICKET2)
        self.assertEqual(notes, self.expected_notes_b)
        # 反斜杠关键字同样只命中工单 2 自己的备注
        self.assertEqual(
            [n["note_id"] for n in self.query_notes(self.id_b, KW_BACKSLASH)],
            [1],
        )
        # 跨备注拼接点只存在于工单 1 的备注之间，工单 2 必须返回 []
        self.assertEqual(self.query_notes(self.id_b, KW_CROSS_NOTE), [])
        self.assert_storage_unchanged()

    # ---------- 有效关键字首尾加空白后结果相同，内部空白不能折叠 ----------

    def test_surrounding_whitespace_is_trimmed_but_internal_kept(self):
        effective_cases = (
            (KW_QUOTE, [1, 4]),
            (KW_BACKSLASH, [1, 4]),
            (KW_DOUBLE_SPACE, [3]),
            (KW_NEWLINE, [3]),
        )
        for keyword, expected_ids in effective_cases:
            padded_variants = (
                " " + keyword + " ",
                "\t " + keyword + " \n",
                "\n" + keyword + "\t",
                " \t\n  " + keyword + "  \n\t ",
            )
            for padded in padded_variants:
                with self.subTest(keyword=keyword, padded=padded):
                    notes = self.query_notes(self.id_a, padded)
                    self.assertEqual(
                        [n["note_id"] for n in notes], expected_ids
                    )

        # 首尾空白只在外层去除：换行关键字被换行包裹后，内部换行仍保留
        self.assertEqual(
            [n["note_id"] for n in self.query_notes(self.id_a, "\n" + KW_NEWLINE)],
            [3],
        )
        # 内部空白不能折叠：单空格/去掉换行的写法都必须返回 []
        self.assertEqual(self.query_notes(self.id_a, KW_SINGLE_SPACE), [])
        self.assertEqual(self.query_notes(self.id_a, KW_NO_NEWLINE), [])
        self.assert_storage_unchanged()

    # ---------- 成功输出格式：唯一 JSON 数组，字段与正文逐字符保留 ----------

    def test_success_output_shape_and_verbatim_text(self):
        result = self.run_cli("notes", str(self.id_a), "--keyword", KW_QUOTE)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        notes = self.parse_single_json(result.stdout)
        self.assertIsInstance(notes, list)
        self.assertEqual(len(notes), 2)
        for note in notes:
            self.assertNoteObject(note, note["note_id"], self.id_a, NOTE_DUP)
        text = notes[0]["text"]
        # 首尾各两个空白、引号与反斜杠逐字符保留
        self.assertTrue(text.startswith("  ") and text.endswith("  "))
        self.assertIn('"A"', text)
        self.assertIn("x\\y", text)
        self.assertEqual(text.count('"'), 2)
        self.assertEqual(text.count("\\"), 1)

        # 换行备注正文同样逐字符保留，换行位置恰在“等待  确认”之后
        multiline = self.query_notes(self.id_a, KW_NEWLINE)
        body = multiline[0]["text"]
        self.assertEqual(body, NOTE_MULTILINE)
        self.assertEqual(body.index("\n"), len("等待  确认"))
        self.assertEqual(body.count("\n"), 1)

        # 空结果同样是标准输出上唯一的 JSON 数组，且标准错误为空
        empty = self.run_cli(
            "notes", str(self.id_a), "--keyword", KW_SINGLE_SPACE
        )
        self.assertEqual(empty.returncode, 0)
        self.assertEqual(empty.stderr, "")
        self.assertEqual(empty.stdout.strip(), "[]")
        self.assertEqual(self.parse_single_json(empty.stdout), [])
        self.assert_storage_unchanged()

    # ---------- 空或纯空白关键字：退出码 1 ----------

    def test_empty_or_blank_keyword_rejected(self):
        for keyword in ("", "   ", "\t\n \t"):
            with self.subTest(keyword=keyword):
                result = self.run_cli(
                    "notes", str(self.id_a), "--keyword", keyword
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "关键字不能为空\n")
        # 已结案的工单 2 同样拒绝，且只报关键字错误
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
        self.assertIn("--keyword", result.stderr)
        self.assert_storage_unchanged()

    # ---------- 重复查询结果一致 ----------

    def test_repeated_queries_return_identical_results(self):
        for keyword, expected_ids in (
            (KW_QUOTE, [1, 4]),
            (KW_NEWLINE, [3]),
            (KW_CROSS_NOTE, []),
        ):
            with self.subTest(keyword=keyword):
                first = self.query_notes(self.id_a, keyword)
                self.assertEqual(
                    [n["note_id"] for n in first], expected_ids
                )
                for _ in range(2):
                    self.assertEqual(
                        self.query_notes(self.id_a, keyword), first
                    )
        self.assert_storage_unchanged()

    # ---------- 成功、空结果与失败查询均不改动存量数据 ----------

    def test_all_query_outcomes_leave_storage_unchanged(self):
        # 命中、空结果、首尾空白、空关键字失败、缺取值失败各跑一遍
        self.assertEqual(
            [n["note_id"] for n in self.query_notes(self.id_a, KW_QUOTE)],
            [1, 4],
        )
        self.assertEqual(self.query_notes(self.id_a, KW_CROSS_NOTE), [])
        self.assertEqual(
            [n["note_id"]
             for n in self.query_notes(self.id_a, " " + KW_NEWLINE + " ")],
            [3],
        )
        blank = self.run_cli("notes", str(self.id_a), "--keyword", " ")
        self.assertEqual(blank.returncode, 1)
        self.assertEqual(blank.stdout, "")
        self.assertEqual(blank.stderr, "关键字不能为空\n")
        missing = self.run_cli("notes", str(self.id_a), "--keyword")
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stdout, "")

        # 查询前后工单、备注与状态历史逐行一致；工单 2 仍为 closed
        self.assert_storage_unchanged()
        # 不传 --keyword 的入口行为不变：仍返回全部四条备注
        result = self.run_cli("notes", str(self.id_a))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            self.parse_single_json(result.stdout), self.expected_notes_a
        )
        self.assert_storage_unchanged()


if __name__ == "__main__":
    unittest.main()
