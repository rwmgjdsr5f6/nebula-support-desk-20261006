"""history 命令的回归测试。

通过真实命令行入口（python main.py）核对退出码、标准输出与标准错误，
并直接读取 SQLite 验证历史表的写入规则。每个用例使用独立临时数据库，
测试结束后清理，不读取或改动用户已有数据库。

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

TICKET_A = {"title": "登录失败", "description": "重置密码后仍无法登录"}
TICKET_B = {"title": "打印异常", "description": "更换网络后无法打印"}


class HistoryCommandTestCase(unittest.TestCase):
    """每个用例使用独立临时数据库，预置两条 open 状态的工单。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.id_a = self._create_ticket(TICKET_A["title"], TICKET_A["description"])
        self.id_b = self._create_ticket(TICKET_B["title"], TICKET_B["description"])

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

    def history(self, ticket_id):
        result = self.run_cli("history", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"history 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def history_rows(self, ticket_id):
        """直接读取历史表，返回 (event_id, from_status, to_status) 行。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT event_id, from_status, to_status "
                "FROM ticket_history WHERE ticket_id = ? ORDER BY event_id",
                (ticket_id,),
            ).fetchall()
        finally:
            conn.close()

    # ---------- 正常流程 ----------

    def test_acceptance_close_close_reopen_records_two_events(self):
        # 验收场景：close、重复 close、reopen 后仅保留两次实际切换
        first_close = self.run_cli("close", str(self.id_a))
        self.assertEqual(first_close.returncode, 0)
        repeated_close = self.run_cli("close", str(self.id_a))
        self.assertEqual(repeated_close.returncode, 0)
        reopen = self.run_cli("reopen", str(self.id_a))
        self.assertEqual(reopen.returncode, 0)

        result = self.run_cli("history", str(self.id_a))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        events = json.loads(result.stdout)
        self.assertEqual(
            events,
            [
                {
                    "event_id": 1,
                    "ticket_id": self.id_a,
                    "from_status": "open",
                    "to_status": "closed",
                },
                {
                    "event_id": 2,
                    "ticket_id": self.id_a,
                    "from_status": "closed",
                    "to_status": "open",
                },
            ],
        )
        # 每项仅含四个约定字段，且编号为整数、状态为字符串
        for event in events:
            self.assertEqual(
                set(event), {"event_id", "ticket_id", "from_status", "to_status"}
            )
            self.assertIsInstance(event["event_id"], int)
            self.assertIsInstance(event["ticket_id"], int)
            self.assertIn(event["from_status"], ("open", "closed"))
            self.assertIn(event["to_status"], ("open", "closed"))

        # show 仍返回原四字段对象，状态为 open
        shown = self.run_cli("show", str(self.id_a))
        self.assertEqual(shown.returncode, 0)
        ticket = json.loads(shown.stdout)
        self.assertEqual(
            ticket,
            {
                "id": self.id_a,
                "title": TICKET_A["title"],
                "description": TICKET_A["description"],
                "status": "open",
            },
        )

    def test_new_ticket_has_empty_history(self):
        # 创建只产生初始 open，不记入历史
        self.assertEqual(self.history(self.id_a), [])
        self.assertEqual(self.history_rows(self.id_a), [])

    def test_repeated_close_and_reopen_add_no_events(self):
        self.run_cli("close", str(self.id_a))
        self.run_cli("close", str(self.id_a))
        self.run_cli("reopen", str(self.id_a))
        self.run_cli("reopen", str(self.id_a))
        self.run_cli("close", str(self.id_a))
        self.run_cli("close", str(self.id_a))

        events = self.history(self.id_a)
        self.assertEqual(
            [(e["event_id"], e["from_status"], e["to_status"]) for e in events],
            [(1, "open", "closed"), (2, "closed", "open"), (3, "open", "closed")],
        )

    def test_event_id_sequence_is_per_ticket_without_mixing(self):
        # 工单 B 先结案：在 B 内编号为 1
        self.run_cli("close", str(self.id_b))
        # 工单 A 再经历 close、reopen：A 内仍从 1 独立计数
        self.run_cli("close", str(self.id_a))
        self.run_cli("reopen", str(self.id_a))

        events_a = self.history(self.id_a)
        events_b = self.history(self.id_b)
        self.assertTrue(all(e["ticket_id"] == self.id_a for e in events_a))
        self.assertTrue(all(e["ticket_id"] == self.id_b for e in events_b))
        self.assertEqual(
            [(e["event_id"], e["from_status"], e["to_status"]) for e in events_a],
            [(1, "open", "closed"), (2, "closed", "open")],
        )
        self.assertEqual(
            [(e["event_id"], e["from_status"], e["to_status"]) for e in events_b],
            [(1, "open", "closed")],
        )

    def test_history_is_persisted_across_runs_and_append_only(self):
        self.run_cli("close", str(self.id_a))
        # 每次 run_cli 都是独立进程；再次查询序号与内容不变
        first = self.history(self.id_a)
        second = self.history(self.id_a)
        self.assertEqual(first, second)
        # 之后的实际切换继续追加，序号接为 2
        self.run_cli("reopen", str(self.id_a))
        self.assertEqual(
            [(e["event_id"], e["from_status"], e["to_status"])
             for e in self.history(self.id_a)],
            [(1, "open", "closed"), (2, "closed", "open")],
        )

    def test_history_is_read_only(self):
        self.run_cli("close", str(self.id_a))
        before_rows = self.history_rows(self.id_a)

        for _ in range(2):
            result = self.run_cli("history", str(self.id_a))
            self.assertEqual(result.returncode, 0)

        self.assertEqual(self.history_rows(self.id_a), before_rows)
        # 工单本身与另一条工单均不受影响
        shown_a = json.loads(self.run_cli("show", str(self.id_a)).stdout)
        self.assertEqual(shown_a["status"], "closed")
        shown_b = json.loads(self.run_cli("show", str(self.id_b)).stdout)
        self.assertEqual(shown_b["status"], "open")

    def test_id_parsing_matches_show(self):
        self.run_cli("close", str(self.id_a))
        # 允许正号、前导零与首尾空白
        for raw in (f"+{self.id_a}", f"0{self.id_a}", f"  {self.id_a}  ",
                    f" +0{self.id_a} "):
            with self.subTest(raw=raw):
                result = self.run_cli("history", raw)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(
                    json.loads(result.stdout),
                    [
                        {
                            "event_id": 1,
                            "ticket_id": self.id_a,
                            "from_status": "open",
                            "to_status": "closed",
                        }
                    ],
                )

    # ---------- 编号格式错误 ----------

    def test_history_rejects_non_positive_integer_ids(self):
        for bad_id in ("abc", "0", "-1", "1.5"):
            with self.subTest(bad_id=bad_id):
                result = self.run_cli("history", bad_id)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单编号必须为正整数\n")
                # 失败不新增历史
                self.assertEqual(self.history_rows(self.id_a), [])

    # ---------- 编号不存在 / 超界 ----------

    def test_history_reports_missing_ticket(self):
        result = self.run_cli("history", "999")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")

    def test_history_overflow_id_reports_missing(self):
        for raw in (str(SQLITE_MAX_INT + 1), "+099999999999999999999"):
            with self.subTest(raw=raw):
                result = self.run_cli("history", raw)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单不存在\n")

    def test_history_max_int_absent_reports_missing(self):
        result = self.run_cli("history", str(SQLITE_MAX_INT))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "工单不存在\n")

    # ---------- 参数用法错误 ----------

    def test_history_usage_errors_exit_with_code_2(self):
        for argv in (("history",), ("history", str(self.id_a), "--unknown")):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage", result.stderr.lower())
                # 用法错误不新增历史
                self.assertEqual(self.history_rows(self.id_a), [])


class HistoryOldDatabaseTestCase(unittest.TestCase):
    """旧版本数据库（无历史表）启用新功能后的兼容行为。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "old_tickets.sqlite")

        # 手工构造旧版本库：只有 tickets 与 ticket_notes，
        # 工单 1 在过去已结案并留有备注；不推断任何历史
        conn = sqlite3.connect(self.db_path)
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
            """
            CREATE TABLE ticket_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                note_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                UNIQUE (ticket_id, note_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO tickets (title, description, status) "
            "VALUES ('旧工单', '过去已结案', 'closed')"
        )
        conn.execute(
            "INSERT INTO ticket_notes (ticket_id, note_id, text) "
            "VALUES (1, 1, '历史备注')"
        )
        conn.commit()
        conn.close()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def test_old_tickets_start_without_inferred_history(self):
        result = self.run_cli("history", "1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), [])

        # 重复结案旧工单不补记历史，备注保留
        repeated = self.run_cli("close", "1")
        self.assertEqual(repeated.returncode, 0)
        self.assertEqual(json.loads(self.run_cli("history", "1").stdout), [])
        notes = json.loads(self.run_cli("notes", "1").stdout)
        self.assertEqual(
            notes, [{"note_id": 1, "ticket_id": 1, "text": "历史备注"}]
        )

        # 实际重开才从序号 1 开始记录，from_status 为当前 closed
        self.assertEqual(self.run_cli("reopen", "1").returncode, 0)
        events = json.loads(self.run_cli("history", "1").stdout)
        self.assertEqual(
            events,
            [
                {
                    "event_id": 1,
                    "ticket_id": 1,
                    "from_status": "closed",
                    "to_status": "open",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
