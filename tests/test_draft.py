"""set-draft 与 draft 命令的回归测试。

通过真实命令行入口（python main.py）核对每张工单一份回复草稿的保存与读取：
固定样例从空库创建“登录失败”和“打印异常”两条合成工单，仅将第二条结案；
覆盖初始无草稿（text 为 null）、保存（首尾空白、换行、中文与引号按原文保留）、
整篇覆盖与重复提交、结案/重开保留草稿、跨工单隔离、跨进程读取与只读性，
以及编号、内容与用法三类确定的失败结果；另核对不含草稿表的旧库可直接读取。

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

# 第一份草稿：首尾空白必须按原文保留
FIRST_DRAFT_TEXT = " 请重试登录 "
# 覆盖后的第二份草稿：含中文、换行与引号，按原文保存
SECOND_DRAFT_TEXT = '已为您重置密码\n请使用"临时密码"登录后尽快修改'

DRAFT_KEYS = {"ticket_id", "text"}

# 超过 SQLite 有符号整数上限的编号：按工单不存在处理
OVERFLOW_ID = "9223372036854775808"


class DraftWorkflowTestCase(unittest.TestCase):
    """空库起步的草稿保存与读取正常流程；每个用例使用独立临时数据库。"""

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

    def assertDraftObject(self, draft, ticket_id, text):
        """仅含 ticket_id/text；ticket_id 为整数，text 为字符串或 None。"""
        self.assertIsInstance(draft, dict)
        self.assertEqual(set(draft.keys()), DRAFT_KEYS)
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(draft["ticket_id"]), int)
        if text is None:
            self.assertIsNone(draft["text"])
        else:
            self.assertIs(type(draft["text"]), str)
        self.assertEqual(draft, {"ticket_id": ticket_id, "text": text})

    def draft_rows(self):
        """直接读取 SQLite 中全部草稿，按工单编号排序。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT ticket_id, text FROM ticket_drafts ORDER BY ticket_id"
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

    # ---------- 最初两条工单都没有草稿 ----------

    def test_draft_initially_returns_null_text_for_both_tickets(self):
        for ticket_id in (self.id_a, self.id_b):
            with self.subTest(ticket_id=ticket_id):
                # 每次都是独立进程调用
                draft = self.read_draft(ticket_id)
                self.assertDraftObject(draft, ticket_id, None)

        # 只读：确认没有生成任何草稿或占位工单
        self.assertEqual(self.draft_rows(), [])
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            [list(row) for row in self.expected_ticket_rows()],
        )

    # ---------- 保存、覆盖与跨进程读取 ----------

    def test_set_draft_saves_verbatim_and_overwrites_without_history(self):
        saved = self.set_draft(self.id_a, FIRST_DRAFT_TEXT)
        self.assertDraftObject(saved, self.id_a, FIRST_DRAFT_TEXT)
        # 首尾空格按原文保留，中文直接可读
        self.assertTrue(saved["text"].startswith(" "))
        self.assertTrue(saved["text"].endswith(" "))
        self.assertIn("请重试登录", json.dumps(saved, ensure_ascii=False))

        # 重复提交相同正文同样成功
        again = self.set_draft(self.id_a, FIRST_DRAFT_TEXT)
        self.assertDraftObject(again, self.id_a, FIRST_DRAFT_TEXT)

        # 整篇覆盖：新正文（含换行、引号、中文）完全替换旧正文
        replaced = self.set_draft(self.id_a, SECOND_DRAFT_TEXT)
        self.assertDraftObject(replaced, self.id_a, SECOND_DRAFT_TEXT)
        self.assertIn("\n", replaced["text"])
        self.assertIn('"', replaced["text"])

        # 库中该工单只有一行草稿，即最后一次保存的全文，不保留版本
        self.assertEqual(
            [list(row) for row in self.draft_rows()],
            [[self.id_a, SECOND_DRAFT_TEXT]],
        )

        # 另一次独立进程调用 draft：返回最后一次成功保存的全文
        for _ in range(2):
            self.assertDraftObject(
                self.read_draft(self.id_a), self.id_a, SECOND_DRAFT_TEXT
            )

        # 工单本身不被草稿操作改变
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            [list(row) for row in self.expected_ticket_rows()],
        )

    # ---------- 结案与重开保留草稿，跨工单隔离 ----------

    def test_draft_survives_close_reopen_and_stays_per_ticket(self):
        self.set_draft(self.id_a, FIRST_DRAFT_TEXT)
        self.set_draft(self.id_b, SECOND_DRAFT_TEXT)

        # 已结案工单可正常保存与读取
        self.assertDraftObject(
            self.read_draft(self.id_b), self.id_b, SECOND_DRAFT_TEXT
        )

        # 结案再重开：草稿保留
        for argv in (("close", str(self.id_a)), ("reopen", str(self.id_a))):
            result = self.run_cli(*argv)
            self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
            self.assertEqual(result.stderr, "")
        self.assertDraftObject(
            self.read_draft(self.id_a), self.id_a, FIRST_DRAFT_TEXT
        )

        # 不同工单互不影响
        self.assertDraftObject(
            self.read_draft(self.id_b), self.id_b, SECOND_DRAFT_TEXT
        )
        self.assertEqual(
            [list(row) for row in self.draft_rows()],
            [
                [self.id_a, FIRST_DRAFT_TEXT],
                [self.id_b, SECOND_DRAFT_TEXT],
            ],
        )

    # ---------- 编号解析沿用 show 的规则 ----------

    def test_id_parsing_accepts_plus_sign_leading_zeros_and_blanks(self):
        self.set_draft(self.id_a, FIRST_DRAFT_TEXT)
        for raw_id in ("+1", "01", " 1 ", " +001 "):
            with self.subTest(raw_id=raw_id):
                self.assertDraftObject(
                    self.read_draft(raw_id), self.id_a, FIRST_DRAFT_TEXT
                )


