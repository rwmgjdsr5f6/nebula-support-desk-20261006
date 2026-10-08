"""数据库文件无法打开时命令行结果的回归测试。

通过真实命令行入口（python main.py）在独立临时工作目录中核对两种路径失败：

- ``--db`` 指向的文件其父目录不存在；
- ``--db`` 直接指向一个已存在的目录。

两者都应以退出码 1 结束、标准输出为空、标准错误恰为“数据库无法打开”加一个
换行，不输出异常堆栈或底层错误；失败后不创建缺失的父目录、不生成任何文件、
不回退到默认库。参数本身不完整或含未知选项时仍优先按用法错误退出 2。

所有用例仅使用临时目录，不依赖平台特定的权限设置，也不读取或改动源码目录
下的默认库。

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
OPEN_ERROR = "数据库无法打开\n"

# 每个现有子命令的一组语法合法参数：参数解析通过后命令才会尝试打开数据库。
# 工单编号是否存在无关紧要——数据库打不开时不得执行任何查询或处理。
VALID_SUBCOMMANDS = (
    ("list",),
    ("create", "--title", "样例标题"),
    ("show", "1"),
    ("close", "1"),
    ("reopen", "1"),
    ("add-note", "1", "--text", "样例备注"),
    ("notes", "1"),
    ("history", "1"),
    ("set-draft", "1", "--text", "样例草稿"),
    ("draft", "1"),
    ("clear-draft", "1"),
    ("summary", "1"),
)


class DatabaseOpenFailureTestCase(unittest.TestCase):
    """每个用例使用独立的空临时工作目录，失败路径均位于该目录之内。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cwd = Path(self._tmpdir.name)

        # 情形一：父目录 missing-parent 始终不存在
        self.missing_parent = self.cwd / "missing-parent"
        self.missing_parent_db = self.missing_parent / "demo.sqlite"

        # 情形二：--db 直接指向一个已存在的目录
        self.existing_dir = self.cwd / "an-existing-dir"
        self.existing_dir.mkdir()

    # ---------- 辅助方法 ----------

    def run_cli(self, *args):
        """以真实命令行入口在空临时工作目录中运行 main.py。"""
        return subprocess.run(
            [sys.executable, str(MAIN_PY), *args],
            cwd=str(self.cwd),
            capture_output=True,
            text=True,
        )

    def assert_database_open_failure(self, result):
        """核对数据库无法打开的固定结果：退出码 1、空 stdout、单行固定 stderr。"""
        self.assertEqual(result.returncode, 1, f"stderr: {result.stderr!r}")
        self.assertEqual(result.stdout, "")
        # 恰好一条固定错误：不含异常堆栈、SQLite 错误类名或底层详情
        self.assertEqual(result.stderr, OPEN_ERROR)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("sqlite", result.stderr.lower())

    def assert_no_fallback_artifacts(self):
        """失败后不得创建缺失父目录，也不得在工作目录生成默认库或其他文件。"""
        self.assertFalse(
            self.missing_parent.exists(), "缺失的父目录被自动创建了"
        )
        self.assertFalse(
            (self.cwd / DEFAULT_DB_NAME).exists(), "失败时回退创建了默认库"
        )

    # ---------- 验收样例：父目录不存在 ----------

    def test_missing_parent_directory_with_list(self):
        # 对应验收命令：python main.py --db missing-parent/demo.sqlite list
        result = self.run_cli("--db", str(self.missing_parent_db), "list")
        self.assert_database_open_failure(result)
        self.assert_no_fallback_artifacts()

    def test_missing_parent_directory_for_every_subcommand(self):
        for command_args in VALID_SUBCOMMANDS:
            with self.subTest(command=command_args):
                result = self.run_cli(
                    "--db", str(self.missing_parent_db), *command_args
                )
                self.assert_database_open_failure(result)
                # 失败后不执行查询或处理：缺失父目录仍不存在，也无占位记录
                self.assert_no_fallback_artifacts()

    # ---------- 验收样例：--db 指向已存在的目录 ----------

    def test_db_path_is_existing_directory_with_list(self):
        result = self.run_cli("--db", str(self.existing_dir), "list")
        self.assert_database_open_failure(result)
        # 目录依旧只是个空目录，没有在其中生成任何文件
        self.assertEqual(list(self.existing_dir.iterdir()), [])
        self.assertFalse((self.cwd / DEFAULT_DB_NAME).exists())

    def test_existing_directory_for_every_subcommand(self):
        for command_args in VALID_SUBCOMMANDS:
            with self.subTest(command=command_args):
                result = self.run_cli(
                    "--db", str(self.existing_dir), *command_args
                )
                self.assert_database_open_failure(result)
                self.assertEqual(list(self.existing_dir.iterdir()), [])
                self.assertFalse((self.cwd / DEFAULT_DB_NAME).exists())

    # ---------- 参数错误优先于数据库打不开 ----------

    def test_unknown_option_exits_2_even_when_db_unopenable(self):
        for db_path in (str(self.missing_parent_db), str(self.existing_dir)):
            with self.subTest(db=db_path):
                result = self.run_cli("--db", db_path, "list", "--unknown-option")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage", result.stderr.lower())
                # 不能改报数据库无法打开
                self.assertNotIn("数据库无法打开", result.stderr)
                self.assert_no_fallback_artifacts()

    def test_missing_subcommand_exits_2_even_when_db_unopenable(self):
        for db_path in (str(self.missing_parent_db), str(self.existing_dir)):
            with self.subTest(db=db_path):
                result = self.run_cli("--db", db_path)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage", result.stderr.lower())
                self.assertNotIn("数据库无法打开", result.stderr)
                self.assert_no_fallback_artifacts()

    # ---------- 失败不波及已有数据库、不回退默认库 ----------

    def test_failure_does_not_touch_existing_default_db(self):
        # 先在工作目录的默认库中准备一条工单
        created = self.run_cli("create", "--title", "默认库工单")
        self.assertEqual(created.returncode, 0, created.stderr)
        default_db = self.cwd / DEFAULT_DB_NAME
        self.assertTrue(default_db.is_file())

        # 指向打不开的 --db 失败后，不允许改用默认库：默认库工单原样可读
        for bad_db in (str(self.missing_parent_db), str(self.existing_dir)):
            with self.subTest(db=bad_db):
                result = self.run_cli("--db", bad_db, "list")
                self.assert_database_open_failure(result)

                shown = self.run_cli("show", "1")
                self.assertEqual(shown.returncode, 0, shown.stderr)
                self.assertEqual(
                    json.loads(shown.stdout),
                    {
                        "id": 1,
                        "title": "默认库工单",
                        "description": "",
                        "status": "open",
                    },
                )
        # 失败不回退到默认库：缺失父目录仍不存在；默认库始终只有预置的一条工单
        self.assertFalse(self.missing_parent.exists())
        listed = self.run_cli("list")
        self.assertEqual(len(json.loads(listed.stdout)), 1)

    # ---------- 目标文件可正常打开时语义不变 ----------

    def test_openable_db_is_created_and_reused_without_rebuild(self):
        db_path = self.cwd / "demo.sqlite"

        first = self.run_cli(
            "--db", str(db_path), "create", "--title", "显式库工单"
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")
        self.assertTrue(db_path.is_file())

        # 再次调用复用同一文件：旧数据不重建，编号接续而非从 1 重来
        second = self.run_cli(
            "--db", str(db_path), "create", "--title", "第二张工单"
        )
        self.assertEqual(json.loads(second.stdout)["id"], 2)

        listed = self.run_cli("--db", str(db_path), "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(
            [ticket["title"] for ticket in json.loads(listed.stdout)],
            ["显式库工单", "第二张工单"],
        )

    def test_default_db_location_unchanged(self):
        # 不传 --db 时仍在当前工作目录创建并使用 tickets.sqlite
        result = self.run_cli("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "[]\n")
        self.assertTrue((self.cwd / DEFAULT_DB_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
