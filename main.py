#!/usr/bin/env python3
"""本地客服工单中心命令行入口。

仅支持两个子命令：
    create  登记一条工单
    show    按编号查看工单

数据存储在本地 SQLite 文件中，默认使用当前工作目录下的 tickets.sqlite。
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = "tickets.sqlite"


def connect(db_path):
    """打开（必要时创建）数据库并确保表结构存在。"""
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


def ticket_to_json(row):
    return {
        "id": row[0],
        "title": row[1],
        "description": row[2],
        "status": row[3],
    }


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
    row = conn.execute(
        "SELECT id, title, description, status FROM tickets WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    print(json.dumps(ticket_to_json(row), ensure_ascii=False))
    return 0


def show_ticket(conn, raw_id):
    try:
        ticket_id = int(raw_id)
        if ticket_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        print("工单编号必须为正整数", file=sys.stderr)
        return 1

    row = conn.execute(
        "SELECT id, title, description, status FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if row is None:
        print("工单不存在", file=sys.stderr)
        return 1

    print(json.dumps(ticket_to_json(row), ensure_ascii=False))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="本地客服工单中心")
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help="SQLite 数据库文件路径（默认：当前工作目录下的 tickets.sqlite）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="登记一条工单")
    create_parser.add_argument("--title", required=True, help="工单标题")
    create_parser.add_argument(
        "--description", default="", help="问题描述（可省略或传空字符串）"
    )

    show_parser = subparsers.add_parser("show", help="按编号查看工单")
    show_parser.add_argument("id", help="工单编号（正整数）")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    db_path = Path(args.db)
    if not db_path.parent.exists():
        print(f"数据库父目录不存在: {db_path.parent}", file=sys.stderr)
        return 1

    conn = connect(str(db_path))
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
