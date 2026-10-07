"""summary --include-draft 选项的回归测试。

通过真实命令行入口（python main.py）核对处理摘要附带回复草稿的公开行为：
固定样例从空库创建两张 open 合成工单（描述均为空，无备注与历史），
第一张用 set-draft 保存首尾带空格的正文，第二张不保存草稿。覆盖
--include-draft 时 draft 字段与 draft 命令结果完全一致（含首尾空白、
换行与引号按原文保留）、无草稿或已清除时 draft 仍为对象且 text 为 null、
覆盖后只展示新正文、open/closed 均可使用、跨工单草稿不混入、不带选项时
仍只有原有四个字段；另核对编号解析沿用既有规则、三类失败的退出码与
标准错误、查询的只读性，以及缺少草稿表的旧库沿用现有初始化行为。

每个用例使用独立临时目录，通过 --db 明确指定临时 SQLite 文件，
测试结束后清理，不读取或改动工作目录下的默认库 tickets.sqlite，
也不依赖网络或第三方包。比较一律基于解析后的 JSON 值、字段类型，
不依赖键顺序或排版空格；建表初始化产生的空表不计为记录变更。

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
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = PROJECT_ROOT / "main.py"

# 超过 SQLite INTEGER 主键最大值的编号：按正整数解析但不可能命中记录
OVERSIZED_ID = "9223372036854775808"

# 固定样例：两张 open 工单，描述均为空
TICKET_A = {"title": "登录失败", "description": ""}
TICKET_B = {"title": "打印异常", "description": ""}

# 第一张工单保存的草稿：首尾空格必须完整保留
DRAFT_TEXT_A = " 请重试登录 "
# 覆盖后的新正文：换行与引号按原文保留
NEW_DRAFT_TEXT = '已为您重试\n请使用"新密码"登录'

SUMMARY_KEYS = {"ticket", "note_count", "latest_note", "status_change_count"}
DRAFT_KEYS = {"ticket_id", "text"}


class SummaryIncludeDraftTestCase(unittest.TestCase):
    """正常流程与边界；每个用例使用独立临时数据库，预置两张 open 工单。"""

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

    @staticmethod
    def parse_single_json(stdout):
        """标准输出必须恰好是一个 JSON 值，不允许前后夹带说明文字。"""
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        if stripped[end:].strip() != "":
            raise AssertionError(f"标准输出不是唯一 JSON 值: {stdout!r}")
        return value

    def set_draft(self, ticket_id, text):
        result = self.run_cli("set-draft", str(ticket_id), "--text", text)
        self.assertEqual(result.returncode, 0, f"set-draft 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json(result.stdout)

    def draft(self, raw_id):
        result = self.run_cli("draft", str(raw_id))
        self.assertEqual(result.returncode, 0, f"draft 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return self.parse_single_json(result.stdout)

    def summary(self, raw_id, include_draft=False):
        argv = ["summary", str(raw_id)]
        if include_draft:
            argv.append("--include-draft")
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 0, f"summary 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        summary = self.parse_single_json(result.stdout)
        self.assertIsInstance(summary, dict)
        return summary

    def assertDraftObject(self, draft, ticket_id, text):
        """draft 使用既有草稿对象结构：仅含整数 ticket_id 与 text。"""
        self.assertIsInstance(draft, dict)
        self.assertEqual(set(draft), DRAFT_KEYS)
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(draft["ticket_id"]), int)
        self.assertEqual(
            draft, {"ticket_id": ticket_id, "text": text}
        )

    def snapshot(self):
        """导出四张表的全部行（含编号），用于核对查询前后无任何记录变更。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return {
                "tickets": conn.execute(
                    "SELECT id, title, description, status "
                    "FROM tickets ORDER BY id"
                ).fetchall(),
                "notes": conn.execute(
                    "SELECT ticket_id, note_id, text "
                    "FROM ticket_notes ORDER BY ticket_id, note_id"
                ).fetchall(),
                "history": conn.execute(
                    "SELECT ticket_id, event_id, from_status, to_status "
                    "FROM ticket_history ORDER BY ticket_id, event_id"
                ).fetchall(),
                "drafts": conn.execute(
                    "SELECT ticket_id, text FROM ticket_drafts ORDER BY ticket_id"
                ).fetchall(),
            }
        finally:
            conn.close()

    # ---------- demo 验收场景 ----------

    def test_demo_draft_included_with_spaces_and_empty_other_is_null(self):
        # 仅第一张保存草稿；两张工单描述为空且没有备注与历史
        self.set_draft(self.id_a, DRAFT_TEXT_A)

        summary_a = self.summary(self.id_a, include_draft=True)
        # 附带草稿后恰好五个字段
        self.assertEqual(set(summary_a), SUMMARY_KEYS | {"draft"})
        self.assertEqual(summary_a["note_count"], 0)
        self.assertEqual(summary_a["status_change_count"], 0)
        self.assertIsNone(summary_a["latest_note"])
        # 首尾空格完整保留
        self.assertDraftObject(summary_a["draft"], self.id_a, DRAFT_TEXT_A)
        # 与同库同编号 draft 命令结果完全一致
        self.assertEqual(summary_a["draft"], self.draft(self.id_a))

        # 第二张没有草稿：draft 仍是对象，ticket_id 为实际编号，text 为 null
        summary_b = self.summary(self.id_b, include_draft=True)
        self.assertEqual(set(summary_b), SUMMARY_KEYS | {"draft"})
        self.assertEqual(summary_b["note_count"], 0)
        self.assertIsNone(summary_b["latest_note"])
        self.assertDraftObject(summary_b["draft"], self.id_b, None)
        self.assertEqual(summary_b["draft"], self.draft(self.id_b))

    # ---------- 不带选项时输出不变 ----------

    def test_without_flag_output_keeps_only_original_fields(self):
        self.set_draft(self.id_a, DRAFT_TEXT_A)
        summary = self.summary(self.id_a)
        # 即使工单已有草稿，不带选项时仍恰好四个原有字段
        self.assertEqual(set(summary), SUMMARY_KEYS)
        self.assertNotIn("draft", summary)

    # ---------- 选项位置 ----------

    def test_flag_works_before_or_after_id(self):
        self.set_draft(self.id_a, DRAFT_TEXT_A)
        expected = self.summary(self.id_a, include_draft=True)

        for argv in (
            ("summary", "--include-draft", str(self.id_a)),
            ("summary", str(self.id_a), "--include-draft"),
        ):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.parse_single_json(result.stdout), expected)

    # ---------- 原文保留：换行、引号与覆盖 ----------

    def test_draft_text_preserves_newlines_quotes_and_overwrite_only_new(self):
        self.set_draft(self.id_a, DRAFT_TEXT_A)
        self.set_draft(self.id_a, NEW_DRAFT_TEXT)

        summary = self.summary(self.id_a, include_draft=True)
        self.assertDraftObject(summary["draft"], self.id_a, NEW_DRAFT_TEXT)
        self.assertEqual(summary["draft"], self.draft(self.id_a))
        # 旧正文不再出现：整篇覆盖
        self.assertNotIn(DRAFT_TEXT_A, summary["draft"]["text"])

    def test_cleared_draft_reports_null_text_but_keeps_object(self):
        self.set_draft(self.id_a, DRAFT_TEXT_A)
        clear = self.run_cli("clear-draft", str(self.id_a))
        self.assertEqual(clear.returncode, 0)
        self.assertEqual(clear.stderr, "")

        summary = self.summary(self.id_a, include_draft=True)
        self.assertDraftObject(summary["draft"], self.id_a, None)
        self.assertEqual(summary["draft"], self.draft(self.id_a))

    # ---------- open / closed 均可使用 ----------

    def test_closed_ticket_includes_draft(self):
        self.set_draft(self.id_a, DRAFT_TEXT_A)
        close = self.run_cli("close", str(self.id_a))
        self.assertEqual(close.returncode, 0)

        summary = self.summary(self.id_a, include_draft=True)
        self.assertEqual(summary["ticket"]["status"], "closed")
        self.assertDraftObject(summary["draft"], self.id_a, DRAFT_TEXT_A)

    # ---------- 跨工单隔离 ----------

    def test_other_tickets_drafts_never_mix_in(self):
        self.set_draft(self.id_a, DRAFT_TEXT_A)
        # 第二张即使结案也不出现第一张的草稿
        self.run_cli("close", str(self.id_b))
        summary_b = self.summary(self.id_b, include_draft=True)
        self.assertDraftObject(summary_b["draft"], self.id_b, None)

        summary_a = self.summary(self.id_a, include_draft=True)
        self.assertDraftObject(summary_a["draft"], self.id_a, DRAFT_TEXT_A)

    # ---------- 重复查询一致 ----------

    def test_repeated_queries_in_separate_processes_return_identical_result(self):
        self.set_draft(self.id_a, DRAFT_TEXT_A)
        results = [self.summary(self.id_a, include_draft=True) for _ in range(3)]
        for other in results[1:]:
            self.assertEqual(other, results[0])
        null_results = [self.summary(self.id_b, include_draft=True) for _ in range(2)]
        self.assertEqual(null_results[1], null_results[0])

    # ---------- 编号写法 ----------

    def test_id_spellings_with_plus_zeros_and_padding_read_same_ticket(self):
        self.set_draft(self.id_a, DRAFT_TEXT_A)
        expected = self.summary(self.id_a, include_draft=True)

        # 正号、前导零、首尾空白（含组合写法）解析为同一编号
        for raw in (
            f"+{self.id_a}",
            f"0{self.id_a}",
            f"  {self.id_a}  ",
            f" +0{self.id_a} ",
        ):
            with self.subTest(raw=raw):
                result = self.run_cli("summary", raw, "--include-draft")
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.parse_single_json(result.stdout), expected)

    # ---------- 编号格式错误 ----------

    def test_non_positive_integer_ids_exit_1_with_format_error(self):
        for bad_id in ("abc", "0", "-1"):
            with self.subTest(bad_id=bad_id):
                before = self.snapshot()
                result = self.run_cli("summary", bad_id, "--include-draft")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单编号必须为正整数\n")
                self.assertEqual(self.snapshot(), before)

    # ---------- 编号不存在 / 超界 ----------

    def test_unknown_ids_exit_1_with_not_found_error(self):
        for raw_id in ("999", OVERSIZED_ID):
            with self.subTest(raw_id=raw_id):
                before = self.snapshot()
                result = self.run_cli("summary", raw_id, "--include-draft")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "工单不存在\n")
                self.assertEqual(self.snapshot(), before)

    # ---------- 参数用法错误 ----------

    def test_usage_errors_exit_2_with_usage_message(self):
        for argv in (
            # 缺少编号（仅有选项）
            ("summary", "--include-draft"),
            # 未知选项
            ("summary", str(self.id_a), "--unknown"),
            # 额外位置参数
            ("summary", str(self.id_a), str(self.id_b), "--include-draft"),
            # --include-draft 是开关，不接受值
            ("summary", str(self.id_a), "--include-draft", "yes"),
        ):
            with self.subTest(argv=argv):
                before = self.snapshot()
                result = self.run_cli(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage", result.stderr.lower())
                self.assertEqual(self.snapshot(), before)

    # ---------- 只读性 ----------

    def test_queries_leave_all_records_including_drafts_unchanged(self):
        self.set_draft(self.id_a, DRAFT_TEXT_A)
        self.set_draft(self.id_b, "第二张草稿")
        self.run_cli("add-note", str(self.id_a), "--text", "备注一")
        self.run_cli("close", str(self.id_b))

        before = self.snapshot()

        # 成功查询（含不带选项与无草稿工单）与各类失败查询交错进行
        self.summary(self.id_a, include_draft=True)
        self.summary(self.id_a)
        self.summary(self.id_b, include_draft=True)
        for raw_id in ("abc", "0", "-1", "999", OVERSIZED_ID):
            failed = self.run_cli("summary", raw_id, "--include-draft")
            self.assertEqual(failed.returncode, 1)
        for argv in (
            ("summary", "--include-draft"),
            ("summary", str(self.id_a), "--unknown"),
        ):
            self.assertEqual(self.run_cli(*argv).returncode, 2)
        self.summary(self.id_a, include_draft=True)

        # 工单、备注、历史、草稿的内容与编号前后完全相同
        self.assertEqual(self.snapshot(), before)


class SummaryIncludeDraftLegacyDatabaseTestCase(unittest.TestCase):
    """缺少 ticket_drafts 表的旧库：沿用现有初始化行为，记录保留。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "legacy_tickets.sqlite")

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "title TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE ticket_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ticket_id INTEGER NOT NULL, note_id INTEGER NOT NULL, text TEXT NOT NULL, "
            "UNIQUE (ticket_id, note_id))"
        )
        conn.execute(
            "CREATE TABLE ticket_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ticket_id INTEGER NOT NULL, event_id INTEGER NOT NULL, "
            "from_status TEXT NOT NULL, to_status TEXT NOT NULL, "
            "UNIQUE (ticket_id, event_id))"
        )
        conn.execute(
            "INSERT INTO tickets (id, title, description, status) "
            "VALUES (1, '旧工单', '旧描述', 'open')"
        )
        conn.commit()
        conn.close()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    @staticmethod
    def parse_single_json(stdout):
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        if stripped[end:].strip() != "":
            raise AssertionError(f"标准输出不是唯一 JSON 值: {stdout!r}")
        return value

    def test_legacy_database_initializes_draft_table_and_preserves_records(self):
        # 带选项查询：旧库工单存在但无草稿，text 为 null；原有记录保留
        result = self.run_cli("summary", "1", "--include-draft")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        summary = self.parse_single_json(result.stdout)
        self.assertEqual(summary["draft"], {"ticket_id": 1, "text": None})
        self.assertEqual(summary["note_count"], 0)
        self.assertEqual(summary["status_change_count"], 0)
        self.assertEqual(
            summary["ticket"],
            {"id": 1, "title": "旧工单", "description": "旧描述", "status": "open"},
        )

        # 不带选项的结果与旧库无草稿时完全相同：无 draft 字段
        plain = self.run_cli("summary", "1")
        self.assertEqual(plain.returncode, 0)
        self.assertEqual(plain.stderr, "")
        plain_summary = self.parse_single_json(plain.stdout)
        self.assertEqual(
            set(plain_summary),
            {"ticket", "note_count", "latest_note", "status_change_count"},
        )

        # 初始化只补建空草稿表，原有工单记录原样保留
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT id, title, description, status FROM tickets"
                ).fetchall(),
                [(1, "旧工单", "旧描述", "open")],
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ticket_drafts").fetchone()[0], 0
            )
        finally:
            conn.close()


class SummaryIncludeDraftEmptyDatabaseTestCase(unittest.TestCase):
    """空库上的失败查询：建表初始化不产生任何记录。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "empty_tickets.sqlite")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def test_usage_error_before_connect_does_not_create_database(self):
        # 缺少编号时 argparse 在打开数据库前退出，不应留下数据库文件
        result = self.run_cli("summary", "--include-draft")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertFalse(os.path.exists(self.db_path))


if __name__ == "__main__":
    unittest.main()
