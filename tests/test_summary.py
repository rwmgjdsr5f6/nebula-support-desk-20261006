"""summary 命令的回归测试。

通过真实命令行入口（python main.py）核对退出码、标准输出与标准错误，
并直接读取 SQLite 验证摘要查询的只读性。每个用例使用独立临时数据库，
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

NOTE_A1 = "已复现"
NOTE_A2 = " 等待确认 "
NOTE_B1 = "独立记录"

SUMMARY_KEYS = {"ticket", "note_count", "latest_note", "status_change_count"}


class SummaryCommandTestCase(unittest.TestCase):
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

    def summary(self, raw_id):
        """运行 summary 并要求成功，返回解析后的 JSON 对象。"""
        result = self.run_cli("summary", str(raw_id))
        self.assertEqual(result.returncode, 0, f"summary 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def show(self, ticket_id):
        result = self.run_cli("show", str(ticket_id))
        self.assertEqual(result.returncode, 0, f"show 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def add_note(self, ticket_id, text):
        result = self.run_cli("add-note", str(ticket_id), "--text", text)
        self.assertEqual(result.returncode, 0, f"add-note 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def all_rows(self):
        """直接读取三张表的全部记录（不含建表等元信息），用于只读性核对。"""
        conn = sqlite3.connect(self.db_path)
        try:
            tickets = conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
            notes = conn.execute(
                "SELECT ticket_id, note_id, text FROM ticket_notes "
                "ORDER BY ticket_id, note_id"
            ).fetchall()
            history = conn.execute(
                "SELECT ticket_id, event_id, from_status, to_status "
                "FROM ticket_history ORDER BY ticket_id, event_id"
            ).fetchall()
            return {"tickets": tickets, "notes": notes, "history": history}
        finally:
            conn.close()

    def assert_summary_shape(self, summary):
        """核对摘要的字段集合与类型，不依赖键顺序或格式空格。"""
        self.assertEqual(set(summary), SUMMARY_KEYS)
        self.assertIsInstance(summary["ticket"], dict)
        self.assertEqual(
            set(summary["ticket"]), {"id", "title", "description", "status"}
        )
        self.assertIsInstance(summary["note_count"], int)
        self.assertIsInstance(summary["status_change_count"], int)
        latest = summary["latest_note"]
        if latest is not None:
            self.assertEqual(set(latest), {"note_id", "ticket_id", "text"})
            self.assertIsInstance(latest["note_id"], int)
            self.assertIsInstance(latest["ticket_id"], int)
            self.assertIsInstance(latest["text"], str)

    # ---------- 正常流程 ----------

    def test_new_ticket_summary_matches_show_with_zero_counts(self):
        # 新建工单无备注与状态切换：ticket 与 show 一致，计数为整数 0
        summary = self.summary(self.id_a)
        self.assert_summary_shape(summary)
        self.assertEqual(summary["ticket"], self.show(self.id_a))
        self.assertEqual(
            summary["ticket"],
            {
                "id": self.id_a,
                "title": TICKET_A["title"],
                "description": TICKET_A["description"],
                "status": "open",
            },
        )
        self.assertEqual(summary["note_count"], 0)
        self.assertEqual(summary["status_change_count"], 0)
        self.assertIsNone(summary["latest_note"])

    def test_acceptance_notes_and_status_changes(self):
        # 交错添加备注：A 两条（第二条含首尾空格），B 一条独立记录
        note_a1 = self.add_note(self.id_a, NOTE_A1)
        note_b1 = self.add_note(self.id_b, NOTE_B1)
        note_a2 = self.add_note(self.id_a, NOTE_A2)
        self.assertEqual(note_a1, {"note_id": 1, "ticket_id": self.id_a, "text": NOTE_A1})
        self.assertEqual(note_b1, {"note_id": 1, "ticket_id": self.id_b, "text": NOTE_B1})
        self.assertEqual(note_a2, {"note_id": 2, "ticket_id": self.id_a, "text": NOTE_A2})

        # A 依次结案、重复结案、重开：仅两次实际状态切换
        self.assertEqual(self.run_cli("close", str(self.id_a)).returncode, 0)
        self.assertEqual(self.run_cli("close", str(self.id_a)).returncode, 0)
        self.assertEqual(self.run_cli("reopen", str(self.id_a)).returncode, 0)

        summary_a = self.summary(self.id_a)
        self.assert_summary_shape(summary_a)
        self.assertEqual(summary_a["ticket"], self.show(self.id_a))
        self.assertEqual(summary_a["ticket"]["status"], "open")
        self.assertEqual(summary_a["note_count"], 2)
        # 最新备注完整保留首尾空格，编号为 2，归属第一张工单
        self.assertEqual(
            summary_a["latest_note"],
            {"note_id": 2, "ticket_id": self.id_a, "text": NOTE_A2},
        )
        self.assertEqual(summary_a["status_change_count"], 2)

        # B 的摘要只含自己的备注，编号从 1 开始，无状态变化
        summary_b = self.summary(self.id_b)
        self.assert_summary_shape(summary_b)
        self.assertEqual(summary_b["ticket"], self.show(self.id_b))
        self.assertEqual(summary_b["ticket"]["status"], "open")
        self.assertEqual(summary_b["note_count"], 1)
        self.assertEqual(
            summary_b["latest_note"],
            {"note_id": 1, "ticket_id": self.id_b, "text": NOTE_B1},
        )
        self.assertEqual(summary_b["status_change_count"], 0)

    def test_summary_is_repeatable_across_processes(self):
        self.add_note(self.id_a, NOTE_A1)
        self.run_cli("close", str(self.id_a))

        # 每次 run_cli 都是独立进程；重复查询与跨进程读取结果一致
        first = self.summary(self.id_a)
        second = self.summary(self.id_a)
        third = self.summary(self.id_a)
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_summary_is_read_only(self):
        self.add_note(self.id_a, NOTE_A1)
        self.add_note(self.id_b, NOTE_B1)
        self.run_cli("close", str(self.id_a))
        self.run_cli("reopen", str(self.id_a))
        before = self.all_rows()

        for _ in range(2):
            self.summary(self.id_a)
            self.summary(self.id_b)

        # 比较三张表的记录内容，建表初始化不算记录变更
        self.assertEqual(self.all_rows(), before)
        # 工单、备注、历史的公开查询结果也不变
        self.assertEqual(self.show(self.id_a)["status"], "open")
        notes_a = json.loads(self.run_cli("notes", str(self.id_a)).stdout)
        self.assertEqual(
            notes_a, [{"note_id": 1, "ticket_id": self.id_a, "text": NOTE_A1}]
        )
        events_a = json.loads(self.run_cli("history", str(self.id_a)).stdout)
        self.assertEqual(
            [(e["event_id"], e["from_status"], e["to_status"]) for e in events_a],
            [(1, "open", "closed"), (2, "closed", "open")],
        )

    def test_id_parsing_matches_show(self):
        self.add_note(self.id_a, NOTE_A1)
        expected = self.summary(self.id_a)
        # 允许正号、前导零与首尾空白
        for raw in (f"+{self.id_a}", f"0{self.id_a}", f"  {self.id_a}  ",
                    f" +0{self.id_a} "):
            with self.subTest(raw=raw):
                result = self.run_cli("summary", raw)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(json.loads(result.stdout), expected)

    # ---------- 编号格式错误 ----------

    def test_summary_rejects_non_positive_integer_ids(self):
        before = self.all_rows()
        for bad_id in ("abc", "0", "-1"):
            with self.subTest(bad_id=bad_id):
                result = self.run_cli("summary", bad_id)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单编号必须为正整数\n")
        # 失败查询不改动任何记录
        self.assertEqual(self.all_rows(), before)

    # ---------- 编号不存在 / 超界 ----------

    def test_summary_reports_missing_ticket(self):
        before = self.all_rows()
        for raw in ("999", str(SQLITE_MAX_INT + 1)):
            with self.subTest(raw=raw):
                result = self.run_cli("summary", raw)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单不存在\n")
        self.assertEqual(self.all_rows(), before)

    # ---------- 参数用法错误 ----------

    def test_summary_usage_errors_exit_with_code_2(self):
        before = self.all_rows()
        for argv in (("summary",), ("summary", str(self.id_a), "--unknown")):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage", result.stderr.lower())
        self.assertEqual(self.all_rows(), before)


if __name__ == "__main__":
    unittest.main()
