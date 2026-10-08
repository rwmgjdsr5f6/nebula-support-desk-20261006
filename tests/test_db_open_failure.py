"""数据库文件无法打开时的命令行结果回归测试。

通过真实命令行入口（python main.py）核对：当 --db 指向的 SQLite 文件
无法打开时（父目录不存在，或 --db 指向一个已存在的目录），任一子命令
都以退出码 1 结束，标准输出为空，标准错误恰好是“数据库无法打开”加
一个换行，不输出异常堆栈或底层错误详情；失败后不创建缺失的父目录、
不生成占位文件、不改动已有目录内容。参数本身不完整或含未知选项时，
仍优先按用法错误退出 2，即使同时指定了打不开的路径也不改报。

另核对目标文件可以正常打开时行为不变：新库照常创建、已有库照常复用，
成功 JSON 与业务错误语义不受影响。

每个用例使用独立临时目录，不依赖平台特定权限设置，
不读取或改动工作目录下的默认库 tickets.sqlite。

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

# 固定错误文本：恰好一行，无堆栈、无底层错误详情
OPEN_ERROR_STDERR = "数据库无法打开\n"

TICKET_A = {"title": "登录失败", "description": "重置密码后仍无法登录"}


class DatabaseOpenFailureTestCase(unittest.TestCase):
    """每个用例使用独立临时目录；missing-parent 目录在测试全程不存在。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.dir = Path(self._tmpdir.name)
        # 父目录不存在的数据库路径
        self.missing_parent = self.dir / "missing-parent"
        self.db_under_missing_parent = str(self.missing_parent / "demo.sqlite")
        # 一个已存在的空目录，作为 --db 的值
        self.existing_dir = self.dir / "existing-dir"
        self.existing_dir.mkdir()

    # ---------- 辅助方法 ----------

    def run_cli(self, *args):
        """以真实命令行入口运行 main.py，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, str(MAIN_PY), *args],
            capture_output=True,
            text=True,
        )

    def assert_open_failure(self, result):
        """核对数据库无法打开时的固定结果：退出码 1、空标准输出、固定错误行。"""
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, OPEN_ERROR_STDERR)

    # ---------- 父目录不存在 ----------

    def test_missing_parent_list_fails_with_fixed_error(self):
        result = self.run_cli(
            "--db", self.db_under_missing_parent, "list"
        )
        self.assert_open_failure(result)
        # 不自动创建缺失的父目录，也不生成占位数据库文件
        self.assertFalse(self.missing_parent.exists())

    def test_missing_parent_other_subcommands_fail_the_same_way(self):
        # 任一现有子命令在数据库无法打开时结果一致
        for args in (
            ("create", "--title", TICKET_A["title"]),
            ("show", "1"),
            ("close", "1"),
            ("reopen", "1"),
            ("add-note", "1", "--text", "已记录"),
            ("notes", "1"),
            ("history", "1"),
            ("set-draft", "1", "--text", "草稿"),
            ("draft", "1"),
            ("clear-draft", "1"),
            ("summary", "1"),
        ):
            with self.subTest(args=args):
                result = self.run_cli(
                    "--db", self.db_under_missing_parent, *args
                )
                self.assert_open_failure(result)
        self.assertFalse(self.missing_parent.exists())

    # ---------- --db 指向已存在的目录 ----------

    def test_db_pointing_to_directory_fails_with_fixed_error(self):
        result = self.run_cli("--db", str(self.existing_dir), "list")
        self.assert_open_failure(result)
        # 已有目录保持原样：仍是目录且内部没有新增任何文件
        self.assertTrue(self.existing_dir.is_dir())
        self.assertEqual(list(self.existing_dir.iterdir()), [])

    def test_db_pointing_to_directory_other_subcommands_fail_the_same_way(self):
        for args in (
            ("create", "--title", TICKET_A["title"]),
            ("show", "1"),
            ("notes", "1"),
            ("summary", "1"),
        ):
            with self.subTest(args=args):
                result = self.run_cli("--db", str(self.existing_dir), *args)
                self.assert_open_failure(result)
        self.assertTrue(self.existing_dir.is_dir())
        self.assertEqual(list(self.existing_dir.iterdir()), [])

    # ---------- 用法错误优先于数据库打开失败 ----------

    def test_usage_error_takes_priority_over_unopenable_db(self):
        # 缺少子命令：退出码 2，即使 --db 指向打不开的路径
        result = self.run_cli("--db", self.db_under_missing_parent)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assertNotIn("数据库无法打开", result.stderr)

    def test_unknown_option_takes_priority_over_unopenable_db(self):
        result = self.run_cli(
            "--db", self.db_under_missing_parent, "list", "--no-such-option"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assertNotIn("数据库无法打开", result.stderr)

    def test_invalid_option_value_takes_priority_over_unopenable_db(self):
        # --limit 0 为非法值：按用法错误退出 2，不报数据库无法打开
        result = self.run_cli(
            "--db", self.db_under_missing_parent, "list", "--limit", "0"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr.lower())
        self.assertNotIn("数据库无法打开", result.stderr)
        # 用法错误同样不触发父目录创建
        self.assertFalse(self.missing_parent.exists())

    # ---------- 正常打开时行为不变 ----------

    def test_openable_db_still_created_and_reused(self):
        db_path = str(self.dir / "normal.sqlite")
        result = self.run_cli(
            "--db", db_path,
            "create",
            "--title", TICKET_A["title"],
            "--description", TICKET_A["description"],
        )
        self.assertEqual(result.returncode, 0, f"create 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        created = json.loads(result.stdout)
        self.assertEqual(created["title"], TICKET_A["title"])
        self.assertTrue((self.dir / "normal.sqlite").is_file())

        # 再次打开同一文件可复用已有数据
        result = self.run_cli("--db", db_path, "list")
        self.assertEqual(result.returncode, 0, f"list 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        tickets = json.loads(result.stdout)
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["id"], created["id"])

    def test_business_error_on_openable_db_unchanged(self):
        # 可正常打开的库上，业务错误语义不变（如工单不存在）
        db_path = str(self.dir / "normal.sqlite")
        result = self.run_cli("--db", db_path, "show", "999")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")


if __name__ == "__main__":
    unittest.main()
