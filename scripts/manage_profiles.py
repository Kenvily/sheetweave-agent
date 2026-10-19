"""Manage permanent report profiles through a menu or explicit subcommands."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from weekly_report_agent.profiles import ProfileStore
from weekly_report_agent.server import load_environment
from weekly_report_agent.task_queue import TaskQueue

ERRORS = (ValueError, OSError, sqlite3.Error)
LINE = "─" * 56


def timestamp(value: float | None) -> str:
    """Format a local timestamp, including its timezone."""
    return (
        datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")
        if value
        else "从未"
    )


def list_profiles(library: ProfileStore, *, keyword: str = "", unused_days: int | None = None):
    """Print numbered profiles with their actual use times and return that exact list."""
    if unused_days is not None and unused_days < 0:
        raise ValueError("未使用天数不能为负数。")
    items = [
        item
        for item in library.list()
        if keyword.casefold() in f"{item['id']} {item['name']}".casefold()
        and (
            unused_days is None
            or time.time() - (item["last_used"] or item["created"]) >= unused_days * 86400
        )
    ]
    print(f"\n方案列表 · 共 {len(items)} 项")
    print(LINE)
    for index, item in enumerate(items, 1):
        print(f"  {index}. {item['name']}  /  v{item['version']}  /  使用 {item['usage_count']} 次")
        print(f"     ID       {item['id']}")
        print(f"     创建     {timestamp(item['created'])}")
        print(f"     更新     {timestamp(item['updated'])}")
        print(f"     最近提交 {timestamp(item['last_used'])}")
        print(f"     最近成功 {timestamp(item['last_successful'])}")
        print()
    if not items:
        print("  没有符合条件的方案。")
    return items


def choose_profile(library: ProfileStore) -> str | None:
    """Choose by a displayed index or a complete ID; never reuse a stale index."""
    items = list_profiles(library)
    if not items:
        return None
    answer = input("输入方案序号或完整 ID（0 返回）：").strip()
    if answer in {"", "0"}:
        return None
    if answer.isdecimal():
        index = int(answer) - 1
        if not 0 <= index < len(items):
            raise ValueError("序号超出范围，请重新选择。")
        return items[index]["id"]
    return answer


def show_profile(library: ProfileStore, profile_id: str) -> None:
    """Show readable configuration summaries for each immutable version."""
    preset, _, metadata = library.load(profile_id)
    print(f"\n方案详情 · {preset.name}")
    print(LINE)
    print(f"  ID：{profile_id}    当前版本：v{metadata['version']}")
    for version in range(1, metadata["version"] + 1):
        rule, template, record = library.load(profile_id, version)
        print(f"\n  v{version} · {rule.name}")
        print(f"  说明：{record.get('description') or '无'}")
        print(f"  报告标题：{rule.document.report_title}")
        print(f"  输入布局：{rule.input.layout}    表头行数：{rule.input.header_rows}")
        print(f"  模板：{template}")
        print("  字段映射：")
        for key, aliases in rule.input.column_aliases.items():
            print(f"    {key} ← {' / '.join(aliases)}")
        print("  内容目录：")
        for section in rule.sections:
            print(f"    {' → '.join(section.heading_path) or section.title}")


def delete_profile(library: ProfileStore, profile_id: str) -> None:
    """Require the full ID and defer all active-task checks to the locked store API."""
    preset, _, metadata = library.load(profile_id)
    print(f"\n将删除 {preset.name} ({profile_id}) 的全部 {metadata['version']} 个版本及模板。")
    print("排队/执行中的引用会阻止删除；已生成的独立审核快照保留。")
    if input("请输入完整方案 ID 确认删除（回车取消）：").strip() != profile_id:
        print("已取消，未删除。")
        return
    library.delete(profile_id, TaskQueue.from_environment())
    print("已删除。")


def rename_profile(library: ProfileStore, profile_id: str, name: str | None = None) -> None:
    """Rename the selected profile; blank interactive input cancels the operation."""
    preset, _, metadata = library.load(profile_id)
    if name is None:
        print(f"\n当前名称：{preset.name}\n方案 ID：{profile_id}")
        name = input("输入新名称（1～100 字，回车取消）：").strip()
        if not name:
            print("已取消，名称未修改。")
            return
    version = library.rename(profile_id, name, expected_version=metadata["version"])
    if name.strip() == preset.name:
        print("名称相同，无需修改。")
    else:
        print(f"已重命名：{preset.name} → {name.strip()}（v{version}）")


def interactive(library: ProfileStore) -> int:
    """Run until exit; failed or cancelled operations return to the same menu."""
    print("\ndocAgent · 报告方案管理")
    print(f"方案目录：{library.root}")
    while True:
        print(f"\n{LINE}\n  1  查询全部方案\n  2  按名称 / ID 搜索\n  3  筛选长期未使用方案")
        print("  4  查看方案详情\n  5  删除方案\n  6  重命名方案\n  0  退出\n" + LINE)
        try:
            command = input("请选择 [0–6]：").strip().lower()
            if command in {"0", "q", "exit", "quit"}:
                print("已退出方案管理。")
                return 0
            if command == "1":
                list_profiles(library)
            elif command == "2":
                list_profiles(
                    library, keyword=input("输入名称或 ID 关键词（回车查询全部）：").strip()
                )
            elif command == "3":
                answer = input("至少多少天未使用？[90，输入 0 返回]：").strip()
                if answer != "0":
                    list_profiles(library, unused_days=int(answer or "90"))
            elif command in {"4", "5", "6"}:
                profile_id = choose_profile(library)
                if profile_id:
                    operation = {"4": show_profile, "5": delete_profile, "6": rename_profile}[
                        command
                    ]
                    operation(library, profile_id)
            elif command:
                print("无效选项，请输入 0–6。")
        except ERRORS as exc:
            print(f"\n操作失败：{exc}\n可重新选择，未自动重试删除。")
        except (EOFError, KeyboardInterrupt):
            print("\n已退出方案管理。")
            return 0


def main(argv=None) -> int:
    """Default to the interactive menu and preserve existing automation commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    environment_options = parser.add_mutually_exclusive_group()
    environment_options.add_argument("--env-file", type=Path, help="默认读取项目 .env")
    environment_options.add_argument("--no-env", action="store_true", help="只用已有环境变量")
    commands = parser.add_subparsers(dest="command")
    listing = commands.add_parser("list", help="列出方案及最近实际提交/成功时间")
    listing.add_argument("--unused-days", type=int, help="仅筛选，不自动删除")
    show = commands.add_parser("show", help="查看指定方案及全部版本元数据（JSON）")
    show.add_argument("id")
    delete = commands.add_parser("delete", help="核对引用并确认后删除自定义方案全部版本")
    delete.add_argument("id")
    rename = commands.add_parser("rename", help="重命名方案，保留 ID 和历史版本")
    rename.add_argument("id")
    rename.add_argument("name", help="新名称，包含空格时加引号")
    args = parser.parse_args(argv)
    try:
        environment = args.env_file or Path(__file__).resolve().parents[1] / ".env"
        if not args.no_env and (args.env_file or environment.is_file()):
            os.environ.update(load_environment(environment))
        library = ProfileStore.from_environment()
        if args.command is None:
            return interactive(library)
        if args.command == "list":
            list_profiles(library, unused_days=args.unused_days)
        elif args.command == "show":
            _, _, metadata = library.load(args.id)
            for version in range(1, metadata["version"] + 1):
                print(json.dumps(library.load(args.id, version)[2], ensure_ascii=False, indent=2))
        elif args.command == "rename":
            rename_profile(library, args.id, args.name)
        else:
            delete_profile(library, args.id)
    except (*ERRORS, EOFError, KeyboardInterrupt) as exc:
        parser.exit(1, f"操作失败：{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
