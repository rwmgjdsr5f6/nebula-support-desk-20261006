"""按编号操作（show/close/reopen）的编号边界回归测试。

重点核对超过 SQLite 最大整数 9223372036854775807 的编号：
这类编号按既有 int() 规则可解析为正整数，但库中不可能存在对应记录，
必须按“工单不存在”处理，而不是触发未捕获的 OverflowError。

通过真实命令行入口（python main.py）核对退出码、标准输出与标准错误。
每个用例在独立临时数据库中准备两条合成工单（第一条 open、第二条 closed），
测试结束后清理，不读取工作目录中的默认库。

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

SQLITE_MAX_INT = 9223372036854775807
OVERSIZED_ID = "9223372036854775808"

TICKET_A = {"title": "登录失败", "description": "重置密码后仍无法登录"}
TICKET_B = {"title": "打印异常", "description": "更换网络后无法打印"}


class IdBoundaryTestCase(unittest.TestCase):
    """show/close/reopen 共用的编号边界：每个用例使用独立临时数据库。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.id_a = self._create_ticket(TICKET_A["title"], TICKET_A["description"])
        self.id_b = self._create_ticket(TICKET_B["title"], TICKET_B["description"])

        # 将第二条工单结案，得到一 open、一 closed 的初始状态
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

    def ticket_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def expected_rows(self):
        return [
            (self.id_a, TICKET_A["title"], TICKET_A["description"], "open"),
            (self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"),
        ]

    def assert_storage_unchanged(self):
        """失败查询不新增工单、不改变状态，也不写入占位编号。"""
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            [list(row) for row in self.expected_rows()],
        )

    def assert_not_found(self, command, raw_id):
        """超大编号按不存在处理：退出码 1，标准输出空，标准错误仅提示语。"""
        result = self.run_cli(command, raw_id)
        self.assertEqual(result.returncode, 1, f"raw_id={raw_id!r}: {result.stderr}")
        self.assertEqual(result.stdout, "")
        # 精确等值也保证了不出现异常堆栈
        self.assertEqual(result.stderr, "工单不存在\n")
        self.assert_storage_unchanged()

    # ---------- 超过 SQLite 最大整数的编号 ----------

    def test_oversized_id_reports_not_found_for_all_commands(self):
        for command in ("show", "close", "reopen"):
            with self.subTest(command=command):
                self.assert_not_found(command, OVERSIZED_ID)

    def test_oversized_id_keeps_same_semantics_with_equivalent_spellings(self):
        # 保留既有 int() 解析语义：正号、前导零、首尾空白仍解析为同一整数，
        # 过大值采用这些写法时同样得到“工单不存在”
        spellings = (
            "+9223372036854775808",
            "09223372036854775808",
            "  9223372036854775808  ",
            "\t+09223372036854775808\n",
        )
        for command in ("show", "close", "reopen"):
            for raw_id in spellings:
                with self.subTest(command=command, raw_id=raw_id):
                    self.assert_not_found(command, raw_id)

    def test_much_larger_id_reports_not_found(self):
        for command in ("show", "close", "reopen"):
            with self.subTest(command=command):
                self.assert_not_found(command, "9" * 40)

    # ---------- 最大整数本身 ----------

    def test_max_sqlite_int_is_not_a_format_error_and_misses(self):
        # 最大整数本身合法，不应被判为格式错误；库中无此记录时按不存在处理
        for raw_id in (str(SQLITE_MAX_INT), "+9223372036854775807"):
            for command in ("show", "close", "reopen"):
                with self.subTest(command=command, raw_id=raw_id):
                    self.assert_not_found(command, raw_id)

    # ---------- 普通有效编号不受影响 ----------

    def test_normal_valid_ids_still_work(self):
        # show 只读：返回既有 JSON 对象，不改变数据
        show_a = self.run_cli("show", str(self.id_a))
        self.assertEqual(show_a.returncode, 0)
        self.assertEqual(show_a.stderr, "")
        self.assertEqual(
            json.loads(show_a.stdout),
            {
                "id": self.id_a,
                "title": TICKET_A["title"],
                "description": TICKET_A["description"],
                "status": "open",
            },
        )
        self.assert_storage_unchanged()

        # close 只改变目标工单状态
        close_a = self.run_cli("close", str(self.id_a))
        self.assertEqual(close_a.returncode, 0)
        self.assertEqual(close_a.stderr, "")
        self.assertEqual(json.loads(close_a.stdout)["status"], "closed")

        # reopen 只改变目标工单状态
        reopen_a = self.run_cli("reopen", str(self.id_a))
        self.assertEqual(reopen_a.returncode, 0)
        self.assertEqual(reopen_a.stderr, "")
        self.assertEqual(json.loads(reopen_a.stdout)["status"], "open")

        # 另一条工单状态未被波及
        self.assert_storage_unchanged()

    def test_close_and_reopen_remain_idempotent(self):
        first_close = self.run_cli("close", str(self.id_a))
        self.assertEqual(first_close.returncode, 0)
        second_close = self.run_cli("close", str(self.id_a))
        self.assertEqual(second_close.returncode, 0)
        self.assertEqual(second_close.stdout, first_close.stdout)

        first_reopen = self.run_cli("reopen", str(self.id_a))
        self.assertEqual(first_reopen.returncode, 0)
        second_reopen = self.run_cli("reopen", str(self.id_a))
        self.assertEqual(second_reopen.returncode, 0)
        self.assertEqual(second_reopen.stdout, first_reopen.stdout)

        self.assertEqual(self.ticket_count(), 2)

    def ticket_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        finally:
            conn.close()

    # ---------- 原有非法编号结果 ----------

    def test_invalid_and_non_positive_ids_still_rejected(self):
        for raw_id in ("abc", "0", "-1", "-9223372036854775808", "1.5", ""):
            for command in ("show", "close", "reopen"):
                with self.subTest(command=command, raw_id=raw_id):
                    result = self.run_cli(command, raw_id)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "工单编号必须为正整数\n")
                    self.assert_storage_unchanged()


if __name__ == "__main__":
    unittest.main()
