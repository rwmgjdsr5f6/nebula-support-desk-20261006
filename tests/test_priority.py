"""set-priority 与 priority 命令的回归测试。

通过真实命令行入口（python main.py）核对每张工单一条优先级的保存与读取：
固定样例从空库创建编号 1 的 open“登录失败”与编号 2 的 closed“打印异常”
两条合成工单（第一条另预置备注与草稿，第二条结案时产生一条状态历史），
覆盖初始未设置读取为 normal 且不落库、set-priority 设置（high/low/normal、
整篇覆盖与重复设置）、跨进程读取最后设置值、open 与 closed 工单彼此独立、
编号解析沿用 show、设置不改工单/备注/草稿/状态历史且 show 与 list
保留原四字段结构，以及编号非法、工单不存在与用法错误三类确定的失败结果；
另核对不含优先级表的旧库可直接读取为 normal，设置后跨次调用仍读到。

每个用例使用独立临时目录，通过 --db 明确指定临时 SQLite 文件，
测试结束后清理，不读取或改动工作目录下的默认库 tickets.sqlite。
比较一律基于解析后的 JSON 值，不依赖键顺序或排版空格。

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

# 固定样例：两条合成工单，仅第二条结案
TICKET_A = {"title": "登录失败", "description": "重置密码后仍无法登录"}
TICKET_B = {"title": "打印异常", "description": "更换网络后无法打印"}

# 第一条工单预置的备注与草稿，用于核对设置优先级不改动这些数据
NOTE_A_TEXT = "已联系用户，等待确认"
DRAFT_A_TEXT = " 请重试登录 "

PRIORITY_KEYS = {"ticket_id", "priority"}
TICKET_KEYS = {"id", "title", "description", "status"}

# 超过 SQLite 有符号整数上限的编号：按工单不存在处理
OVERFLOW_ID = "9223372036854775808"


class PriorityWorkflowTestCase(unittest.TestCase):
    """空库起步的优先级保存与读取正常流程；每个用例使用独立临时数据库。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        # 从空库创建两条固定样例工单，编号固定为 1、2
        self.id_a = self._create_ticket(
            TICKET_A["title"], TICKET_A["description"]
        )
        self.id_b = self._create_ticket(
            TICKET_B["title"], TICKET_B["description"]
        )
        self.assertEqual(self.id_a, 1)
        self.assertEqual(self.id_b, 2)

        # 仅将第二条结案：为工单 2 产生一条 open -> closed 状态历史
        result = self.run_cli("close", str(self.id_b))
        self.assertEqual(result.returncode, 0, f"准备样例失败: {result.stderr}")
        self.assertEqual(result.stderr, "")

        # 第一条工单预置一条备注与一份草稿
        noted = self.run_cli(
            "add-note", str(self.id_a), "--text", NOTE_A_TEXT
        )
        self.assertEqual(noted.returncode, 0, f"准备样例失败: {noted.stderr}")
        self.assertEqual(noted.stderr, "")
        drafted = self.run_cli(
            "set-draft", str(self.id_a), "--text", DRAFT_A_TEXT
        )
        self.assertEqual(drafted.returncode, 0, f"准备样例失败: {drafted.stderr}")
        self.assertEqual(drafted.stderr, "")

        # 优先级操作前后都必须保持不变的四张表快照
        self.fixture_snapshot = self.snapshot_other_tables()

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

    def set_priority(self, raw_id, value):
        """执行 set-priority，要求成功并返回解析后的唯一优先级 JSON 对象。"""
        result = self.run_cli("set-priority", str(raw_id), "--value", value)
        self.assertEqual(
            result.returncode,
            0,
            f"set-priority {raw_id!r} --value {value!r} 失败: {result.stderr}",
        )
        self.assertEqual(result.stderr, "")
        priority = self.parse_single_json(result.stdout)
        self.assertIsInstance(priority, dict)
        return priority

    def read_priority(self, raw_id):
        """执行 priority，要求成功并返回解析后的唯一优先级 JSON 对象。"""
        result = self.run_cli("priority", str(raw_id))
        self.assertEqual(
            result.returncode, 0, f"priority {raw_id!r} 失败: {result.stderr}"
        )
        self.assertEqual(result.stderr, "")
        priority = self.parse_single_json(result.stdout)
        self.assertIsInstance(priority, dict)
        return priority

    def assertPriorityObject(self, priority, ticket_id, level):
        """成功输出仅含整数 ticket_id 与字符串 priority，且值与预期一致。"""
        self.assertIsInstance(priority, dict)
        self.assertEqual(set(priority.keys()), PRIORITY_KEYS)
        # 明确要求 int 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(priority["ticket_id"]), int)
        self.assertIs(type(priority["priority"]), str)
        self.assertEqual(
            priority, {"ticket_id": ticket_id, "priority": level}
        )

    def priority_rows(self):
        """直接读取 SQLite 中全部优先级记录，按工单编号排序。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT ticket_id, priority FROM ticket_priorities "
                "ORDER BY ticket_id"
            ).fetchall()
        finally:
            conn.close()

    def snapshot_other_tables(self):
        """导出工单、备注、状态历史与草稿四张表的全部行（含自增编号）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return {
                "tickets": conn.execute(
                    "SELECT id, title, description, status FROM tickets ORDER BY id"
                ).fetchall(),
                "ticket_notes": conn.execute(
                    "SELECT id, ticket_id, note_id, text FROM ticket_notes "
                    "ORDER BY ticket_id, note_id, id"
                ).fetchall(),
                "ticket_history": conn.execute(
                    "SELECT id, ticket_id, event_id, from_status, to_status "
                    "FROM ticket_history ORDER BY ticket_id, event_id, id"
                ).fetchall(),
                "ticket_drafts": conn.execute(
                    "SELECT id, ticket_id, text FROM ticket_drafts "
                    "ORDER BY ticket_id, id"
                ).fetchall(),
            }
        finally:
            conn.close()

    def assert_other_tables_unchanged(self):
        """优先级操作不改变工单、备注、状态历史与草稿中的任何一行。"""
        self.assertEqual(
            self.snapshot_other_tables(),
            self.fixture_snapshot,
        )

    # ---------- 初次读取均为 normal，且只读不落库 ----------

    def test_initial_read_returns_normal_without_creating_record(self):
        for ticket_id in (self.id_a, self.id_b):
            with self.subTest(command="priority", ticket_id=ticket_id):
                # 每次都是独立进程调用
                self.assertPriorityObject(
                    self.read_priority(ticket_id), ticket_id, "normal"
                )

        # 读取未设置等级不新增任何优先级记录
        self.assertEqual(self.priority_rows(), [])
        self.assert_other_tables_unchanged()

    # ---------- 设置、覆盖、恢复与跨进程读取 ----------

    def test_set_priority_returns_same_object_as_read_and_last_value_wins(self):
        # set-priority 1 --value high 的返回对象本身就是该对象
        saved = self.set_priority(self.id_a, "high")
        self.assertPriorityObject(saved, self.id_a, "high")

        # 另一次独立进程调用 priority 1：返回同样的对象
        self.assertPriorityObject(self.read_priority(self.id_a), self.id_a, "high")

        # 继续改成 low：设置返回值与随后读取都是最后设置值
        self.assertPriorityObject(
            self.set_priority(self.id_a, "low"), self.id_a, "low"
        )
        self.assertPriorityObject(self.read_priority(self.id_a), self.id_a, "low")

        # 恢复 normal：读取仍返回最后设置值
        self.assertPriorityObject(
            self.set_priority(self.id_a, "normal"), self.id_a, "normal"
        )
        self.assertPriorityObject(
            self.read_priority(self.id_a), self.id_a, "normal"
        )

        # 重复设置同一等级同样成功，读取不变
        self.assertPriorityObject(
            self.set_priority(self.id_a, "normal"), self.id_a, "normal"
        )
        self.assertPriorityObject(
            self.read_priority(self.id_a), self.id_a, "normal"
        )

        # 整篇覆盖：该工单在库中始终只有一行等级记录
        self.assertEqual(self.priority_rows(), [(self.id_a, "normal")])
        self.assert_other_tables_unchanged()

    # ---------- 两张工单均可设置且彼此独立 ----------

    def test_closed_ticket_settable_and_priorities_stay_per_ticket(self):
        # open 的工单 1 设为 low，closed 的工单 2 设为 high
        self.assertPriorityObject(
            self.set_priority(self.id_a, "low"), self.id_a, "low"
        )
        self.assertPriorityObject(
            self.set_priority(self.id_b, "high"), self.id_b, "high"
        )

        # 跨进程读取：各自保留自己的等级
        self.assertPriorityObject(self.read_priority(self.id_a), self.id_a, "low")
        self.assertPriorityObject(self.read_priority(self.id_b), self.id_b, "high")

        # 只改工单 2：工单 1 的等级不受影响，反之亦然
        self.assertPriorityObject(
            self.set_priority(self.id_b, "normal"), self.id_b, "normal"
        )
        self.assertPriorityObject(self.read_priority(self.id_a), self.id_a, "low")
        self.assertPriorityObject(
            self.read_priority(self.id_b), self.id_b, "normal"
        )
        self.assertPriorityObject(
            self.set_priority(self.id_a, "high"), self.id_a, "high"
        )
        self.assertPriorityObject(self.read_priority(self.id_b), self.id_b, "normal")

        # 每张工单至多一行，互不串改
        self.assertEqual(
            self.priority_rows(),
            [(self.id_a, "high"), (self.id_b, "normal")],
        )
        self.assert_other_tables_unchanged()

    # ---------- 编号解析沿用 show：正号、前导零与首尾空白 ----------

    def test_id_parsing_accepts_plus_sign_leading_zeros_and_blanks(self):
        self.set_priority(self.id_a, "high")
        for raw_id in ("+1", "01", " 1 ", " +001 "):
            with self.subTest(command="priority", raw_id=raw_id):
                self.assertPriorityObject(
                    self.read_priority(raw_id), self.id_a, "high"
                )

        # set-priority 的编号采用同等写法时仍指向同一工单
        saved = self.set_priority(" 01 ", "low")
        self.assertPriorityObject(saved, self.id_a, "low")
        self.assertPriorityObject(self.read_priority("+001"), self.id_a, "low")
        # 等价写法没有产生第二行记录
        self.assertEqual(self.priority_rows(), [(self.id_a, "low")])

    # ---------- show 与 list 保留原四字段结构 ----------

    def test_show_and_list_keep_original_four_field_shape(self):
        self.set_priority(self.id_a, "high")
        self.set_priority(self.id_b, "low")

        expected_tickets = [
            {
                "id": self.id_a,
                "title": TICKET_A["title"],
                "description": TICKET_A["description"],
                "status": "open",
            },
            {
                "id": self.id_b,
                "title": TICKET_B["title"],
                "description": TICKET_B["description"],
                "status": "closed",
            },
        ]

        for ticket_id, expected in (
            (self.id_a, expected_tickets[0]),
            (self.id_b, expected_tickets[1]),
        ):
            with self.subTest(command="show", ticket_id=ticket_id):
                result = self.run_cli("show", str(ticket_id))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                shown = self.parse_single_json(result.stdout)
                self.assertIsInstance(shown, dict)
                self.assertEqual(set(shown.keys()), TICKET_KEYS)
                self.assertEqual(shown, expected)

        result = self.run_cli("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        listed = self.parse_single_json(result.stdout)
        self.assertIsInstance(listed, list)
        for item in listed:
            self.assertEqual(set(item.keys()), TICKET_KEYS)
        self.assertEqual(listed, expected_tickets)

        self.assert_other_tables_unchanged()


class PriorityErrorTestCase(unittest.TestCase):
    """失败结果：编号非法、工单不存在与用法错误。

    样例库中工单 1 已预置 high，工单 2 从未设置；每次失败调用前后
    工单与优先级记录的数量和内容都必须完全一致（原等级不变）。
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

        self.id_a = self._create_ticket(
            TICKET_A["title"], TICKET_A["description"]
        )
        self.id_b = self._create_ticket(
            TICKET_B["title"], TICKET_B["description"]
        )
        closed = self.run_cli("close", str(self.id_b))
        self.assertEqual(closed.returncode, 0, f"准备样例失败: {closed.stderr}")
        self.assertEqual(closed.stderr, "")

        # 预置等级：工单 1 为 high，工单 2 未设置
        saved = self.run_cli("set-priority", str(self.id_a), "--value", "high")
        self.assertEqual(saved.returncode, 0, f"准备样例失败: {saved.stderr}")
        self.assertEqual(saved.stderr, "")

        self.expected_ticket_rows = [
            [self.id_a, TICKET_A["title"], TICKET_A["description"], "open"],
            [self.id_b, TICKET_B["title"], TICKET_B["description"], "closed"],
        ]
        self.expected_priority_rows = [(self.id_a, "high")]
        self.assert_storage_unchanged()

    # ---------- 辅助方法 ----------

    def run_cli(self, *args):
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

    def priority_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT ticket_id, priority FROM ticket_priorities "
                "ORDER BY ticket_id"
            ).fetchall()
        finally:
            conn.close()

    def assert_storage_unchanged(self):
        """失败调用不新增或修改任何工单与优先级记录。"""
        self.assertEqual(
            [list(row) for row in self.ticket_rows()],
            self.expected_ticket_rows,
        )
        self.assertEqual(self.priority_rows(), self.expected_priority_rows)

    def assert_business_error(self, argv, message):
        """退出码 1、标准输出为空、标准错误仅为提示语及换行。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 1, f"argv={argv!r}")
        self.assertEqual(result.stdout, "", f"argv={argv!r}")
        self.assertEqual(result.stderr, message + "\n", f"argv={argv!r}")
        self.assert_storage_unchanged()

    def assert_usage_error(self, argv):
        """退出码 2、标准输出为空、标准错误包含用法提示，原等级不变。"""
        result = self.run_cli(*argv)
        self.assertEqual(result.returncode, 2, f"argv={argv!r}: {result.stderr}")
        self.assertEqual(result.stdout, "", f"argv={argv!r}")
        self.assertIn("usage", result.stderr.lower(), f"argv={argv!r}")
        self.assert_storage_unchanged()

    # ---------- 编号不是正整数 ----------

    def test_non_positive_integer_ids_rejected_for_both_commands(self):
        for command in ("set-priority", "priority"):
            for bad_id in ("abc", "0", "-1"):
                with self.subTest(command=command, bad_id=bad_id):
                    if command == "set-priority":
                        argv = (command, bad_id, "--value", "high")
                    else:
                        argv = (command, bad_id)
                    self.assert_business_error(argv, "工单编号必须为正整数")

    # ---------- 编号为正整数但工单不存在（含超过 SQLite 整数上限） ----------

    def test_missing_ticket_reported_for_both_commands(self):
        for missing_id in ("999", OVERFLOW_ID):
            with self.subTest(missing_id=missing_id):
                self.assert_business_error(
                    ("priority", missing_id), "工单不存在"
                )
                self.assert_business_error(
                    ("set-priority", missing_id, "--value", "high"),
                    "工单不存在",
                )

    # ---------- 用法错误：退出码 2 ----------

    def test_usage_errors_exit_with_code_2(self):
        usage_cases = [
            # priority 缺少工单编号
            ("priority",),
            # priority 携带未知选项
            ("priority", str(self.id_a), "--unknown"),
            # set-priority 同时缺少编号与 --value
            ("set-priority",),
            # set-priority 缺少 --value
            ("set-priority", str(self.id_a)),
            # set-priority 缺少工单编号
            ("set-priority", "--value", "high"),
            # --value 缺少取值
            ("set-priority", str(self.id_a), "--value"),
            # 等级区分大小写：HIGH 不是合法取值
            ("set-priority", str(self.id_a), "--value", "HIGH"),
            # 空字符串不是合法取值
            ("set-priority", str(self.id_a), "--value", ""),
            # 带首尾空白的等级不做修剪，按非法取值拒绝
            ("set-priority", str(self.id_a), "--value", " high "),
            # set-priority 携带未知选项
            (
                "set-priority",
                str(self.id_a),
                "--value",
                "high",
                "--unknown",
            ),
        ]
        for argv in usage_cases:
            with self.subTest(argv=argv):
                self.assert_usage_error(argv)

        # 所有用法错误之后：工单 1 仍为预置的 high，工单 2 仍无记录
        result = self.run_cli("priority", str(self.id_a))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {"ticket_id": self.id_a, "priority": "high"},
        )
        result = self.run_cli("priority", str(self.id_b))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {"ticket_id": self.id_b, "priority": "normal"},
        )
        self.assert_storage_unchanged()