class DraftErrorTestCase(unittest.TestCase):
    """失败结果：编号非法、工单不存在、内容为空与用法错误。

    样例库中已有两条工单（第二条已结案）与一份草稿，
    每次失败调用前后工单与草稿的数量和内容都必须完全一致。
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

        # 预置草稿：第一条工单一份
        saved = self.run_cli(
            "set-draft", str(self.id_a), "--text", FIRST_DRAFT_TEXT
        )
        self.assertEqual(saved.returncode, 0, f"准备样例失败: {saved.stderr}")
        self.assertEqual(saved.stderr, "")

        self.expected_ticket_rows = [
            [self.id_a, TICKET_A["title"], TICKET_A["description"], "open"],
            [self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"],
        ]
        self.expected_draft_rows = [[self.id_a, FIRST_DRAFT_TEXT]]
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

    def draft_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT ticket_id, text FROM ticket_drafts ORDER BY ticket_id"
            ).fetchall()
        finally:
            conn.close()

    def assert_storage_unchanged(self):
        """失败调用不新增或修改任何工单与草稿。"""
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

    def test_non_positive_integer_ids_rejected_for_both_commands(self):
        for command in ("set-draft", "draft"):
            for bad_id in ("abc", "0", "-1", "1.5"):
                with self.subTest(command=command, bad_id=bad_id):
                    if command == "set-draft":
                        argv = (command, bad_id, "--text", "任意内容")
                    else:
                        argv = (command, bad_id)
                    self.assert_business_error(argv, "工单编号必须为正整数")

    # ---------- 编号为正整数但工单不存在 ----------

    def test_missing_ticket_reported_for_both_commands(self):
        for missing_id in ("999", OVERFLOW_ID):
            with self.subTest(missing_id=missing_id):
                self.assert_business_error(
                    ("draft", missing_id), "工单不存在"
                )
                self.assert_business_error(
                    ("set-draft", missing_id, "--text", "任意内容"),
                    "工单不存在",
                )

    # ---------- 草稿内容为空或纯空白 ----------

    def test_set_draft_rejects_empty_or_blank_text(self):
        for text in ("", "   ", "\t\n  \t"):
            with self.subTest(text=text):
                self.assert_business_error(
                    ("set-draft", str(self.id_a), "--text", text),
                    "草稿不能为空",
                )
        # 对已结案工单同样拒绝，且不改动存量数据
        self.assert_business_error(
            ("set-draft", str(self.id_b), "--text", "   "),
            "草稿不能为空",
        )
        # 原草稿保持最后一次成功保存的全文
        result = self.run_cli("draft", str(self.id_a))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {"ticket_id": self.id_a, "text": FIRST_DRAFT_TEXT},
        )

    # ---------- 用法错误：退出码 2 ----------

    def test_usage_errors_exit_with_code_2(self):
        usage_cases = [
            # draft 缺少工单编号
            ("draft",),
            # draft 携带未知选项
            ("draft", str(self.id_a), "--unknown"),
            # set-draft 缺少工单编号与 --text
            ("set-draft",),
            # set-draft 缺少工单编号
            ("set-draft", "--text", "任意内容"),
            # set-draft 缺少 --text
            ("set-draft", str(self.id_a)),
            # --text 缺少取值
            ("set-draft", str(self.id_a), "--text"),
            # set-draft 携带未知选项
            ("set-draft", str(self.id_a), "--text", "任意内容", "--unknown"),
        ]
        for argv in usage_cases:
            with self.subTest(argv=argv):
                self.assert_usage_error(argv)


class DraftLegacyDatabaseTestCase(unittest.TestCase):
    """旧库无需重建：不含草稿表的既有库可直接读取，原工单初始没有草稿。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "legacy.sqlite")

        # 手工构造只含工单表的旧库，预置一张编号为 1 的工单
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

    def test_legacy_database_reads_null_draft_then_saves(self):
        # 旧库直接读取：成功且 text 为 null
        result = self.run_cli("draft", "1")
        self.assertEqual(result.returncode, 0, f"draft 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout), {"ticket_id": 1, "text": None}
        )

        # 旧库保存与再次读取同样正常
        saved = self.run_cli("set-draft", "1", "--text", FIRST_DRAFT_TEXT)
        self.assertEqual(saved.returncode, 0, f"set-draft 失败: {saved.stderr}")
        self.assertEqual(saved.stderr, "")
        self.assertEqual(
            json.loads(saved.stdout),
            {"ticket_id": 1, "text": FIRST_DRAFT_TEXT},
        )

        reread = self.run_cli("draft", "1")
        self.assertEqual(reread.returncode, 0)
        self.assertEqual(reread.stderr, "")
        self.assertEqual(
            json.loads(reread.stdout),
            {"ticket_id": 1, "text": FIRST_DRAFT_TEXT},
        )


if __name__ == "__main__":
    unittest.main()
