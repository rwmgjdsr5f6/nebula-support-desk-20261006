"""set-priority 与 priority 命令的回归测试。

通过真实命令行入口（python main.py）核对工单优先级的设置与读取：
固定样例从空库创建编号 1 的 open“登录失败”工单与编号 2 的 closed
“打印异常”工单；覆盖初次读取按 normal 返回且不落库、设置 high/low/normal
及重复设置后的跨进程读取、open 与 closed 工单彼此独立、设置不改变工单、
备注、草稿与状态历史，以及编号校验（沿用 show）与参数用法错误的确定结果；
另手工构造缺少优先级表的旧库，核对直接读取为 normal、设置后跨次调用仍
读到 high，且原工单不变。

每个用例使用独立临时目录，通过 --db 明确指定临时 SQLite 文件，
测试结束后清理，不读取或改动工作目录下的默认库 tickets.sqlite。
比较一律基于解析后的 JSON 值与字段类型，不依赖 JSON 键顺序或排版空格。

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

# 固定样例：编号 1 为 open，编号 2 经 close 后为 closed
TICKET_A = {"id": 1, "title": "登录失败", "description": "重置密码后仍无法登录"}
TICKET_B = {"id": 2, "title": "打印异常", "description": "更换网络后无法打印"}

PRIORITY_KEYS = {"ticket_id", "priority"}

# 超过 SQLite 有符号整数上限的编号：按工单不存在处理
OVERFLOW_ID = "9223372036854775808"

# 与当前版本相同的 tickets 表结构：旧库只是“缺少优先级表”，工单表保持一致
TICKETS_DDL = """
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL
)
"""


class PriorityTestMixin:
    """共用的临时库、命令调用、JSON 解析与存量核对辅助。"""

    def make_temp_db(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = str(Path(self._tmpdir.name) / "test_tickets.sqlite")

    def run_cli(self, *args):
        """以真实命令行入口运行 main.py，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, str(MAIN_PY), "--db", self.db_path, *args],
            capture_output=True,
            text=True,
        )

    @staticmethod
    def parse_single_json_object(stdout):
        """标准输出必须恰好是一个 JSON 对象，不允许前后夹带说明文字。"""
        decoder = json.JSONDecoder()
        stripped = stdout.lstrip()
        value, end = decoder.raw_decode(stripped)
        if not isinstance(value, dict) or stripped[end:].strip() != "":
            raise AssertionError(f"标准输出不是唯一 JSON 对象: {stdout!r}")
        return value

    def assertPriorityObject(self, obj, ticket_id, priority):
        """仅含整数 ticket_id 与字符串 priority，且值与期望一致。"""
        self.assertIsInstance(obj, dict, f"priority 输出不是对象: {obj!r}")
        self.assertEqual(set(obj.keys()), PRIORITY_KEYS)
        # 明确要求 int / str 类型（bool 虽是 int 子类但不合法）
        self.assertIs(type(obj["ticket_id"]), int)
        self.assertIs(type(obj["priority"]), str)
        self.assertEqual(obj, {"ticket_id": ticket_id, "priority": priority})

    def read_priority(self, raw_id, ticket_id=None):
        """执行 priority，要求成功并返回解析后的唯一优先级 JSON 对象。"""
        result = self.run_cli("priority", str(raw_id))
        self.assertEqual(
            result.returncode, 0, f"priority {raw_id!r} 失败: {result.stderr!r}"
        )
        self.assertEqual(result.stderr, "", f"priority {raw_id!r} 标准错误非空")
        return self.parse_single_json_object(result.stdout)

    def set_priority(self, raw_id, value):
        """执行 set-priority，要求成功并返回解析后的唯一优先级 JSON 对象。"""
        result = self.run_cli("set-priority", str(raw_id), "--value", value)
        self.assertEqual(
            result.returncode,
            0,
            f"set-priority {raw_id!r} --value {value!r} 失败: {result.stderr!r}",
        )
        self.assertEqual(result.stderr, "", "set-priority 成功时标准错误必须为空")
        return self.parse_single_json_object(result.stdout)

    def snapshot_tables(self):
        """导出各表全部行（含自增编号），用于逐表逐行比对。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return {
                "tickets": conn.execute(
                    "SELECT id, title, description, status FROM tickets ORDER BY id"
                ).fetchall(),
                "ticket_notes": conn.execute(
                    "SELECT id, ticket_id, note_id, text "
                    "FROM ticket_notes ORDER BY id"
                ).fetchall(),
                "ticket_drafts": conn.execute(
                    "SELECT id, ticket_id, text FROM ticket_drafts ORDER BY id"
                ).fetchall(),
                "ticket_history": conn.execute(
                    "SELECT id, ticket_id, event_id, from_status, to_status "
                    "FROM ticket_history ORDER BY id"
                ).fetchall(),
                "ticket_priorities": conn.execute(
                    "SELECT id, ticket_id, priority "
                    "FROM ticket_priorities ORDER BY id"
                ).fetchall(),
            }
        finally:
            conn.close()

    def priority_rows(self, ticket_id=None):
        conn = sqlite3.connect(self.db_path)
        try:
            if ticket_id is None:
                return conn.execute(
                    "SELECT ticket_id, priority FROM ticket_priorities ORDER BY id"
                ).fetchall()
            return conn.execute(
                "SELECT ticket_id, priority FROM ticket_priorities "
                "WHERE ticket_id = ? ORDER BY id",
                (ticket_id,),
            ).fetchall()
        finally:
            conn.close()

    def seed_two_tickets(self):
        """从空库创建固定样例：编号 1 open、编号 2 closed。"""
        first = self.run_cli(
            "create",
            "--title",
            TICKET_A["title"],
            "--description",
            TICKET_A["description"],
        )
        self.assertEqual(first.returncode, 0, f"准备样例失败: {first.stderr}")
        self.assertEqual(first.stderr, "")
        first_obj = self.parse_single_json_object(first.stdout)
        self.assertEqual(first_obj["id"], TICKET_A["id"])

        second = self.run_cli(
            "create",
            "--title",
            TICKET_B["title"],
            "--description",
            TICKET_B["description"],
        )
        self.assertEqual(second.returncode, 0, f"准备样例失败: {second.stderr}")
        self.assertEqual(second.stderr, "")
        second_obj = self.parse_single_json_object(second.stdout)
        self.assertEqual(second_obj["id"], TICKET_B["id"])

        closed = self.run_cli("close", str(TICKET_B["id"]))
        self.assertEqual(closed.returncode, 0, f"准备样例失败: {closed.stderr}")
        self.assertEqual(closed.stderr, "")

    def expected_ticket_rows(self):
        return [
            (
                TICKET_A["id"],
                TICKET_A["title"],
                TICKET_A["description"],
                "open",
            ),
            (
                TICKET_B["id"],
                TICKET_B["title"],
                TICKET_B["description"],
                "closed",
            ),
        ]


class PriorityWorkflowTestCase(PriorityTestMixin, unittest.TestCase):
    """优先级设置与读取主流程；每个用例使用独立临时数据库。"""

    def setUp(self):
        self.make_temp_db()
        self.seed_two_tickets()

    # ---------- 初次读取 ----------

    def test_initial_read_returns_normal_and_creates_no_record(self):
        for ticket_id in (TICKET_A["id"], TICKET_B["id"]):
            with self.subTest(command="priority", ticket_id=ticket_id):
                obj = self.read_priority(ticket_id)
                self.assertPriorityObject(obj, ticket_id, "normal")

        # 未设置等级的读取是只读操作：不新增任何优先级记录
        self.assertEqual(self.priority_rows(), [])

    # ---------- 设置并跨进程读取 ----------

    def test_set_returns_same_object_as_following_read(self):
        set_result = self.run_cli(
            "set-priority", str(TICKET_A["id"]), "--value", "high"
        )
        self.assertEqual(set_result.returncode, 0)
        self.assertEqual(set_result.stderr, "")
        set_obj = self.parse_single_json_object(set_result.stdout)
        self.assertPriorityObject(set_obj, TICKET_A["id"], "high")

        # 另一次全新进程调用 priority 1 必须读到同形状同值的对象
        read_obj = self.read_priority(TICKET_A["id"])
        self.assertPriorityObject(read_obj, TICKET_A["id"], "high")
        self.assertEqual(read_obj, set_obj)

        # 只影响被设置的工单：工单 2 仍为 normal，且没有它的优先级记录
        self.assertPriorityObject(
            self.read_priority(TICKET_B["id"]), TICKET_B["id"], "normal"
        )
        self.assertEqual(self.priority_rows(), [(TICKET_A["id"], "high")])

    # ---------- 改级、恢复与重复设置 ----------

    def test_change_restore_and_repeat_set_keep_last_value(self):
        sequence = ("high", "low", "normal", "normal", "high", "high", "low")
        for value in sequence:
            with self.subTest(command="set-priority", value=value):
                set_obj = self.set_priority(TICKET_A["id"], value)
                self.assertPriorityObject(set_obj, TICKET_A["id"], value)
                # 每次设置后另一次调用读取，始终返回最后设置值
                self.assertPriorityObject(
                    self.read_priority(TICKET_A["id"]), TICKET_A["id"], value
                )

        # 整篇覆盖：反复设置后该工单在表中仍只有一行，值为最后一次的 low
        self.assertEqual(self.priority_rows(TICKET_A["id"]), [(TICKET_A["id"], "low")])

    # ---------- 两张工单互不影响，closed 工单也可设置 ----------

    def test_open_and_closed_tickets_keep_independent_priorities(self):
        # 工单 2 是 closed 工单，同样可以设置
        self.assertPriorityObject(
            self.set_priority(TICKET_B["id"], "high"), TICKET_B["id"], "high"
        )
        self.assertPriorityObject(
            self.set_priority(TICKET_A["id"], "low"), TICKET_A["id"], "low"
        )
        self.assertPriorityObject(
            self.read_priority(TICKET_A["id"]), TICKET_A["id"], "low"
        )
        self.assertPriorityObject(
            self.read_priority(TICKET_B["id"]), TICKET_B["id"], "high"
        )

        # 改动工单 1 不波及工单 2，反之亦然
        self.assertPriorityObject(
            self.set_priority(TICKET_A["id"], "normal"), TICKET_A["id"], "normal"
        )
        self.assertPriorityObject(
            self.read_priority(TICKET_B["id"]), TICKET_B["id"], "high"
        )
        self.assertPriorityObject(
            self.set_priority(TICKET_B["id"], "low"), TICKET_B["id"], "low"
        )
        self.assertPriorityObject(
            self.read_priority(TICKET_A["id"]), TICKET_A["id"], "normal"
        )

        self.assertEqual(
            self.priority_rows(),
            [(TICKET_A["id"], "normal"), (TICKET_B["id"], "low")],
        )

    # ---------- 不改动工单、备注、草稿、历史；视图结构不变 ----------

    def test_setting_priority_keeps_other_records_and_views_intact(self):
        # 先为工单 1 准备备注与草稿；工单 2 在 setUp 的 close 已产生一条历史
        note = self.run_cli(
            "add-note", str(TICKET_A["id"]), "--text", "已联系用户，等待确认"
        )
        self.assertEqual(note.returncode, 0, f"准备备注失败: {note.stderr}")
        draft = self.run_cli(
            "set-draft", str(TICKET_A["id"]), "--text", " 请重试登录 "
        )
        self.assertEqual(draft.returncode, 0, f"准备草稿失败: {draft.stderr}")

        before = self.snapshot_tables()

        for ticket_id, value in (
            (TICKET_A["id"], "high"),
            (TICKET_B["id"], "low"),
            (TICKET_A["id"], "normal"),
            (TICKET_B["id"], "high"),
        ):
            set_obj = self.set_priority(ticket_id, value)
            self.assertPriorityObject(set_obj, ticket_id, value)

        after = self.snapshot_tables()

        # 工单、备注、草稿、状态历史四张表逐行不变（含自增编号）
        for table in (
            "tickets",
            "ticket_notes",
            "ticket_drafts",
            "ticket_history",
        ):
            self.assertEqual(
                after[table],
                before[table],
                f"set-priority 改动了 {table}: "
                f"{before[table]!r} != {after[table]!r}",
            )
        self.assertEqual(after["tickets"], self.expected_ticket_rows())

        # 两张工单在优先级表中各至多一行。INSERT OR REPLACE 会重建行，
        # 内部自增 id 不属于对外契约，只逐行核对 (ticket_id, priority)
        self.assertEqual(
            [(row[1], row[2]) for row in after["ticket_priorities"]],
            [
                (TICKET_A["id"], "normal"),
                (TICKET_B["id"], "high"),
            ],
        )

        # show 仍只返回原四字段对象，内容与设置前完全一致
        for ticket in (TICKET_A, TICKET_B):
            show = self.run_cli("show", str(ticket["id"]))
            self.assertEqual(show.returncode, 0, f"show 失败: {show.stderr}")
            self.assertEqual(show.stderr, "")
            shown = self.parse_single_json_object(show.stdout)
            self.assertEqual(
                set(shown.keys()), {"id", "title", "description", "status"}
            )
            expected_status = "open" if ticket["id"] == TICKET_A["id"] else "closed"
            self.assertEqual(
                shown,
                {
                    "id": ticket["id"],
                    "title": ticket["title"],
                    "description": ticket["description"],
                    "status": expected_status,
                },
            )

        # list 仍只返回两张原四字段结构的工单，按编号升序
        listed = self.run_cli("list")
        self.assertEqual(listed.returncode, 0, f"list 失败: {listed.stderr}")
        self.assertEqual(listed.stderr, "")
        tickets = json.loads(listed.stdout)
        self.assertIsInstance(tickets, list)
        self.assertEqual(len(tickets), 2)
        for ticket in tickets:
            self.assertEqual(
                set(ticket.keys()), {"id", "title", "description", "status"}
            )
        self.assertEqual(
            [(t["id"], t["status"]) for t in tickets],
            [(TICKET_A["id"], "open"), (TICKET_B["id"], "closed")],
        )


class PriorityIdValidationTestCase(PriorityTestMixin, unittest.TestCase):
    """两个入口的编号校验沿用 show；每个用例使用独立临时数据库。"""

    def setUp(self):
        self.make_temp_db()
        self.seed_two_tickets()

    def assert_failure(
        self, argv, returncode, expected_stderr, before_tables
    ):
        """失败调用：退出码与标准错误精确匹配，标准输出为空，存量不变。"""
        result = self.run_cli(*argv)
        self.assertEqual(
            result.returncode,
            returncode,
            f"命令 {argv!r} 退出码不符: {result.returncode} "
            f"(stderr={result.stderr!r})",
        )
        self.assertEqual(result.stdout, "", f"命令 {argv!r} 标准输出应为空")
        self.assertEqual(
            result.stderr,
            expected_stderr,
            f"命令 {argv!r} 标准错误与预期不符",
        )
        after = self.snapshot_tables()
        self.assertEqual(
            after,
            before_tables,
            f"命令 {argv!r} 改动了库内数据: {before_tables!r} != {after!r}",
        )

    # ---------- 正号、前导零与首尾空白仍指向同一工单 ----------

    def test_equivalent_id_spellings_point_to_same_ticket(self):
        read_spellings = ("+1", "01", "  1  ", "\t+01\n", "+2", "02")
        for raw_id in read_spellings:
            with self.subTest(command="priority", raw_id=raw_id):
                result = self.run_cli("priority", raw_id)
                self.assertEqual(
                    result.returncode, 0, f"priority {raw_id!r}: {result.stderr!r}"
                )
                self.assertEqual(result.stderr, "")
                obj = self.parse_single_json_object(result.stdout)
                self.assertEqual(
                    obj["ticket_id"], int(raw_id), f"priority {raw_id!r} 指向错误工单"
                )

        # 同一编号的不同写法用于设置时同样落到工单 1
        writes = (("+1", "high"), ("01", "low"), ("  1  ", "normal"), ("\t+01\n", "high"))
        for raw_id, value in writes:
            with self.subTest(command="set-priority", raw_id=raw_id, value=value):
                result = self.run_cli("set-priority", raw_id, "--value", value)
                self.assertEqual(
                    result.returncode,
                    0,
                    f"set-priority {raw_id!r} --value {value!r}: {result.stderr!r}",
                )
                self.assertEqual(result.stderr, "")
                obj = self.parse_single_json_object(result.stdout)
                self.assertPriorityObject(obj, TICKET_A["id"], value)
                self.assertPriorityObject(
                    self.read_priority(TICKET_A["id"]), TICKET_A["id"], value
                )

        # 工单 2 始终未被这些写法波及
        self.assertPriorityObject(
            self.read_priority(TICKET_B["id"]), TICKET_B["id"], "normal"
        )
        self.assertEqual(self.priority_rows(TICKET_B["id"]), [])

    # ---------- 编号格式错误 ----------

    def test_non_positive_integer_ids_exit_1(self):
        before = self.snapshot_tables()
        for raw_id in ("abc", "0", "-1"):
            for argv in (
                ("priority", raw_id),
                ("set-priority", raw_id, "--value", "high"),
            ):
                with self.subTest(argv=argv):
                    self.assert_failure(
                        argv, 1, "工单编号必须为正整数\n", before
                    )

        # 失败的设置不写入任何优先级
        self.assertEqual(self.priority_rows(), [])

    # ---------- 编号不存在 ----------

    def test_missing_tickets_exit_1(self):
        before = self.snapshot_tables()
        for raw_id in ("999", OVERFLOW_ID):
            for argv in (
                ("priority", raw_id),
                ("set-priority", raw_id, "--value", "high"),
            ):
                with self.subTest(argv=argv):
                    self.assert_failure(argv, 1, "工单不存在\n", before)

        # 工单表仍只有两张固定样例
        self.assertEqual(before["tickets"], self.expected_ticket_rows())
        self.assertEqual(self.priority_rows(), [])


class PriorityUsageTestCase(PriorityTestMixin, unittest.TestCase):
    """用法错误退出 2：缺编号、缺 --value、非法等级、未知选项。"""

    def setUp(self):
        self.make_temp_db()
        self.seed_two_tickets()
        # 先把工单 1 置为 high：任何用法错误后原等级必须保持不变
        prepared = self.run_cli(
            "set-priority", str(TICKET_A["id"]), "--value", "high"
        )
        self.assertEqual(prepared.returncode, 0, f"准备样例失败: {prepared.stderr}")
        self.assertEqual(prepared.stderr, "")

    def test_usage_errors_exit_2_and_keep_priority_unchanged(self):
        usages = (
            ("priority",),
            ("set-priority",),
            ("set-priority", "--value", "high"),
            ("set-priority", str(TICKET_A["id"])),
            ("set-priority", str(TICKET_A["id"]), "--value"),
            ("set-priority", str(TICKET_A["id"]), "--value", "HIGH"),
            ("set-priority", str(TICKET_A["id"]), "--value", ""),
            ("set-priority", str(TICKET_A["id"]), "--value", " high "),
            ("priority", str(TICKET_A["id"]), "--unknown"),
            (
                "set-priority",
                str(TICKET_A["id"]),
                "--value",
                "high",
                "--unknown",
            ),
        )
        for argv in usages:
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(
                    result.returncode,
                    2,
                    f"命令 {argv!r} 应退出 2，实际 {result.returncode} "
                    f"(stderr={result.stderr!r})",
                )
                self.assertEqual(result.stdout, "", f"命令 {argv!r} 标准输出应为空")
                self.assertIn(
                    "usage",
                    result.stderr.lower(),
                    f"命令 {argv!r} 标准错误缺少用法提示: {result.stderr!r}",
                )

                # 原等级不变：工单 1 仍为 high，工单 2 仍无记录
                self.assertPriorityObject(
                    self.read_priority(TICKET_A["id"]), TICKET_A["id"], "high"
                )
                self.assertEqual(
                    self.priority_rows(),
                    [(TICKET_A["id"], "high")],
                    f"命令 {argv!r} 后优先级记录与预期不符",
                )


def build_legacy_db_without_priority_table(path):
    """构造旧版库：只有 tickets 表及两条固定工单，没有任何优先级表。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(TICKETS_DDL)
        conn.executemany(
            "INSERT INTO tickets (id, title, description, status) "
            "VALUES (?, ?, ?, ?)",
            [
                (
                    TICKET_A["id"],
                    TICKET_A["title"],
                    TICKET_A["description"],
                    "open",
                ),
                (
                    TICKET_B["id"],
                    TICKET_B["title"],
                    TICKET_B["description"],
                    "closed",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


class LegacyPriorityDbTestCase(PriorityTestMixin, unittest.TestCase):
    """缺少优先级表但保留原工单的旧库兼容。"""

    def setUp(self):
        self.make_temp_db()
        build_legacy_db_without_priority_table(self.db_path)

    def test_read_is_normal_without_priority_table(self):
        for ticket_id in (TICKET_A["id"], TICKET_B["id"]):
            with self.subTest(command="priority", ticket_id=ticket_id):
                obj = self.read_priority(ticket_id)
                self.assertPriorityObject(obj, ticket_id, "normal")

        # 读取不补占位记录；原工单逐行不变
        self.assertEqual(self.priority_rows(), [])
        conn = sqlite3.connect(self.db_path)
        try:
            tickets = conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(tickets, self.expected_ticket_rows())

    def test_set_high_persists_across_processes_without_changing_tickets(self):
        # 直接设置 high：设置时返回 high
        set_obj = self.set_priority(TICKET_A["id"], "high")
        self.assertPriorityObject(set_obj, TICKET_A["id"], "high")

        # 另一次全新进程调用仍读到 high；工单 2 仍为 normal
        self.assertPriorityObject(
            self.read_priority(TICKET_A["id"]), TICKET_A["id"], "high"
        )
        self.assertPriorityObject(
            self.read_priority(TICKET_B["id"]), TICKET_B["id"], "normal"
        )

        # 原工单逐行不变；优先级表只新增工单 1 的一行 high
        conn = sqlite3.connect(self.db_path)
        try:
            tickets = conn.execute(
                "SELECT id, title, description, status FROM tickets ORDER BY id"
            ).fetchall()
            priorities = conn.execute(
                "SELECT ticket_id, priority FROM ticket_priorities ORDER BY ticket_id"
            ).fetchall()
            notes = conn.execute("SELECT COUNT(*) FROM ticket_notes").fetchone()[0]
            drafts = conn.execute("SELECT COUNT(*) FROM ticket_drafts").fetchone()[0]
            history = conn.execute(
                "SELECT COUNT(*) FROM ticket_history"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(tickets, self.expected_ticket_rows())
        self.assertEqual(priorities, [(TICKET_A["id"], "high")])
        self.assertEqual((notes, drafts, history), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