class PriorityLegacyDatabaseTestCase(unittest.TestCase):
    """旧库无需重建：不含优先级表的既有库可直接读取为 normal。

    允许产品按既有规则补建缺失的空表，但不得改动原工单，也不得产生
    占位等级记录；实际设置 high 后，跨次调用必须仍读到 high。
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "legacy.sqlite")

        # 手工构造只含工单表的旧库，预置编号 1 的 open 工单
        conn = sqlite3.connect(self.db_path)
        try:
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
                "INSERT INTO tickets (id, title, description, status) "
                "VALUES (?, ?, ?, ?)",
                (1, TICKET_A["title"], TICKET_A["description"], "open"),
            )
            conn.commit()
        finally:
            conn.close()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    def table_rows(self, sql):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_legacy_database_reads_normal_then_persists_high(self):
        # 旧库直接读取：成功且为 normal，标准错误为空
        result = self.run_cli("priority", "1")
        self.assertEqual(result.returncode, 0, f"priority 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        read_value = json.loads(result.stdout)
        self.assertEqual(read_value, {"ticket_id": 1, "priority": "normal"})
        self.assertIs(type(read_value["ticket_id"]), int)
        self.assertIs(type(read_value["priority"]), str)

        # 只读不落库：补建出的优先级表仍为空，原工单不变
        self.assertEqual(
            self.table_rows(
                "SELECT ticket_id, priority FROM ticket_priorities"
            ),
            [],
        )
        self.assertEqual(
            self.table_rows(
                "SELECT id, title, description, status FROM tickets"
            ),
            [(1, TICKET_A["title"], TICKET_A["description"], "open")],
        )

        # 旧库设置 high：返回同一对象
        saved = self.run_cli("set-priority", "1", "--value", "high")
        self.assertEqual(saved.returncode, 0, f"set-priority 失败: {saved.stderr}")
        self.assertEqual(saved.stderr, "")
        self.assertEqual(
            json.loads(saved.stdout), {"ticket_id": 1, "priority": "high"}
        )

        # 另一次独立进程调用仍读到 high
        reread = self.run_cli("priority", "1")
        self.assertEqual(reread.returncode, 0, f"priority 失败: {reread.stderr}")
        self.assertEqual(reread.stderr, "")
        self.assertEqual(
            json.loads(reread.stdout), {"ticket_id": 1, "priority": "high"}
        )

        # 每工单至多一行；备注、历史、草稿等补建表保持为空；原工单不变
        self.assertEqual(
            self.table_rows(
                "SELECT ticket_id, priority FROM ticket_priorities"
            ),
            [(1, "high")],
        )
        for table, sql in (
            ("ticket_notes", "SELECT id, ticket_id, note_id, text FROM ticket_notes"),
            (
                "ticket_history",
                "SELECT id, ticket_id, event_id, from_status, to_status "
                "FROM ticket_history",
            ),
            ("ticket_drafts", "SELECT id, ticket_id, text FROM ticket_drafts"),
        ):
            self.assertEqual(
                self.table_rows(sql), [], f"旧库 {table} 不应出现记录"
            )
        self.assertEqual(
            self.table_rows(
                "SELECT id, title, description, status FROM tickets"
            ),
            [(1, TICKET_A["title"], TICKET_A["description"], "open")],
        )


if __name__ == "__main__":
    unittest.main()
