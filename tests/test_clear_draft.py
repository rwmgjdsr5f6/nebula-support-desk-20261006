"""clear-draft 命令的回归测试。

通过真实命令行入口（python main.py）核对主动清除单张工单当前回复草稿的行为：
固定样例从空库创建“登录失败”（open）和“打印异常”（closed）两条合成工单，
各保存不同草稿；覆盖清除成功（clear-draft 与随后 draft 均返回 text 为 null 的
同一结构）、本来没有草稿与重复清除同样成功、结案工单可清除、跨工单隔离、
清除后可重新保存、编号解析沿用 show 的规则，以及编号非法、工单不存在与
用法错误三类确定的失败结果；另核对清除不改变工单、备注、状态历史与
summary/list，且不含草稿表的旧库可直接清除。

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

# 两条工单预先保存的不同草稿
DRAFT_TEXT_A = " 请重试登录 "
DRAFT_TEXT_B = '已为您重置密码\n请使用"临时密码"登录后尽快修改'
# 清除后重新保存的新正文
NEW_DRAFT_TEXT = "问题已处理，请确认"

DRAFT_KEYS = {"ticket_id", "text"}

# 超过 SQLite 有符号整数上限的编号：按工单不存在处理
OVERFLOW_ID = "9223372036854775808"


class ClearDraftWorkflowTestCase(unittest.TestCase):
    """空库起步：两张工单（一 open 一 closed）各存草稿后的清除正常流程。"""

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

        self.set_draft(self.id_a, DRAFT_TEXT_A)
        self.set_draft(self.id_b, DRAFT_TEXT_B)

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
        return self.parse_single_json(result.stdout)

    def clear_draft(self, ticket_id):
        """执行 clear-draft，要求成功并返回解析后的唯一草稿 JSON 对象。"""
        result = self.run_cli("clear-draft", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"clear-draft 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json(result.stdout)

    def read_draft(self, ticket_id):
        """执行 draft，要求成功并返回解析后的唯一草稿 JSON 对象。"""
        result = self.run_cli("draft", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"draft 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json(result.stdout)

    def assertClearedObject(self, draft, ticket_id):
        """清除成功的唯一输出：仅含 ticket_id（整数）与 text（null）。"""
        self.assertIsInstance(draft, dict)
        self.assertEqual(set(draft.keys()), DRAFT_KEYS)
        self.assertIs(type(draft["ticket_id"]), int)
        self.assertIsNone(draft["text"])
        self.assertEqual(draft, {"ticket_id": ticket_id, "text": None})

    def draft_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT ticket_id, text FROM ticket_drafts ORDER BY ticket_id"
            ).fetchall()
        finally:
            conn.close()

    # ---------- 验收主流程 ----------

    def test_acceptance_clear_open_ticket_then_read_returns_null_twice(self):
        # clear-draft 1 与随后的 draft 1 均输出同一结构
        self.assertClearedObject(self.clear_draft(self.id_a), self.id_a)
        self.assertClearedObject(self.read_draft(self.id_a), self.id_a)

        # 工单 2（closed）的草稿仍为原文
        self.assertEqual(
            self.read_draft(self.id_b),
            {"ticket_id": self.id_b, "text": DRAFT_TEXT_B},
        )
        self.assertEqual(
            [list(row) for row in self.draft_rows()],
            [[self.id_b, DRAFT_TEXT_B]],
        )

    def test_closed_ticket_can_be_cleared(self):
        self.assertClearedObject(self.clear_draft(self.id_b), self.id_b)
        self.assertClearedObject(self.read_draft(self.id_b), self.id_b)
        # 工单 1 的草稿不受影响
        self.assertEqual(
            self.read_draft(self.id_a),
            {"ticket_id": self.id_a, "text": DRAFT_TEXT_A},
        )

    def test_repeated_clear_without_draft_still_succeeds(self):
        # 第一次清除有草稿
        self.assertClearedObject(self.clear_draft(self.id_a), self.id_a)
        # 本来没有草稿后再次清除仍成功，返回同一结构
        self.assertClearedObject(self.clear_draft(self.id_a), self.id_a)
        self.assertClearedObject(self.read_draft(self.id_a), self.id_a)
        self.assertEqual(
            [list(row) for row in self.draft_rows()],
            [[self.id_b, DRAFT_TEXT_B]],
        )

    def test_clear_then_save_returns_new_text(self):
        self.clear_draft(self.id_a)
        saved = self.set_draft(self.id_a, NEW_DRAFT_TEXT)
        self.assertEqual(saved, {"ticket_id": self.id_a, "text": NEW_DRAFT_TEXT})
        # 独立进程再次读取，仍按新原文返回，而不是旧草稿或空串
        self.assertEqual(
            self.read_draft(self.id_a),
            {"ticket_id": self.id_a, "text": NEW_DRAFT_TEXT},
        )

    # ---------- 编号解析沿用 show 的规则 ----------

    def test_id_parsing_accepts_plus_sign_leading_zeros_and_blanks(self):
        for raw_id in ("+1", "01", " 1 ", " +001 "):
            with self.subTest(raw_id=raw_id):
                result = self.run_cli("clear-draft", raw_id)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(
                    self.parse_single_json(result.stdout),
                    {"ticket_id": self.id_a, "text": None},
                )
        # 上述写法都指向同一张工单，最终无草稿；工单 2 不受影响
        self.assertIsNone(self.read_draft(self.id_a)["text"])
        self.assertEqual(
            self.read_draft(self.id_b),
            {"ticket_id": self.id_b, "text": DRAFT_TEXT_B},
        )


class ClearDraftPreservesRecordsTestCase(unittest.TestCase):
    """清除草稿不改变工单、备注、状态历史，summary 与 list 输出保持不变。"""

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
        # 工单 1 结案再重开：产生两条状态历史
        for argv in (("close", str(self.id_a)), ("reopen", str(self.id_a))):
            result = self.run_cli(*argv)
            self.assertEqual(result.returncode, 0, result.stderr)
        # 工单 2 结案
        self.assertEqual(self.run_cli("close", str(self.id_b)).returncode, 0)
        # 各加备注
        self.run_cli("add-note", str(self.id_a), "--text", "工单1备注")
        self.run_cli("add-note", str(self.id_b), "--text", "工单2备注")
        # 各存草稿
        self.run_cli("set-draft", str(self.id_a), "--text", DRAFT_TEXT_A)
        self.run_cli("set-draft", str(self.id_b), "--text", DRAFT_TEXT_B)

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
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["id"]

    @staticmethod
    def _json(result):
        return json.loads(result.stdout)

    def table_rows(self, table, columns):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                f"SELECT {columns} FROM {table} ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def test_clear_does_not_change_ticket_notes_or_history(self):
        before = {
            "tickets": self.table_rows(
                "tickets", "id, title, description, status"
            ),
            "notes": self.table_rows(
                "ticket_notes", "id, ticket_id, note_id, text"
            ),
            "history": self.table_rows(
                "ticket_history", "id, ticket_id, event_id, from_status, to_status"
            ),
            "summary_a": self._json(self.run_cli("summary", str(self.id_a))),
            "summary_b": self._json(self.run_cli("summary", str(self.id_b))),
            "list": self._json(self.run_cli("list")),
        }

        for ticket_id in (self.id_a, self.id_b):
            result = self.run_cli("clear-draft", str(ticket_id))
            self.assertEqual(result.returncode, 0, result.stderr)

        after = {
            "tickets": self.table_rows(
                "tickets", "id, title, description, status"
            ),
            "notes": self.table_rows(
                "ticket_notes", "id, ticket_id, note_id, text"
            ),
            "history": self.table_rows(
                "ticket_history", "id, ticket_id, event_id, from_status, to_status"
            ),
            "summary_a": self._json(self.run_cli("summary", str(self.id_a))),
            "summary_b": self._json(self.run_cli("summary", str(self.id_b))),
            "list": self._json(self.run_cli("list")),
        }
        self.assertEqual(before, after)
        # 两张草稿均已清除
        self.assertEqual(
            self.table_rows("ticket_drafts", "id, ticket_id, text"), []
        )


class ClearDraftErrorTestCase(unittest.TestCase):
    """失败结果：编号非法、工单不存在与用法错误，均不清除已有草稿。

    样例库中工单 1（open）预先保存一份草稿；每次失败调用后该草稿必须原样保留。
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
        saved = self.run_cli(
            "set-draft", str(self.id_a), "--text", DRAFT_TEXT_A
        )
        self.assertEqual(saved.returncode, 0, f"准备样例失败: {saved.stderr}")
        self.assertEqual(saved.stderr, "")

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
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["id"]

    def draft_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT ticket_id, text FROM ticket_drafts ORDER BY ticket_id"
            ).fetchall()
        finally:
            conn.close()

    def assert_draft_intact(self):
        self.assertEqual(
            [list(row) for row in self.draft_rows()],
            [[self.id_a, DRAFT_TEXT_A]],
        )

    def assert_business_error(self, argv, message):
        """退出码 1、标准输出为空、标准错误仅为提示语及换行，草稿不变。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 1, f"argv={argv!r}")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, message + "\n")
        self.assert_draft_intact()

    def assert_usage_error(self, argv):
        """退出码 2、标准输出为空、标准错误包含用法提示，草稿不变。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 2, f"argv={argv!r}: {result.stderr}")
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assert_draft_intact()

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
        for argv in (
            ("clear-draft",),                          # 缺少编号
            ("clear-draft", str(self.id_a), "--unknown"),  # 未知选项
            ("clear-draft", str(self.id_a), "2"),     # 多余位置参数
        ):
            with self.subTest(argv=argv):
                self.assert_usage_error(argv)


