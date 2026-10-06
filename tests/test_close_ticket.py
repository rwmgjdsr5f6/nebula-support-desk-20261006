"""close（按编号结案）命令的回归测试。

仅核对 close：create 只用于在每个用例独立的临时数据库中准备两条合成工单，
show 只用于读取结案后的工单以验证持久化结果。所有命令均通过真实命令行入口
main.py 以子进程执行，核对退出码、标准输出与标准错误。

运行方式（项目根目录）：
    python -m unittest discover -s tests
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_PY = os.path.join(PROJECT_ROOT, "main.py")

TITLE_A = "登录失败"
DESCRIPTION_A = "重置密码后仍无法登录"
TITLE_B = "打印异常"
DESCRIPTION_B = "更换网络后无法打印"


class CloseTicketTests(unittest.TestCase):
    def setUp(self):
        # 每个用例使用独立的临时数据库，结束后自动清理，不触碰用户已有数据库。
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.db_path = os.path.join(self._tempdir.name, "tickets-test.sqlite")
        self.id_a = self._create_ticket(TITLE_A, DESCRIPTION_A)
        self.id_b = self._create_ticket(TITLE_B, DESCRIPTION_B)

    # ---- 辅助方法 ----

    def _run_cli(self, *args):
        """通过真实命令行入口执行一条命令，返回完成进程。"""
        return subprocess.run(
            [sys.executable, MAIN_PY, "--db", self.db_path, *args],
            capture_output=True,
            encoding="utf-8",
        )

    def _create_ticket(self, title, description):
        result = subprocess.run(
            [
                sys.executable,
                MAIN_PY,
                "--db",
                self.db_path,
                "create",
                "--title",
                title,
                "--description",
                description,
            ],
            capture_output=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return self._parse_single_json(result.stdout)["id"]

    @staticmethod
    def _parse_single_json(stdout):
        """标准输出必须恰好包含一个 JSON 对象，不允许多余的非空行。"""
        non_empty = [line for line in stdout.splitlines() if line.strip()]
        assert len(non_empty) == 1, f"标准输出应只有一个 JSON 对象：{stdout!r}"
        return json.loads(non_empty[0])

    def _show(self, ticket_id):
        result = self._run_cli("show", str(ticket_id))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return self._parse_single_json(result.stdout)

    def _ticket_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        finally:
            conn.close()

    def _assert_ticket_shape_matches_show(self, ticket, reference):
        """与 show 相同的结构和字段类型：四个字段，id 为整数，其余为字符串。"""
        self.assertEqual(set(ticket.keys()), {"id", "title", "description", "status"})
        self.assertEqual(set(ticket.keys()), set(reference.keys()))
        self.assertIsInstance(ticket["id"], int)
        for field in ("title", "description", "status"):
            self.assertIsInstance(ticket[field], str)

    def _assert_two_tickets_unchanged(self, expected_a, expected_b):
        """每次失败后，两条已有工单及工单数量均保持原样。"""
        self.assertEqual(self._ticket_count(), 2)
        self.assertEqual(self._show(self.id_a), expected_a)
        self.assertEqual(self._show(self.id_b), expected_b)

    # ---- 结案成功 ----

    def test_close_marks_first_ticket_closed_and_persists(self):
        before_a = self._show(self.id_a)
        before_b = self._show(self.id_b)
        self.assertEqual(before_a["status"], "open")
        self.assertEqual(before_b["status"], "open")

        result = self._run_cli("close", str(self.id_a))

        # 退出码为 0，标准错误为空。
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

        # 标准输出只有一个与 show 同结构、同字段类型的 JSON 对象。
        closed = self._parse_single_json(result.stdout)
        self._assert_ticket_shape_matches_show(closed, before_a)

        # 仅状态变为 closed，其余内容保持不变。
        self.assertEqual(closed["id"], before_a["id"])
        self.assertEqual(closed["title"], TITLE_A)
        self.assertEqual(closed["description"], DESCRIPTION_A)
        self.assertEqual(closed["status"], "closed")

        # 用另一条命令查看，证明 closed 状态已保存。
        after_a = self._show(self.id_a)
        self.assertEqual(after_a, closed)

        # 第二条工单仍为 open 且内容不变。
        after_b = self._show(self.id_b)
        self.assertEqual(after_b, before_b)
        self.assertEqual(after_b["status"], "open")
        self.assertEqual(after_b["title"], TITLE_B)
        self.assertEqual(after_b["description"], DESCRIPTION_B)

        # 结案不改变工单数量。
        self.assertEqual(self._ticket_count(), 2)

    def test_close_already_closed_is_idempotent(self):
        first = self._run_cli("close", str(self.id_a))
        self.assertEqual(first.returncode, 0, first.stderr)
        first_ticket = self._parse_single_json(first.stdout)

        # 再次结案同一编号仍成功，返回同一工单，不增加工单数量。
        second = self._run_cli("close", str(self.id_a))
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stderr, "")
        second_ticket = self._parse_single_json(second.stdout)
        self.assertEqual(second_ticket, first_ticket)
        self.assertEqual(second_ticket["status"], "closed")
        self.assertEqual(self._ticket_count(), 2)

        # 重新查看得到相同结果。
        self.assertEqual(self._show(self.id_a), first_ticket)

    # ---- 编号格式错误 ----

    def test_close_invalid_ids(self):
        expected_a = self._show(self.id_a)
        expected_b = self._show(self.id_b)

        for raw_id in ("abc", "0", "-1"):
            with self.subTest(raw_id=raw_id):
                result = self._run_cli("close", raw_id)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单编号必须为正整数\n")
                self._assert_two_tickets_unchanged(expected_a, expected_b)

    # ---- 工单不存在 ----

    def test_close_missing_ticket(self):
        # 999 不属于两条样例工单的编号。
        self.assertNotIn(999, (self.id_a, self.id_b))
        expected_a = self._show(self.id_a)
        expected_b = self._show(self.id_b)

        result = self._run_cli("close", "999")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")

        self._assert_two_tickets_unchanged(expected_a, expected_b)

    # ---- 参数用法错误 ----

    def test_close_missing_id_or_unknown_option(self):
        expected_a = self._show(self.id_a)
        expected_b = self._show(self.id_b)

        for argv in (("close",), ("close", str(self.id_a), "--unknown")):
            with self.subTest(argv=argv):
                result = self._run_cli(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                # 标准错误包含用法提示即可，不要求固定完整提示文案。
                self.assertIn("usage", result.stderr.lower())
                self._assert_two_tickets_unchanged(expected_a, expected_b)


if __name__ == "__main__":
    unittest.main()
