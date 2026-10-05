"""本地客服工单中心命令行入口。

仅支持工单的创建与查看，数据存储于本地 SQLite 文件。
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


def show_ticket(conn, raw_id):
    try:
        ticket_id = int(raw_id)
    except (TypeError, ValueError):
        ticket_id = None
    if ticket_id is None or ticket_id <= 0:
        print("工单编号必须为正整数", file=sys.stderr)
        return 1

    row = conn.execute(
        "SELECT id, title, description, status FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if row is None:
        print("工单不存在", file=sys.stderr)
        return 1

    print_ticket(
        {
            "id": row[0],
            "title": row[1],
            "description": row[2],
            "status": row[3],
        }
    )
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
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