class ClearDraftLegacyDatabaseTestCase(unittest.TestCase):
    """旧库无需重建：不含草稿表的既有库可直接清除，原工单按没有草稿处理。"""

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

    def draft_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT ticket_id, text FROM ticket_drafts ORDER BY ticket_id"
            ).fetchall()
        finally:
            conn.close()

    def test_legacy_database_clears_as_no_draft(self):
        # 旧库直接清除：成功且 text 为 null
        result = self.run_cli("clear-draft", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout), {"ticket_id": 1, "text": None}
        )
        # 重复清除仍成功
        again = self.run_cli("clear-draft", "1")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(
            json.loads(again.stdout), {"ticket_id": 1, "text": None}
        )
        # draft 仍返回无草稿结果
        read = self.run_cli("draft", "1")
        self.assertEqual(read.returncode, 0)
        self.assertEqual(
            json.loads(read.stdout), {"ticket_id": 1, "text": None}
        )
        # 不产生空字符串或占位草稿
        self.assertEqual([list(row) for row in self.draft_rows()], [])

        # 保存后再清除同样正常
        saved = self.run_cli("set-draft", "1", "--text", DRAFT_TEXT_A)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        cleared = self.run_cli("clear-draft", "1")
        self.assertEqual(cleared.returncode, 0)
        self.assertEqual(
            json.loads(cleared.stdout), {"ticket_id": 1, "text": None}
        )
        self.assertEqual([list(row) for row in self.draft_rows()], [])


if __name__ == "__main__":
    unittest.main()
