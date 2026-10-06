"""create 后按编号 show 的回归测试。

通过真实命令行入口（python main.py）分两次独立进程调用完成创建与查询，
两次调用共用同一个位于独立临时目录中的数据库（--db 指定），
测试结束后临时目录自动清理，不读取或改动用户已有数据库。

覆盖内容：
    - 空库中创建标题带首尾空格的工单，标题被去除首尾空白，
      描述的首尾空格原样保留，状态为 open，编号为正整数；
    - 用创建返回的编号 show，结果与创建结果一致，重复查看不改动记录；
    - 省略描述创建第二条工单，得到不同编号与空字符串描述，
      第一条工单仍可按原编号查看；
    - 省略标题、空字符串标题、纯空白标题均以退出码 1 拒绝；
    - show 接收非正整数编号、不存在的编号时分别以退出码 1 报错；
    - show 缺少编号或追加未知选项时以退出码 2 给出用法提示；
    - 所有失败均不新增占位工单、不改变已有记录。

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

# 合成样例：标题与描述均带首尾空格，用于核对去除/保留规则
PADDED_TITLE = "  登录失败  "
PADDED_DESCRIPTION = "  重置密码后仍无法登录  "
STRIPPED_TITLE = "登录失败"

SECOND_TITLE = "无法导出报表"

EXPECTED_FIELDS = {"id", "title", "description", "status"}


class CreateThenShowTestCase(unittest.TestCase):
    """每个用例使用独立临时目录与独立数据库，互不影响。"""

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

    def parse_single_json_object(self, stdout):
        """标准输出必须恰好是一个 JSON 对象，不允许前后夹带说明文字。

        通过 json 解析比较，不依赖对象键序或格式空格。
        """
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        self.assertIsInstance(value, dict)
        self.assertEqual(
            stripped[end:].strip(),
            "",
            f"JSON 对象后存在额外输出: {stdout!r}",
        )
        return value

    def assertTicketShape(self, ticket):
        """仅含 id/title/description/status；id 为整数，其余为字符串。"""
        self.assertIsInstance(ticket, dict)
        self.assertEqual(set(ticket.keys()), EXPECTED_FIELDS)
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(ticket["id"]), int)
        for key in ("title", "description", "status"):
            self.assertIs(type(ticket[key]), str)

    def create_ticket(self, *args):
        """执行 create 并断言成功，返回解析后的工单对象。"""
        result = self.run_cli("create", *args)
        self.assertEqual(result.returncode, 0, f"create 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        ticket = self.parse_single_json_object(result.stdout)
        self.assertTicketShape(ticket)
        return ticket

    def show_ticket(self, ticket_id):
        """执行 show 并断言成功，返回解析后的工单对象。"""
        result = self.run_cli("show", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"show 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        ticket = self.parse_single_json_object(result.stdout)
        self.assertTicketShape(ticket)
        return ticket

    def stored_rows(self):
        """直接读取数据库，返回按编号升序的存量记录列表。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def assert_storage_matches(self, expected_tickets):
        """存量记录的数量、编号与各字段内容与期望完全一致。"""
        self.assertEqual(
            [list(row) for row in self.stored_rows()],
            [
                [t["id"], t["title"], t["description"], t["status"]]
                for t in expected_tickets
            ],
        )

    # ---------- 创建后按编号重新查看 ----------

    def test_create_then_show_round_trip(self):
        # 空库中创建：标题首尾空格被去除，描述首尾空格保留
        result = self.run_cli(
            "create", "--title", PADDED_TITLE, "--description", PADDED_DESCRIPTION
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        # 中文正文在标准输出中直接可读，而非转义序列
        self.assertIn(STRIPPED_TITLE, result.stdout)
        self.assertIn(PADDED_DESCRIPTION.strip(), result.stdout)

        created = self.parse_single_json_object(result.stdout)
        self.assertTicketShape(created)
        self.assertGreater(created["id"], 0)
        self.assertEqual(created["title"], STRIPPED_TITLE)
        self.assertEqual(created["description"], PADDED_DESCRIPTION)
        self.assertEqual(created["status"], "open")

        # 独立进程用返回的编号 show，结果与创建结果一致
        shown = self.show_ticket(created["id"])
        self.assertEqual(shown, created)

        # 重复查看结果不变，且不改动存量记录
        self.assertEqual(self.show_ticket(created["id"]), shown)
        self.assert_storage_matches([created])

    def test_create_without_description_gets_distinct_id_and_empty_description(self):
        first = self.create_ticket(
            "--title", PADDED_TITLE, "--description", PADDED_DESCRIPTION
        )

        # 省略描述创建第二条工单
        result = self.run_cli("create", "--title", SECOND_TITLE)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        second = self.parse_single_json_object(result.stdout)
        self.assertTicketShape(second)
        self.assertNotEqual(second["id"], first["id"])
        self.assertGreater(second["id"], 0)
        self.assertEqual(second["title"], SECOND_TITLE)
        self.assertEqual(second["description"], "")
        self.assertEqual(second["status"], "open")

        # 第一条工单仍可按原编号查看，内容不变
        self.assertEqual(self.show_ticket(first["id"]), first)
        self.assertEqual(self.show_ticket(second["id"]), second)
        self.assert_storage_matches([first, second])

    # ---------- 拒绝创建 ----------

    def test_create_rejects_missing_or_blank_title(self):
        # 先建一条合法工单，用于验证失败不会改动已有记录
        existing = self.create_ticket("--title", STRIPPED_TITLE)

        for argv in (
            ("create",),
            ("create", "--title", ""),
            ("create", "--title", "   "),
            ("create", "--title", " \t ", "--description", "任意描述"),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "标题不能为空\n")
                # 不新增占位工单，已有记录保持不变
                self.assert_storage_matches([existing])
                self.assertEqual(self.show_ticket(existing["id"]), existing)

    # ---------- 查询失败 ----------

    def test_show_rejects_non_positive_integer_ids(self):
        existing = self.create_ticket("--title", STRIPPED_TITLE)

        for bad_id in ("abc", "0", "-1"):
            with self.subTest(bad_id=bad_id):
                result = self.run_cli("show", bad_id)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单编号必须为正整数\n")
                # 失败不改动已有记录
                self.assert_storage_matches([existing])

    def test_show_reports_missing_ticket(self):
        existing = self.create_ticket("--title", STRIPPED_TITLE)

        result = self.run_cli("show", "999")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")
        # 失败不改动已有记录
        self.assert_storage_matches([existing])
        self.assertEqual(self.show_ticket(existing["id"]), existing)

    # ---------- 参数用法错误 ----------

    def test_show_usage_errors_exit_with_code_2(self):
        existing = self.create_ticket("--title", STRIPPED_TITLE)

        for argv in (
            ("show",),
            ("show", str(existing["id"]), "--unknown"),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                # 包含用法提示，不限定其中的路径文字
                self.assertIn("usage", result.stderr.lower())
                # 参数错误不改动已有记录
                self.assert_storage_matches([existing])


if __name__ == "__main__":
    unittest.main()
