"""本地客服工单中心命令行入口。

支持工单的创建、查看、结案、重开、内部备注与列表筛选，数据存储于本地 SQLite 文件。
"""

import argparse
import json
import sqlite3
import sys

DEFAULT_DB = "tickets.sqlite"

# SQLite INTEGER 主键可保存的最大有符号整数；超过该值的编号不可能命中任何记录
SQLITE_MAX_INT = 9223372036854775807


def connect(db_path):
    """打开（必要时创建）数据库并确保工单表存在。"""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
    # 备注编号在每张工单内从 1 开始递增：UNIQUE(ticket_id, note_id)
    # 兜底，note_id 由代码按该工单当前最大值加 1 计算
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ticket_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            note_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            UNIQUE (ticket_id, note_id)
        )
        """
    )
    # 状态变更历史同样按工单独立编号：仅记录实际发生的状态切换，
    # 创建产生的初始 open 与重复结案/重开均不写入
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ticket_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            from_status TEXT NOT NULL,
            to_status TEXT NOT NULL,
            UNIQUE (ticket_id, event_id)
        )
        """
    )
    conn.commit()
    return conn


def print_ticket(ticket):
    """将工单作为唯一 JSON 对象输出到标准输出。"""
    print(json.dumps(ticket, ensure_ascii=False))


def create_ticket(conn, title, description):
    title = title.strip()
    if not title:
        print("标题不能为空", file=sys.stderr)
        return 1

    cursor = conn.execute(
        "INSERT INTO tickets (title, description, status) VALUES (?, ?, ?)",
        (title, description, "open"),
    )
    conn.commit()
    print_ticket(
        {
            "id": cursor.lastrowid,
            "title": title,
            "description": description,
            "status": "open",
        }
    )
    return 0


