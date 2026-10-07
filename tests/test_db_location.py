"""数据库位置选择（默认 tickets.sqlite 与 --db）的回归测试。

通过真实命令行入口（python main.py）在两个不同于源码目录的临时工作目录中
核对：不传 --db 时使用当前工作目录下的 tickets.sqlite，--db 指定的库彼此独立，
再次调用能读回同一文件中的工单。测试结束后清理临时数据，
不读取或改动源码目录下的默认库。

运行方式（项目根目录）：
    python -m unittest discover -s tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = PROJECT_ROOT / "main.py"
DEFAULT_DB_NAME = "tickets.sqlite"
CUSTOM_DB_NAME = "custom.sqlite"

TICKET_A = {"id": 1, "title": "登录失败", "description": "重置密码后仍无法登录", "status": "open"}
TICKET_B = {"id": 1, "title": "打印异常", "description": "", "status": "open"}
TICKET_C = {"id": 1, "title": "导出失败", "description": "", "status": "open"}


class DatabaseLocationTestCase(unittest.TestCase):
    """每个用例使用两个独立的临时工作目录，均不同于源码目录。"""

    def setUp(self):
        self._tmpdir_a = tempfile.TemporaryDirectory()
        self._tmpdir_b = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir_a.cleanup)
        self.addCleanup(self._tmpdir_b.cleanup)
        self.dir_a = Path(self._tmpdir_a.name)
        self.dir_b = Path(self._tmpdir_b.name)
        # 记录源码目录默认库的既有状态，用于断言测试不触碰真实工作目录的数据
        self._root_db_existed = (PROJECT_ROOT / DEFAULT_DB_NAME).exists()

    # ---------- 辅助方法 ----------

    def run_cli(self, cwd, *args):
        """在指定工作目录中以真实命令行入口运行 main.py。"""
        return subprocess.run(
            [sys.executable, str(MAIN_PY), *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )

    def assert_ticket_result(self, result, expected):
        """核对成功调用：退出码 0、标准错误为空、标准输出仅为一个工单 JSON 对象。

        id 为整数，title/description/status 为字符串；按解析后的字典比较，
        不依赖键序。
        """
        self.assertEqual(result.returncode, 0, f"命令失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        ticket = json.loads(result.stdout)
        self.assertEqual(set(ticket), {"id", "title", "description", "status"})
        self.assertIsInstance(ticket["id"], int)
        for key in ("title", "description", "status"):
            self.assertIsInstance(ticket[key], str)
        self.assertEqual(ticket, expected)
        return ticket

    def create_in_dir_a(self):
        """在目录 A 用默认库创建“登录失败”工单并核对结果。"""
        result = self.run_cli(
            self.dir_a,
            "create",
            "--title", TICKET_A["title"],
            "--description", TICKET_A["description"],
        )
        self.assert_ticket_result(result, TICKET_A)

    def create_in_dir_b(self):
        """在目录 B 用默认库创建“打印异常”工单（无描述）并核对结果。"""
        result = self.run_cli(self.dir_b, "create", "--title", TICKET_B["title"])
        self.assert_ticket_result(result, TICKET_B)

    def show_default(self, cwd, expected):
        """在指定目录不传 --db 查看编号 1，应读回该目录默认库中的工单。"""
        result = self.run_cli(cwd, "show", "1")
        return self.assert_ticket_result(result, expected)

    def assert_root_db_untouched(self):
        """源码目录下的默认库不因测试被创建或改动。"""
        self.assertEqual(
            (PROJECT_ROOT / DEFAULT_DB_NAME).exists(), self._root_db_existed
        )

    # ---------- 不传 --db：默认使用当前工作目录下的 tickets.sqlite ----------

    def test_default_db_created_in_cwd_and_show_reads_back(self):
        self.create_in_dir_a()

        # 默认库生成在执行命令的临时目录中，而不是源码目录
        self.assertTrue((self.dir_a / DEFAULT_DB_NAME).is_file())
        self.assertFalse((self.dir_b / DEFAULT_DB_NAME).exists())
        self.assert_root_db_untouched()

        # 独立调用 show 1 读回同一文件中的同一工单
        self.assertEqual(self.show_default(self.dir_a, TICKET_A), TICKET_A)

    def test_default_dbs_in_two_dirs_are_independent(self):
        self.create_in_dir_a()
        self.create_in_dir_b()

        # 两份默认库各自编号从 1 开始，内容互不覆盖
        self.assertTrue((self.dir_a / DEFAULT_DB_NAME).is_file())
        self.assertTrue((self.dir_b / DEFAULT_DB_NAME).is_file())
        self.show_default(self.dir_a, TICKET_A)
        self.show_default(self.dir_b, TICKET_B)

        # 切换工作目录后再次查看，原目录的记录不消失、也不读到对方的工单
        self.show_default(self.dir_b, TICKET_B)
        self.show_default(self.dir_a, TICKET_A)
        self.assert_root_db_untouched()

    # ---------- --db：显式库与默认库彼此独立 ----------

    def test_custom_db_create_and_show_by_absolute_path(self):
        self.create_in_dir_a()
        self.create_in_dir_b()

        # 目录 A 保留已有默认工单，再创建另一份自定义库
        result = self.run_cli(
            self.dir_a, "--db", CUSTOM_DB_NAME, "create", "--title", TICKET_C["title"]
        )
        self.assert_ticket_result(result, TICKET_C)
        self.assertTrue((self.dir_a / CUSTOM_DB_NAME).is_file())

        # 从目录 B 以绝对路径查看自定义库，读到“导出失败”
        custom_db_path = str(self.dir_a / CUSTOM_DB_NAME)
        result = self.run_cli(self.dir_b, "--db", custom_db_path, "show", "1")
        self.assert_ticket_result(result, TICKET_C)

        # 不传 --db 时仍分别返回各目录默认库中的原有工单
        self.show_default(self.dir_a, TICKET_A)
        self.show_default(self.dir_b, TICKET_B)
        self.assert_root_db_untouched()

    def test_show_missing_ticket_on_explicit_db(self):
        self.create_in_dir_a()
        result = self.run_cli(
            self.dir_a, "--db", CUSTOM_DB_NAME, "create", "--title", TICKET_C["title"]
        )
        self.assert_ticket_result(result, TICKET_C)

        # 对显式库查看不存在的编号：退出码 1，标准输出为空，
        # 标准错误仅为“工单不存在”及换行
        custom_db_path = str(self.dir_a / CUSTOM_DB_NAME)
        result = self.run_cli(self.dir_b, "--db", custom_db_path, "show", "999")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")

        # 失败的查看不改动已有记录，编号 1 内容不变
        result = self.run_cli(self.dir_b, "--db", custom_db_path, "show", "1")
        self.assert_ticket_result(result, TICKET_C)


if __name__ == "__main__":
    unittest.main()
