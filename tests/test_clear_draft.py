"""clear-draft 命令的回归测试。

通过真实命令行入口（python main.py）核对单张工单当前回复草稿的主动清除：
固定样例从空库创建“登录失败”（open）和“打印异常”（closed）两条合成工单，
预先各保存一份不同草稿，覆盖清除成功输出（text 为 null）、清除后 draft 仍为
无草稿结果、本来无草稿与重复清除同样成功、open/closed 工单均可清除、清除后
可重新 set-draft 并原文读回、跨工单隔离、不改动工单/备注/状态历史，以及编号
与用法两类确定的失败结果（失败不清除已有草稿）；另核对不含草稿表的旧库可直接
清除（按没有草稿处理）。

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

# 两张工单预先各保存一份不同草稿
FIRST_DRAFT_TEXT = " 请重试登录 "
SECOND_DRAFT_TEXT = '已为您重置密码\n请使用"临时密码"登录后尽快修改'

# 清除后重新保存的新正文：含首尾空白与换行，仍按原文返回
NEW_DRAFT_TEXT = " 新的回复正文\n第二行 "

DRAFT_KEYS = {"ticket_id", "text"}

# 超过 SQLite 有符号整数上限的编号：按工单不存在处理
OVERFLOW_ID = "9223372036854775808"


class ClearDraftWorkflowTestCase(unittest.TestCase):
    """空库起步的草稿清除正常流程；每个用例使用独立临时数据库。"""

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

        # 预先各保存一份不同草稿
        self.set_draft(self.id_a, FIRST_DRAFT_TEXT)
        self.set_draft(self.id_b, SECOND_DRAFT_TEXT)
        self.assert_draft_rows(
            [
                (self.id_a, FIRST_DRAFT_TEXT),
                (self.id_b, SECOND_DRAFT_TEXT),
            ]
        )

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

    def set_draft(self, ticket_id, text):
        """执行 set-draft，要求成功并返回解析后的唯一草稿 JSON 对象。"""
        result = self.run_cli("set-draft", str(ticket_id), "--text", text)
        self.assertEqual(result.returncode, 0, f"set-draft 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        draft = self.parse_single_json(result.stdout)
        self.assertIsInstance(draft, dict)
        return draft

    def read_draft(self, ticket_id):
        """执行 draft，要求成功并返回解析后的唯一草稿 JSON 对象。"""
        result = self.run_cli("draft", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"draft 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        draft = self.parse_single_json(result.stdout)
        self.assertIsInstance(draft, dict)
        return draft

    def clear_draft(self, ticket_id):
        """执行 clear-draft，要求成功并返回解析后的唯一草稿 JSON 对象。"""
        result = self.run_cli("clear-draft", str(ticket_id))
        self.assertEqual(
            result.returncode, 0, f"clear-draft 失败: {result.stderr}"
        )
        self.assertEqual(result.stderr, "")
        draft = self.parse_single_json(result.stdout)
        self.assertIsInstance(draft, dict)
        return draft

    def assertNullDraftObject(self, draft, ticket_id):
        """成功清除/无草稿结果：仅含 ticket_id/text，text 必须为 null。

        不用空字符串或占位内容代替无草稿。
        """
        self.assertEqual(set(draft.keys()), DRAFT_KEYS)
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(draft["ticket_id"]), int)
        self.assertIsNone(draft["text"])
        self.assertEqual(draft, {"ticket_id": ticket_id, "text": None})

    def assert_draft_rows(self, expected):
        """直接读取 SQLite 中全部草稿，按工单编号排序后逐行核对。"""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT ticket_id, text FROM ticket_drafts ORDER BY ticket_id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([list(row) for row in rows], [list(row) for row in expected])

    def assert_ticket_rows_unchanged(self):
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [list(row) for row in rows],
            [
                [self.id_a, TICKET_A["title"], TICKET_A["description"], "open"],
                [self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"],
            ],
        )

    # ---------- 验收主流程：清除、重读、重复清除、再保存 ----------

    def test_clear_draft_acceptance_flow(self):
        # 清除工单 1（open）：退出码 0、标准错误为空、stdout 仅一个
        # {"ticket_id": 1, "text": null}
        cleared = self.clear_draft(self.id_a)
        self.assertNullDraftObject(cleared, self.id_a)

        # 重新运行 draft 仍返回无草稿结果（独立进程）
        self.assertNullDraftObject(self.read_draft(self.id_a), self.id_a)

        # 工单 2（closed）的草稿仍为原文，清除工单 1 不波及工单 2
        draft_b = self.read_draft(self.id_b)
        self.assertEqual(
            draft_b, {"ticket_id": self.id_b, "text": SECOND_DRAFT_TEXT}
        )

        # 库中只剩工单 2 的一行草稿，且不用空串/占位代替工单 1 的无草稿
        self.assert_draft_rows([(self.id_b, SECOND_DRAFT_TEXT)])

        # 再次清除工单 1（本来已无草稿）仍成功，返回同一结构
        again = self.clear_draft(self.id_a)
        self.assertNullDraftObject(again, self.id_a)
        self.assert_draft_rows([(self.id_b, SECOND_DRAFT_TEXT)])

        # 此后用 set-draft 保存新正文并读取，仍按原文返回
        saved = self.set_draft(self.id_a, NEW_DRAFT_TEXT)
        self.assertEqual(
            saved, {"ticket_id": self.id_a, "text": NEW_DRAFT_TEXT}
        )
        self.assertEqual(
            self.read_draft(self.id_a),
            {"ticket_id": self.id_a, "text": NEW_DRAFT_TEXT},
        )
        self.assert_draft_rows(
            [
                (self.id_a, NEW_DRAFT_TEXT),
                (self.id_b, SECOND_DRAFT_TEXT),
            ]
        )

    # ---------- closed 工单同样可清除，且本来无草稿也成功 ----------

    def test_clear_closed_ticket_draft_and_already_empty_draft(self):
        # closed 工单可以清除
        cleared = self.clear_draft(self.id_b)
        self.assertNullDraftObject(cleared, self.id_b)
        self.assertNullDraftObject(self.read_draft(self.id_b), self.id_b)
        # 重复清除 closed 工单同样成功
        self.assertNullDraftObject(self.clear_draft(self.id_b), self.id_b)
        self.assert_draft_rows([(self.id_a, FIRST_DRAFT_TEXT)])

        # 新建工单本就没有草稿：清除仍成功返回 null，不产生占位行
        id_c = self._create_ticket("导出失败", "")
        self.assertNullDraftObject(self.clear_draft(id_c), id_c)
        self.assertNullDraftObject(self.read_draft(id_c), id_c)
        # 重复清除无草稿工单
        self.assertNullDraftObject(self.clear_draft(id_c), id_c)
        self.assert_draft_rows([(self.id_a, FIRST_DRAFT_TEXT)])

    # ---------- 清除不改变工单本身、备注与状态历史 ----------

    def test_clear_draft_does_not_change_tickets_notes_or_history(self):
        # 给工单 1 追加一条内部备注，并让它经历一次结案/重开历史
        note = self.run_cli("add-note", str(self.id_a), "--text", "内部备注原文")
        self.assertEqual(note.returncode, 0, f"准备样例失败: {note.stderr}")
        for argv in (("close", str(self.id_a)), ("reopen", str(self.id_a))):
            result = self.run_cli(*argv)
            self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")

        self.clear_draft(self.id_a)
        self.clear_draft(self.id_b)
        self.assert_ticket_rows_unchanged()

        # 备注原样保留
        notes = self.run_cli("notes", str(self.id_a))
        self.assertEqual(notes.returncode, 0)
        self.assertEqual(
            json.loads(notes.stdout),
            [{"note_id": 1, "ticket_id": self.id_a, "text": "内部备注原文"}],
        )
        # 工单 1 的状态历史为两条（open→closed、closed→open），不受清除影响
        history_a = self.run_cli("history", str(self.id_a))
        self.assertEqual(history_a.returncode, 0)
        self.assertEqual(
            [(e["from_status"], e["to_status"]) for e in json.loads(history_a.stdout)],
            [("open", "closed"), ("closed", "open")],
        )
        # 工单 2 仍保留结案产生的一条历史
        history_b = self.run_cli("history", str(self.id_b))
        self.assertEqual(
            [e["event_id"] for e in json.loads(history_b.stdout)], [1]
        )

        # show / summary / list 的原有输出保持不变
        show_a = self.run_cli("show", str(self.id_a))
        self.assertEqual(show_a.returncode, 0)
        self.assertEqual(
            json.loads(show_a.stdout),
            {
                "id": self.id_a,
                "title": TICKET_A["title"],
                "description": TICKET_A["description"],
                "status": "open",
            },
        )
        summary_b = self.run_cli("summary", str(self.id_b))
        self.assertEqual(summary_b.returncode, 0)
        summary = json.loads(summary_b.stdout)
        self.assertEqual(summary["ticket"]["status"], "closed")
        self.assertEqual(summary["status_change_count"], 1)

        listed = self.run_cli("list")
        self.assertEqual(listed.returncode, 0)
        self.assertEqual(
            [t["id"] for t in json.loads(listed.stdout)],
            [self.id_a, self.id_b],
        )

    # ---------- 编号解析与 show 一致：正号、前导零、首尾空白 ----------

    def test_id_parsing_accepts_plus_sign_leading_zeros_and_blanks(self):
        for raw_id in ("+1", "01", " 1 ", " +001 "):
            with self.subTest(raw_id=raw_id):
                # 用例库工单从编号 1 开始，四种写法都命中工单 1
                draft = self.clear_draft(raw_id)
                self.assertNullDraftObject(draft, self.id_a)
        self.assert_draft_rows([(self.id_b, SECOND_DRAFT_TEXT)])


class ClearDraftErrorTestCase(unittest.TestCase):
    """失败结果：编号非法、工单不存在与用法错误。

    样例库中两条工单（第二条已结案）各有一份草稿；每次失败调用前后，
    工单与草稿的数量和内容都必须完全一致（失败不清除草稿、不生成占位）。
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

        # 预置两份不同草稿
        save_a = self.run_cli(
            "set-draft", str(self.id_a), "--text", FIRST_DRAFT_TEXT
        )
        self.assertEqual(save_a.returncode, 0, f"准备样例失败: {save_a.stderr}")
        save_b = self.run_cli(
            "set-draft", str(self.id_b), "--text", SECOND_DRAFT_TEXT
        )
        self.assertEqual(save_b.returncode, 0, f"准备样例失败: {save_b.stderr}")

        self.expected_ticket_rows = [
            [self.id_a, TICKET_A["title"], TICKET_A["description"], "open"],
            [self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"],
        ]
        self.expected_draft_rows = [
            [self.id_a, FIRST_DRAFT_TEXT],
            [self.id_b, SECOND_DRAFT_TEXT],
        ]
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

    def draft_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT ticket_id, text FROM ticket_drafts ORDER BY ticket_id"
            ).fetchall()
        finally:
            conn.close()

    def assert_storage_unchanged(self):
        """失败调用不清除草稿，也不新增或修改任何工单。"""
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            self.expected_ticket_rows,
        )
        self.assertEqual(
            [list(row) for row in self.draft_rows()], self.expected_draft_rows
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

    def test_non_positive_integer_ids_rejected(self):
        for bad_id in ("abc", "0", "-1", "1.5"):
            with self.subTest(bad_id=bad_id):
                self.assert_business_error(
                    ("clear-draft", bad_id), "工单编号必须为正整数"
                )

    # ---------- 编号为正整数但工单不存在 ----------

    def test_missing_ticket_reported(self):
        for missing_id in ("999", OVERFLOW_ID):
            with self.subTest(missing_id=missing_id):
                self.assert_business_error(
                    ("clear-draft", missing_id), "工单不存在"
                )

    # ---------- 用法错误：退出码 2 ----------

    def test_usage_errors_exit_with_code_2(self):
        usage_cases = [
            # 缺少工单编号
            ("clear-draft",),
            # 携带未知选项
            ("clear-draft", str(self.id_a), "--unknown"),
            # 多余位置参数
            ("clear-draft", str(self.id_a), "extra"),
        ]
        for argv in usage_cases:
            with self.subTest(argv=argv):
                self.assert_usage_error(argv)


class ClearDraftLegacyDatabaseTestCase(unittest.TestCase):
    """旧库无需重建：不含草稿表的既有库中，原工单按没有草稿处理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "legacy.sqlite")

        # 手工构造只含工单表的旧库，预置一张编号为 1 的 open 工单
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO tickets (title, description, status) "
                "VALUES (?, ?, ?)",
                (TICKET_A["title"], TICKET_A["description"], "open"),
            )
            conn.commit()
        finally:
            conn.close()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def test_legacy_database_clears_as_null_without_placeholder(self):
        # 旧库直接清除：成功且 text 为 null（按没有草稿处理）
        result = self.run_cli("clear-draft", "1")
        self.assertEqual(result.returncode, 0, f"clear-draft 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout), {"ticket_id": 1, "text": None}
        )

        # 重复清除仍成功；draft 读取也是无草稿结果
        again = self.run_cli("clear-draft", "1")
        self.assertEqual(again.returncode, 0)
        self.assertEqual(again.stderr, "")
        self.assertEqual(
            json.loads(again.stdout), {"ticket_id": 1, "text": None}
        )
        reread = self.run_cli("draft", "1")
        self.assertEqual(reread.returncode, 0)
        self.assertEqual(reread.stderr, "")
        self.assertEqual(
            json.loads(reread.stdout), {"ticket_id": 1, "text": None}
        )

        # 草稿表已由连接初始化补建，但不含任何占位行；原工单保留
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("ticket_drafts", tables)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ticket_drafts").fetchone()[0],
                0,
            )
            ticket = conn.execute(
                "SELECT id, title, description, status FROM tickets WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            list(ticket),
            [1, TICKET_A["title"], TICKET_A["description"], "open"],
        )

        # 补建后仍可正常保存、再次清除
        saved = self.run_cli("set-draft", "1", "--text", FIRST_DRAFT_TEXT)
        self.assertEqual(saved.returncode, 0, f"set-draft 失败: {saved.stderr}")
        cleared = self.run_cli("clear-draft", "1")
        self.assertEqual(cleared.returncode, 0)
        self.assertEqual(
            json.loads(cleared.stdout), {"ticket_id": 1, "text": None}
        )


if __name__ == "__main__":
    unittest.main()
