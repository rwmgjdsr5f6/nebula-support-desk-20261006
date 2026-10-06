"""本地客服工单中心命令行入口。

支持工单的创建、查看与结案，数据存储于本地 SQLite 文件。
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
    """将工单状态更新为指定值；close 与 reopen 共用此流程。"""
    row = find_ticket(conn, raw_id)
    if row is None:
        return 1

    conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, row[0]))
    conn.commit()

    ticket = row_to_ticket(row)
    ticket["status"] = status
    print_ticket(ticket)
    return 0


def close_ticket(conn, raw_id):
    return set_ticket_status(conn, raw_id, "closed")


def reopen_ticket(conn, raw_id):
    return set_ticket_status(conn, raw_id, "open")


def list_tickets(conn, status, keyword):
    # 关键字先去除首尾空白；显式传入空字符串或去除后为空时，
    # 按使用错误处理（退出码 1），不进入查询
    if keyword is not None:
        keyword = keyword.strip()
        if not keyword:
            print("关键字不能为空", file=sys.stderr)
            return 1

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
    print(json.dumps(tickets, ensure_ascii=False))
    return 0


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
        if args.command == "list":
            return list_tickets(conn, args.status, args.keyword)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
