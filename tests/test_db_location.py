"""数据库位置选择（默认 tickets.sqlite 与 --db）的回归测试。

全部用例通过真实命令行入口（python main.py）观察结果，并在两个与源码
目录不同的临时工作目录之间切换工作目录，验证：

- 不传 --db 时始终使用“当前工作目录/tickets.sqlite”，两个目录各有独立默认库，
  切换目录后原目录记录不消失，两份默认库互不覆盖；
- --db 指定的库与默认库并存，相对路径落在当前工作目录，
  也可用绝对路径从另一个工作目录读取；
- 对显式库查询不存在的编号时报错，且不影响显式库与默认库中的已有工单。

所有成功调用均核对退出码 0、标准错误为空、标准输出为唯一一个
含 id/title/description/status 的 JSON 对象（id 为整数，其余为字符串）。
测试结束后自动清理临时目录，不读取或修改项目目录下的数据库文件。

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
TESTS_DIR = Path(__file__).resolve().parent
MAIN_PY = PROJECT_ROOT / "main.py"

DEFAULT_DB_NAME = "tickets.sqlite"
CUSTOM_DB_NAME = "custom.sqlite"

FIRST_TITLE = "登录失败"
FIRST_DESCRIPTION = "重置密码后仍无法登录"
SECOND_TITLE = "打印异常"
THIRD_TITLE = "导出失败"

TICKET_KEYS = {"id", "title", "description", "status"}


def parse_single_json_object(stdout):
    """标准输出必须恰好是一个 JSON 对象，不允许前后夹带说明文字。"""
    decoder = json.JSONDecoder()
    stripped = stdout.lstrip()
    value, end = decoder.raw_decode(stripped)
    if not isinstance(value, dict) or stripped[end:].strip() != "":
        raise AssertionError(f"标准输出不是唯一 JSON 对象: {stdout!r}")
    return value


def snapshot_db_files(directory):
    """记录目录下各 SQLite 文件的当前字节内容，用于事后核对未被改动。"""
    snapshot = {}
    for path in directory.glob("*.sqlite*"):
        if path.is_file():
            snapshot[path] = path.read_bytes()
    return snapshot


class DatabaseLocationSelectionTestCase(unittest.TestCase):
    """在两个临时工作目录中核对默认库选择与 --db 选择。"""

    def setUp(self):
        # 两个相互独立、且都不同于源码目录的临时工作目录
        self._tmp_a = tempfile.TemporaryDirectory()
        self._tmp_b = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_a.cleanup)
        self.addCleanup(self._tmp_b.cleanup)
        self.dir_a = Path(self._tmp_a.name)
        self.dir_b = Path(self._tmp_b.name)
        self.assertNotEqual(self.dir_a, PROJECT_ROOT)
        self.assertNotEqual(self.dir_b, PROJECT_ROOT)

        self.default_a = self.dir_a / DEFAULT_DB_NAME
        self.default_b = self.dir_b / DEFAULT_DB_NAME
        self.custom_a = self.dir_a / CUSTOM_DB_NAME

        # 快照项目目录与 tests 目录中既有的数据库文件，
        # 用例结束后核对测试从未在真实工作目录中创建或修改数据库
        self._db_snapshot = {}
        for directory in (PROJECT_ROOT, TESTS_DIR):
            self._db_snapshot.update(snapshot_db_files(directory))
        self.addCleanup(self._assert_project_directories_untouched)

    # ---------- 辅助方法 ----------

    def run_cli(self, cwd, *args, db=None):
        """在指定工作目录中以真实命令行入口运行 main.py，返回 CompletedProcess。

        db 不为 None 时在子命令前插入全局选项 --db（相对路径由该工作目录解析）。
        """
        command = [sys.executable, str(MAIN_PY)]
        if db is not None:
            command.extend(["--db", db])
        command.extend(args)
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=str(cwd),
        )

    @staticmethod
    def assertTicketShape(ticket):
        """仅含 id/title/description/status；id 为整数，其余为字符串。"""
        if not isinstance(ticket, dict):
            raise AssertionError(f"工单不是 JSON 对象: {ticket!r}")
        if set(ticket.keys()) != TICKET_KEYS:
            raise AssertionError(f"工单字段不匹配: {set(ticket.keys())}")
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        if type(ticket["id"]) is not int:
            raise AssertionError(f"id 不是整数: {ticket['id']!r}")
        for key in ("title", "description", "status"):
            if type(ticket[key]) is not str:
                raise AssertionError(f"{key} 不是字符串: {ticket[key]!r}")

    def assert_success_ticket(self, result, expected):
        """核对成功调用：退出码 0、标准错误为空、标准输出为期望工单对象。"""
        self.assertEqual(result.returncode, 0, f"命令失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        ticket = parse_single_json_object(result.stdout)
        self.assertTicketShape(ticket)
        # 解析后按字段比较，不依赖 JSON 键顺序
        self.assertEqual(ticket, expected)
        return ticket

    def _assert_project_directories_untouched(self):
        """项目根目录与 tests 目录下的数据库文件集合与内容必须保持用例前状态。"""
        current = {}
        for directory in (PROJECT_ROOT, TESTS_DIR):
            current.update(snapshot_db_files(directory))
        self.assertEqual(
            set(current),
            set(self._db_snapshot),
            "测试在项目目录或 tests 目录中创建或删除了数据库文件",
        )
        for path, content in current.items():
            self.assertEqual(content, self._db_snapshot[path])

    # ---------- 不传 --db：默认库跟随当前工作目录 ----------

    def test_default_db_follows_cwd_and_two_directories_stay_isolated(self):
        first = {
            "id": 1,
            "title": FIRST_TITLE,
            "description": FIRST_DESCRIPTION,
            "status": "open",
        }

        # 第一个临时目录：不传 --db 创建，应在该目录生成 tickets.sqlite
        result = self.run_cli(
            self.dir_a,
            "create",
            "--title",
            FIRST_TITLE,
            "--description",
            FIRST_DESCRIPTION,
        )
        self.assert_success_ticket(result, first)
        self.assertTrue(self.default_a.is_file())
        # 此时第二个目录不应被动出现默认库（即没有使用源码目录或其他位置）
        self.assertFalse(self.default_b.exists())

        # 随后独立调用 show 1，应从同一文件读回相同对象
        self.assert_success_ticket(self.run_cli(self.dir_a, "show", "1"), first)

        # 第二个临时目录：只给标题；新库编号同样从 1 开始，描述缺省为空字符串
        second = {
            "id": 1,
            "title": SECOND_TITLE,
            "description": "",
            "status": "open",
        }
        result = self.run_cli(self.dir_b, "create", "--title", SECOND_TITLE)
        self.assert_success_ticket(result, second)
        self.assertTrue(self.default_b.is_file())
        self.assertNotEqual(
            self.default_a.resolve(), self.default_b.resolve()
        )

        # 两个目录再次分别查看，只能读到自己的工单
        self.assert_success_ticket(self.run_cli(self.dir_a, "show", "1"), first)
        self.assert_success_ticket(self.run_cli(self.dir_b, "show", "1"), second)

        # 反复切换工作目录：原目录记录不消失，两份默认库互不覆盖
        self.assert_success_ticket(self.run_cli(self.dir_b, "show", "1"), second)
        self.assert_success_ticket(self.run_cli(self.dir_a, "show", "1"), first)
        self.assert_success_ticket(self.run_cli(self.dir_a, "show", "1"), first)
        self.assert_success_ticket(self.run_cli(self.dir_b, "show", "1"), second)

    # ---------- 显式 --db 与默认库并存，可跨目录以绝对路径读取 ----------

    def test_explicit_db_coexists_with_default_and_is_readable_by_absolute_path(self):
        # 两个临时目录各自先保留一条默认工单
        first = {
            "id": 1,
            "title": FIRST_TITLE,
            "description": FIRST_DESCRIPTION,
            "status": "open",
        }
        second = {
            "id": 1,
            "title": SECOND_TITLE,
            "description": "",
            "status": "open",
        }
        self.assert_success_ticket(
            self.run_cli(
                self.dir_a,
                "create",
                "--title",
                FIRST_TITLE,
                "--description",
                FIRST_DESCRIPTION,
            ),
            first,
        )
        self.assert_success_ticket(
            self.run_cli(self.dir_b, "create", "--title", SECOND_TITLE),
            second,
        )

        # 第一个目录保留已有默认工单，另用 --db custom.sqlite 创建独立库
        custom = {
            "id": 1,
            "title": THIRD_TITLE,
            "description": "",
            "status": "open",
        }
        result = self.run_cli(
            self.dir_a, "create", "--title", THIRD_TITLE, db=CUSTOM_DB_NAME
        )
        self.assert_success_ticket(result, custom)
        # 显式库是新文件，默认库仍然保留，二者互不影响
        self.assertTrue(self.custom_a.is_file())
        self.assertTrue(self.default_a.is_file())
        self.assertNotEqual(self.custom_a.resolve(), self.default_a.resolve())

        # 从第二个临时目录以 custom.sqlite 的绝对路径查看，应读到导出失败
        self.assert_success_ticket(
            self.run_cli(self.dir_b, "show", "1", db=str(self.custom_a)),
            custom,
        )

        # 不传 --db 时仍分别返回各目录原有的默认工单
        self.assert_success_ticket(self.run_cli(self.dir_a, "show", "1"), first)
        self.assert_success_ticket(self.run_cli(self.dir_b, "show", "1"), second)

        # 对显式库调用 show 999：退出 1、标准输出为空，
        # 标准错误仅为“工单不存在”及换行
        missing = self.run_cli(
            self.dir_b, "show", "999", db=str(self.custom_a)
        )
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(missing.stdout, "")
        self.assertEqual(missing.stderr, "工单不存在\n")

        # 随后显式库编号 1 内容不变；两份默认库中的原有工单同样不变
        self.assert_success_ticket(
            self.run_cli(self.dir_b, "show", "1", db=str(self.custom_a)),
            custom,
        )
        self.assert_success_ticket(self.run_cli(self.dir_a, "show", "1"), first)
        self.assert_success_ticket(self.run_cli(self.dir_b, "show", "1"), second)


if __name__ == "__main__":
    unittest.main()
