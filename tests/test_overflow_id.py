"""过大正整数编号边界的回归测试。

通过真实命令行入口（python main.py）核对 show、close、reopen 对超出
SQLite 可保存范围（大于 9223372036854775807）的正整数编号的统一行为：
退出码 1、标准输出为空、标准错误仅为“工单不存在”加换行，不出现
异常堆栈，也不创建占位工单或改动已有工单。同时覆盖最大整数本身的
未命中结果、普通有效编号及原有非法编号结果。

每个用例使用独立临时数据库，测试结束后清理，不读取或改动工作目录下
的默认库。

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

TICKET_A = {"title": "登录失败", "description": "重置密码后仍无法登录"}
TICKET_B = {"title": "打印异常", "description": "更换网络后无法打印"}

# SQLite INTEGER 主键可保存的最大值及其之外的最小正整数
MAX_SQLITE_INT = 9223372036854775807
OVERFLOW_ID = "9223372036854775808"

# 同一过大数值的合法等价写法：带正号、前导零、首尾空白
OVERFLOW_VARIANTS = (
    OVERFLOW_ID,
    "+9223372036854775808",
    "09223372036854775808",
    "  9223372036854775808  ",
)

ID_COMMANDS = ("show", "close", "reopen")


class OverflowIdTestCase(unittest.TestCase):
    """每个用例使用独立临时数据库，预置一 open 一 closed 两条工单。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.id_a = self._create_ticket(TICKET_A["title"], TICKET_A["description"])
        self.id_b = self._create_ticket(TICKET_B["title"], TICKET_B["description"])
        close_result = self.run_cli("close", str(self.id_b))
        self.assertEqual(close_result.returncode, 0, f"准备样例失败: {close_result.stderr}")

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

    def assert_sample_tickets_untouched(self):
        """两条样例工单内容与状态不变，总数仍为 2，未出现占位工单。"""
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            [
                [self.id_a, TICKET_A["title"], TICKET_A["description"], "open"],
                [self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"],
            ],
        )

    # ---------- 过大正整数编号 ----------

    def test_overflow_id_reports_missing_ticket_for_all_id_commands(self):
        for command in ID_COMMANDS:
            for raw_id in OVERFLOW_VARIANTS:
                with self.subTest(command=command, raw_id=raw_id):
                    result = self.run_cli(command, raw_id)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    # 仅为提示语加末尾换行，不出现 OverflowError 堆栈
                    self.assertEqual(result.stderr, "工单不存在\n")
                    # 不创建占位工单，也不改动已有工单
                    self.assert_sample_tickets_untouched()

    def test_far_beyond_overflow_id_reports_missing_ticket(self):
        for command in ID_COMMANDS:
            with self.subTest(command=command):
                result = self.run_cli(command, "99999999999999999999999999")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单不存在\n")
                self.assert_sample_tickets_untouched()

    # ---------- 最大整数本身 ----------

    def test_max_sqlite_int_is_valid_format_but_missing(self):
        for command in ID_COMMANDS:
            with self.subTest(command=command):
                result = self.run_cli(command, str(MAX_SQLITE_INT))
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                # 最大整数不是格式错误，库中无记录时同样报工单不存在
                self.assertEqual(result.stderr, "工单不存在\n")
                self.assert_sample_tickets_untouched()

    # ---------- 普通有效编号不受影响 ----------

    def test_normal_ids_still_work(self):
        shown = self.run_cli("show", str(self.id_a))
        self.assertEqual(shown.returncode, 0)
        self.assertEqual(shown.stderr, "")
        self.assertEqual(
            json.loads(shown.stdout),
            {
                "id": self.id_a,
                "title": TICKET_A["title"],
                "description": TICKET_A["description"],
                "status": "open",
            },
        )

        reopened = self.run_cli("reopen", str(self.id_b))
        self.assertEqual(reopened.returncode, 0)
        self.assertEqual(json.loads(reopened.stdout)["status"], "open")

        closed = self.run_cli("close", str(self.id_a))
        self.assertEqual(closed.returncode, 0)
        self.assertEqual(json.loads(closed.stdout)["status"], "closed")

    # ---------- 原有非法编号结果保持原样 ----------

    def test_non_positive_or_unparseable_ids_still_rejected(self):
        for command in ID_COMMANDS:
            for bad_id in ("abc", "0", "-1"):
                with self.subTest(command=command, bad_id=bad_id):
                    result = self.run_cli(command, bad_id)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "工单编号必须为正整数\n")
                    self.assert_sample_tickets_untouched()


if __name__ == "__main__":
    unittest.main()