def find_ticket(conn, raw_id):
    """校验编号并查找工单；失败时打印错误并返回 None。"""
    try:
        ticket_id = int(raw_id)
    except (TypeError, ValueError):
        ticket_id = None
    if ticket_id is None or ticket_id <= 0:
        print("工单编号必须为正整数", file=sys.stderr)
        return None
    if ticket_id > SQLITE_MAX_INT:
        # 超过 SQLite 可保存的最大整数时不可能存在对应记录，
        # 直接按不存在处理，避免绑定参数触发 OverflowError
        print("工单不存在", file=sys.stderr)
        return None

    row = conn.execute(
        "SELECT id, title, description, status FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if row is None:
        print("工单不存在", file=sys.stderr)
        return None
    return row


def row_to_ticket(row):
    """将查询行转换为对外输出的工单字典。"""
    return {
        "id": row[0],
        "title": row[1],
        "description": row[2],
        "status": row[3],
    }


def show_ticket(conn, raw_id):
    row = find_ticket(conn, raw_id)
    if row is None:
        return 1

    print_ticket(row_to_ticket(row))
    return 0


def set_ticket_status(conn, raw_id, status):
    """查找工单并将其状态更新为 status，输出更新后的工单。

    仅当状态实际变化（open ↔ closed）时才写库并追加一条历史；
    重复结案或重复重开仍成功返回当前工单，但不占用历史序号。
    """
    row = find_ticket(conn, raw_id)
    if row is None:
        return 1

    ticket_id = row[0]
    current_status = row[3]
    if current_status != status:
        conn.execute(
            "UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id)
        )
        row_max = conn.execute(
            "SELECT MAX(event_id) FROM ticket_history WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()
        next_event_id = (row_max[0] or 0) + 1
        conn.execute(
            """
            INSERT INTO ticket_history
                (ticket_id, event_id, from_status, to_status)
            VALUES (?, ?, ?, ?)
            """,
            (ticket_id, next_event_id, current_status, status),
        )
        conn.commit()

    ticket = row_to_ticket(row)
    ticket["status"] = status
    print_ticket(ticket)
    return 0


def close_ticket(conn, raw_id):
    return set_ticket_status(conn, raw_id, "closed")


def reopen_ticket(conn, raw_id):
    return set_ticket_status(conn, raw_id, "open")


def note_to_dict(ticket_id, note_id, text):
    """构造对外输出的备注字典：仅含 note_id、ticket_id、text。"""
    return {"note_id": note_id, "ticket_id": ticket_id, "text": text}


def add_note(conn, raw_id, text):
    """确认工单存在后追加一条内部备注，编号在该工单内从 1 递增。"""
    row = find_ticket(conn, raw_id)
    if row is None:
        return 1

    # 工单存在后才校验内容：空字符串或去除首尾空白后为空一律拒绝
    if text is None or not text.strip():
        print("备注不能为空", file=sys.stderr)
        return 1

    ticket_id = row[0]
    row_max = conn.execute(
        "SELECT MAX(note_id) FROM ticket_notes WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    next_note_id = (row_max[0] or 0) + 1
    conn.execute(
        "INSERT INTO ticket_notes (ticket_id, note_id, text) VALUES (?, ?, ?)",
        (ticket_id, next_note_id, text),
    )
    conn.commit()

    print(json.dumps(note_to_dict(ticket_id, next_note_id, text), ensure_ascii=False))
    return 0


def list_notes(conn, raw_id, keyword=None):
    """按 note_id 升序输出工单的备注；无备注或无命中时输出 []。

    不传 keyword 时返回全部备注；传入时先去除关键字首尾空白，再对备注
    正文做区分大小写的连续字面子串匹配（内部空白、%、_、引号与反斜杠
    均按原字符比较）。仅查询当前工单，其他工单的备注不参与。
    """
    row = find_ticket(conn, raw_id)
    if row is None:
        return 1

    # 工单存在后才校验关键字：显式空字符串或纯空白一律拒绝
    if keyword is not None:
        keyword = keyword.strip()
        if not keyword:
            print("关键字不能为空", file=sys.stderr)
            return 1

    ticket_id = row[0]
    rows = conn.execute(
        "SELECT note_id, text FROM ticket_notes WHERE ticket_id = ? ORDER BY note_id",
        (ticket_id,),
    ).fetchall()
    notes = [
        note_to_dict(ticket_id, note_row[0], note_row[1])
        for note_row in rows
        if keyword is None or keyword in note_row[1]
    ]
    print(json.dumps(notes, ensure_ascii=False))
    return 0


def history_event_to_dict(ticket_id, event_id, from_status, to_status):
    """构造对外输出的历史事件字典：仅含 event_id、ticket_id、from_status、to_status。"""
    return {
        "event_id": event_id,
        "ticket_id": ticket_id,
        "from_status": from_status,
        "to_status": to_status,
    }


def list_history(conn, raw_id):
    """按 event_id 升序输出工单的状态变更历史；无历史时输出 []。只读操作。"""
    row = find_ticket(conn, raw_id)
    if row is None:
        return 1

    ticket_id = row[0]
    rows = conn.execute(
        """
        SELECT event_id, from_status, to_status
        FROM ticket_history
        WHERE ticket_id = ?
        ORDER BY event_id
        """,
        (ticket_id,),
    ).fetchall()
    events = [
        history_event_to_dict(ticket_id, event_row[0], event_row[1], event_row[2])
        for event_row in rows
    ]
    print(json.dumps(events, ensure_ascii=False))
    return 0


def summary_ticket(conn, raw_id):
    """一次查询输出工单当前状态与已有处理记录的摘要；只读操作。

    摘要仅汇总本地记录：备注总数、最新备注（note_id 最大）与状态变更
    总数，不分析备注语义，也不推断问题是否解决。
    """
    row = find_ticket(conn, raw_id)
    if row is None:
        return 1

    ticket_id = row[0]
    note_count = conn.execute(
        "SELECT COUNT(*) FROM ticket_notes WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()[0]
    latest_row = conn.execute(
        "SELECT note_id, text FROM ticket_notes WHERE ticket_id = ? "
        "ORDER BY note_id DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    latest_note = (
        None
        if latest_row is None
        else note_to_dict(ticket_id, latest_row[0], latest_row[1])
    )
    status_change_count = conn.execute(
        "SELECT COUNT(*) FROM ticket_history WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()[0]

    print(
        json.dumps(
            {
                "ticket": row_to_ticket(row),
                "note_count": note_count,
                "latest_note": latest_note,
                "status_change_count": status_change_count,
            },
            ensure_ascii=False,
        )
    )
    return 0


def list_tickets(conn, status, keyword, note_keyword=None, limit=None, after_id=None):
    # 关键字先去除首尾空白；显式传入空字符串或去除后为空时，
    # 按使用错误处理（退出码 1），不进入查询
    if keyword is not None:
        keyword = keyword.strip()
        if not keyword:
            print("关键字不能为空", file=sys.stderr)
            return 1
    if note_keyword is not None:
        note_keyword = note_keyword.strip()
        if not note_keyword:
            print("关键字不能为空", file=sys.stderr)
            return 1

    # 游标只是编号边界：超过 SQLite 可保存的最大整数时不可能有编号
    # 严格大于它的记录，直接返回空数组，避免绑定参数触发 OverflowError
    if after_id is not None and after_id > SQLITE_MAX_INT:
        print("[]")
        return 0

    query = "SELECT id, title, description, status FROM tickets"
    conditions = []
    params = []
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if keyword:
        # 用 instr 做区分大小写的连续字面子串匹配：关键字作为绑定参数，
        # 其中的 %、_、\、引号与内部空白均按原字符比较，不作为通配符
        conditions.append("(instr(title, ?) > 0 OR instr(description, ?) > 0)")
        params.extend((keyword, keyword))
    if note_keyword:
        # 备注命中条件取 EXISTS：任意一条备注（含较早的备注）正文包含
        # 字面子串即可；多条备注命中只让工单入选一次，无备注工单不入选
        conditions.append(
            "EXISTS (SELECT 1 FROM ticket_notes "
            "WHERE ticket_notes.ticket_id = tickets.id "
            "AND instr(ticket_notes.text, ?) > 0)"
        )
        params.append(note_keyword)
    if after_id:
        # 游标与原有筛选条件取交集：只保留编号严格大于游标的工单；
        # 0 等价于未设置边界，不附加条件。游标不要求对应工单存在
        conditions.append("id > ?")
        params.append(after_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id"

    rows = conn.execute(query, params).fetchall()

    tickets = [
        {
            "id": row[0],
            "title": row[1],
            "description": row[2],
            "status": row[3],
        }
        for row in rows
    ]
    # 先按上述条件取交集并按编号整数升序得到全部匹配工单，再在 Python 侧
    # 截取前 limit 张：limit 为 None 时切片原样返回全部；数量超过匹配数时
    # 同样返回全部，不补齐。不在 SQL 中使用 LIMIT，以便数量超过 SQLite
    # 有符号整数上限时仍按此语义成功处理（Python int 无上限）
    tickets = tickets[:limit]
    print(json.dumps(tickets, ensure_ascii=False))
    return 0


def positive_limit(raw):
    """解析 list --limit 的数量：按 Python int() 规则，仅正整数有效。

    允许正号、前导零与首尾空白（与 int() 自身规则一致）；零、负数、
    小数、无法解析的文本或空字符串均抛错，由 argparse 按用法错误
    处理（退出码 2）。返回 Python 任意精度 int，即便数量超过 SQLite
    有符号整数上限也能正常用于后续切片。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须为正整数")
    if value <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return value


def non_negative_cursor(raw):
    """解析 list --after-id 的游标：按 Python int() 规则，非负整数有效。

    允许正号、前导零与首尾空白（与 int() 自身规则一致）；0 等价于未设置
    边界。负数、小数、无法解析的文本或空字符串均抛错，由 argparse 按用法
    错误处理（退出码 2）。返回 Python 任意精度 int，即便超过 SQLite
    有符号整数上限也能在查询前按“无更大编号”语义成功处理。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须为非负整数")
    if value < 0:
        raise argparse.ArgumentTypeError("必须为非负整数")
    return value


def build_parser():
    parser = argparse.ArgumentParser(description="本地客服工单中心")
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help="SQLite 数据库文件路径（默认：当前工作目录下的 tickets.sqlite）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="创建工单")
    create_parser.add_argument("--title", default="", help="工单标题")
    create_parser.add_argument(
        "--description", default="", help="工单描述（可为空字符串）"
    )

    show_parser = subparsers.add_parser("show", help="按编号查看工单")
    show_parser.add_argument("id", help="工单编号（正整数）")

    close_parser = subparsers.add_parser("close", help="按编号结案工单")
    close_parser.add_argument("id", help="工单编号（正整数）")

    reopen_parser = subparsers.add_parser("reopen", help="按编号重新打开工单")
    reopen_parser.add_argument("id", help="工单编号（正整数）")

    add_note_parser = subparsers.add_parser("add-note", help="为工单追加内部备注")
    add_note_parser.add_argument("id", help="工单编号（正整数）")
    add_note_parser.add_argument(
        "--text",
        required=True,
        help="备注内容（按原文保存；空字符串或去除首尾空白后为空时拒绝）",
    )

    notes_parser = subparsers.add_parser("notes", help="按编号列出工单的内部备注")
    notes_parser.add_argument("id", help="工单编号（正整数）")
    notes_parser.add_argument(
        "--keyword",
        default=None,
        help="只返回正文包含该关键字的备注（区分大小写的字面子串匹配）；不传则返回全部",
    )

    history_parser = subparsers.add_parser(
        "history", help="按编号列出工单的状态变更历史"
    )
    history_parser.add_argument("id", help="工单编号（正整数）")

    summary_parser = subparsers.add_parser(
        "summary", help="按编号读取工单处理摘要"
    )
    summary_parser.add_argument("id", help="工单编号（正整数）")

    list_parser = subparsers.add_parser("list", help="列出工单")
    list_parser.add_argument(
        "--status",
        choices=["open", "closed"],
        default=None,
        help="按状态筛选（open 或 closed；不传则返回全部工单）",
    )
    list_parser.add_argument(
        "--keyword",
        default=None,
        help="按标题或描述中的连续关键字筛选（区分大小写的字面子串匹配）",
    )
    list_parser.add_argument(
        "--note-keyword",
        default=None,
        help="按内部备注正文中的连续关键字筛选：任意一条备注命中即入选"
        "（区分大小写的字面子串匹配）；与 --status、--keyword 取交集",
    )
    list_parser.add_argument(
        "--limit",
        default=None,
        type=positive_limit,
        help="只返回匹配工单中编号最小的前若干张（正整数，允许正号、"
        "前导零与首尾空白）；不传则返回全部匹配工单",
    )
    list_parser.add_argument(
        "--after-id",
        default=None,
        type=non_negative_cursor,
        dest="after_id",
        help="只返回编号严格大于该值的匹配工单（非负整数，允许正号、"
        "前导零与首尾空白；0 等价于未设置边界）；用于从上次返回的"
        "最后一个编号继续翻页，在 --limit 之前生效",
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        if args.command == "create":
            return create_ticket(conn, args.title, args.description)
        if args.command == "show":
            return show_ticket(conn, args.id)
        if args.command == "close":
            return close_ticket(conn, args.id)
        if args.command == "reopen":
            return reopen_ticket(conn, args.id)
        if args.command == "add-note":
            return add_note(conn, args.id, args.text)
        if args.command == "notes":
            return list_notes(conn, args.id, args.keyword)
        if args.command == "history":
            return list_history(conn, args.id)
        if args.command == "summary":
            return summary_ticket(conn, args.id)
        if args.command == "list":
            return list_tickets(
                conn,
                args.status,
                args.keyword,
                args.note_keyword,
                args.limit,
                args.after_id,
            )
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
