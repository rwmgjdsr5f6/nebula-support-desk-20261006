"""本地客服工单中心命令行入口。

支持工单的创建、查看与结案，数据存储于本地 SQLite 文件。
"""

import argparse
import json
import sqlite3
import sys

DEFAULT_DB = "tickets.sqlite"


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


def parse_ticket_id(raw_id):
    """将命令行编号参数解析为正整数，无法解析或非正数时返回 None。"""
    try:
        ticket_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    if ticket_id <= 0:
        return None
    return ticket_id


def fetch_ticket(conn, raw_id):
    """按编号取出工单字典；编号非法或工单不存在时打印错误并返回 None。"""
    ticket_id = parse_ticket_id(raw_id)
    if ticket_id is None:
        print("工单编号必须为正整数", file=sys.stderr)
        return None

    row = conn.execute(
        "SELECT id, title, description, status FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if row is None:
        print("工单不存在", file=sys.stderr)
        return None

    return {
        "id": row[0],
        "title": row[1],
        "description": row[2],
        "status": row[3],
    }


def show_ticket(conn, raw_id):
    ticket = fetch_ticket(conn, raw_id)
    if ticket is None:
        return 1

    print_ticket(ticket)
    return 0


def close_ticket(conn, raw_id):
    ticket = fetch_ticket(conn, raw_id)
    if ticket is None:
        return 1

    conn.execute("UPDATE tickets SET status = ? WHERE id = ?", ("closed", ticket["id"]))
    conn.commit()

    ticket["status"] = "closed"
    print_ticket(ticket)
    return 0


def list_tickets(conn, status):
    if status is None:
        rows = conn.execute(
            "SELECT id, title, description, status FROM tickets ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, description, status FROM tickets "
            "WHERE status = ? ORDER BY id",
            (status,),
        ).fetchall()

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

    list_parser = subparsers.add_parser("list", help="列出工单")
    list_parser.add_argument(
        "--status",
        choices=["open", "closed"],
        default=None,
        help="按状态筛选（open 或 closed；不传则返回全部工单）",
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
        if args.command == "list":
            return list_tickets(conn, args.status)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
