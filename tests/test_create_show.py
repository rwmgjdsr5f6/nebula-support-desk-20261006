"""create 与 show 基础流程的回归测试。

通过真实命令行入口（python main.py）核对“创建后按编号重新查看”这条
基础流程的退出码、标准输出与标准错误，并覆盖创建被拒绝与查询失败的边界。
创建与查询分属独立进程，共用同一个通过 --db 指定的临时数据库。
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

# 第一条工单：标题与描述首尾都带空白；标题去空白后保存，描述按原文保留
FULL_TITLE = "  登录失败  "
FULL_DESCRIPTION = "  重置密码后仍无法登录  "
TRIMMED_TITLE = "登录失败"
# 第二条工单创建时省略 --description
SECOND_TITLE = "导出失败"

# 拒绝创建样例库中预先存在的工单
REFERENCE_TITLE = "打印异常"
REFERENCE_DESCRIPTION = "更换网络后无法打印"

TICKET_KEYS = {"id", "title", "description", "status"}


class CreateThenShowTestCase(unittest.TestCase):
    """空库起步：创建后按编号重新查看的正常流程。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

    # ---------- 辅助方法 ----------

    def run_cli(self, *args):
        """以真实命令行入口运行 main.py，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def create_ticket(self, *args):
        """执行 create 命令，要求成功并返回解析后的唯一 JSON 对象。"""
        result = self.run_cli("create", *args)
        self.assertEqual(result.returncode, 0, f"create 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json_object(result.stdout)

    def show_ticket(self, ticket_id):
        """执行 show 命令，要求成功并返回解析后的唯一 JSON 对象。"""
        result = self.run_cli("show", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"show 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json_object(result.stdout)

    @staticmethod
    def parse_single_json_object(stdout):
        """标准输出必须恰好是一个 JSON 对象，不允许前后夹带说明文字。"""
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        if not isinstance(value, dict) or stripped[end:].strip() != "":
            raise AssertionError(f"标准输出不是唯一 JSON 对象: {stdout!r}")
        return value

    def assertTicketShape(self, ticket):
        """仅含 id/title/description/status；id 为整数，其余为字符串。"""
        self.assertIsInstance(ticket, dict)
        self.assertEqual(set(ticket.keys()), TICKET_KEYS)
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(ticket["id"]), int)
        for key in ("title", "description", "status"):
            self.assertIs(type(ticket[key]), str)

    def ticket_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def ticket_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        finally:
            conn.close()

    def create_first_ticket(self):
        """在空库中创建首尾带空白的第一条工单。"""
        return self.create_ticket(
            "--title", FULL_TITLE, "--description", FULL_DESCRIPTION
        )

    # ---------- 空库创建 ----------

    def test_create_in_empty_database_trims_title_and_keeps_description_spaces(self):
        result = self.run_cli(
            "create", "--title", FULL_TITLE, "--description", FULL_DESCRIPTION
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

        ticket = self.parse_single_json_object(result.stdout)
        self.assertTicketShape(ticket)
        # 编号为正整数
        self.assertGreater(ticket["id"], 0)
        # 标题去除首尾空白，描述首尾空白原样保留，初始状态固定 open
        self.assertEqual(ticket["title"], TRIMMED_TITLE)
        self.assertEqual(ticket["description"], FULL_DESCRIPTION)
        self.assertTrue(ticket["description"].startswith("  "))
        self.assertTrue(ticket["description"].endswith("  "))
        self.assertEqual(ticket["status"], "open")
        # 中文正文直接可读，不做 \\u 转义
        self.assertIn(TRIMMED_TITLE, result.stdout)
        self.assertIn("重置密码后仍无法登录", result.stdout)

        # 空库中只落库这一条工单，内容与创建结果一致
        self.assertEqual(self.ticket_count(), 1)
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            [
                [
                    ticket["id"],
                    TRIMMED_TITLE,
                    FULL_DESCRIPTION,
                    "open",
                ]
            ],
        )

    # ---------- 创建后按编号查看 ----------

    def test_show_returns_the_same_ticket_as_create(self):
        created = self.create_first_ticket()

        result = self.run_cli("show", str(created["id"]))

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

        shown = self.parse_single_json_object(result.stdout)
        self.assertTicketShape(shown)
        # 解析后按字段比较，不依赖 JSON 键顺序或格式空格
        self.assertEqual(shown, created)
        # 中文在 show 的标准输出中同样直接可读
        self.assertIn(TRIMMED_TITLE, result.stdout)

    # ---------- 省略描述的第二条工单 ----------

    def test_create_without_description_uses_empty_string_and_keeps_first(self):
        first = self.create_first_ticket()

        # 省略 --description 再创建一条
        result = self.run_cli("create", "--title", SECOND_TITLE)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

        second = self.parse_single_json_object(result.stdout)
        self.assertTicketShape(second)
        self.assertGreater(second["id"], 0)
        self.assertEqual(second["title"], SECOND_TITLE)
        self.assertEqual(second["description"], "")
        self.assertEqual(second["status"], "open")
        # 第二条获得不同编号
        self.assertNotEqual(second["id"], first["id"])

        # 第一条工单仍可按原编号查看，内容保持创建结果
        self.assertEqual(self.show_ticket(first["id"]), first)
        self.assertEqual(self.show_ticket(second["id"]), second)
        self.assertEqual(self.ticket_count(), 2)

    # ---------- 重复查看不改动存量记录 ----------

    def test_repeated_show_does_not_modify_stored_records(self):
        first = self.create_first_ticket()
        second = self.create_ticket("--title", SECOND_TITLE)
        expected_rows = [
            [first["id"], TRIMMED_TITLE, FULL_DESCRIPTION, "open"],
            [second["id"], SECOND_TITLE, "", "open"],
        ]
        self.assertEqual([list(row) for row in self.ticket_rows()], expected_rows)

        # 对同一编号重复查看，每次结果一致且成功
        for _ in range(3):
            self.assertEqual(self.show_ticket(first["id"]), first)
        self.assertEqual(self.show_ticket(second["id"]), second)

        # 存量记录的数量、编号与各字段内容完全不变
        self.assertEqual(self.ticket_count(), 2)
        self.assertEqual([list(row) for row in self.ticket_rows()], expected_rows)


class CreateRejectionTestCase(unittest.TestCase):
    """已有一条工单的库：拒绝创建的各种标题。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        result = self.run_cli(
            "create",
            "--title",
            REFERENCE_TITLE,
            "--description",
            REFERENCE_DESCRIPTION,
        )
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        self.reference = self.parse_single_json_object(result.stdout)
        self.expected_rows = [
            [
                self.reference["id"],
                REFERENCE_TITLE,
                REFERENCE_DESCRIPTION,
                "open",
            ]
        ]

    # ---------- 辅助方法 ----------

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    @staticmethod
    def parse_single_json_object(stdout):
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        if not isinstance(value, dict) or stripped[end:].strip() != "":
            raise AssertionError(f"标准输出不是唯一 JSON 对象: {stdout!r}")
        return value

    def ticket_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def assert_storage_unchanged(self):
        """不新增占位工单，已有工单内容不变。"""
        self.assertEqual(len(self.ticket_rows()), 1)
        self.assertEqual(
            [list(row) for row in self.ticket_rows()], self.expected_rows
        )
        shown = self.run_cli("show", str(self.reference["id"]))
        self.assertEqual(shown.returncode, 0)
        self.assertEqual(shown.stderr, "")
        self.assertEqual(
            self.parse_single_json_object(shown.stdout), self.reference
        )

    # ---------- 标题校验 ----------

    def test_create_rejects_missing_empty_or_blank_title(self):
        # 省略标题、空字符串标题、纯空白标题（空格 / 制表符换行混合）
        for argv in (
            ("create",),
            ("create", "--title", ""),
            ("create", "--title", "   "),
            ("create", "--title", "\t\n  \t"),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                # 错误只写标准错误，仅为提示语加末尾换行
                self.assertEqual(result.stderr, "标题不能为空\n")
                self.assert_storage_unchanged()


class CreateRejectionOnEmptyDatabaseTestCase(unittest.TestCase):
    """空库上拒绝创建：只自动建库建表，不生成占位工单。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def ticket_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        finally:
            conn.close()

    def test_rejected_creates_leave_no_placeholder_ticket(self):
        for argv in (
            ("create",),
            ("create", "--title", ""),
            ("create", "--title", "   "),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "标题不能为空\n")
                # 库中始终没有任何工单
                self.assertEqual(self.ticket_count(), 0)


class ShowErrorTestCase(unittest.TestCase):
    """两条工单的小型样例库上的查询失败边界。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.first = self.create_ticket(
            "--title", FULL_TITLE, "--description", FULL_DESCRIPTION
        )
        self.second = self.create_ticket("--title", SECOND_TITLE)
        self.expected_rows = [
            [self.first["id"], TRIMMED_TITLE, FULL_DESCRIPTION, "open"],
            [self.second["id"], SECOND_TITLE, "", "open"],
        ]

    # ---------- 辅助方法 ----------

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def create_ticket(self, *args):
        result = self.run_cli("create", *args)
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return self._parse_object(result.stdout)

    @staticmethod
    def _parse_object(stdout):
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        if not isinstance(value, dict) or stripped[end:].strip() != "":
            raise AssertionError(f"标准输出不是唯一 JSON 对象: {stdout!r}")
        return value

    def ticket_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def assert_storage_unchanged(self):
        """失败查询不新增工单，也不改变已有记录。"""
        self.assertEqual(
            [list(row) for row in self.ticket_rows()], self.expected_rows
        )

    # ---------- 编号格式错误 ----------

    def test_show_rejects_non_positive_integer_ids(self):
        for bad_id in ("abc", "0", "-1"):
            with self.subTest(bad_id=bad_id):
                result = self.run_cli("show", bad_id)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                # 错误只写标准错误，仅为提示语加末尾换行
                self.assertEqual(result.stderr, "工单编号必须为正整数\n")
                self.assert_storage_unchanged()

    # ---------- 编号不存在 ----------

    def test_show_reports_missing_ticket(self):
        result = self.run_cli("show", "999")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")
        self.assert_storage_unchanged()

    # ---------- 参数用法错误 ----------

    def test_show_usage_errors_exit_with_code_2(self):
        for argv in (
            ("show",),
            ("show", str(self.first["id"]), "--unknown"),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                # 标准错误包含用法提示即可，不限定其中的路径文字
                self.assertIn("usage", result.stderr.lower())
                self.assert_storage_unchanged()


if __name__ == "__main__":
    unittest.main()
