"""Inspect permanently saved schemes and explicitly delete unused custom profiles."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

from weekly_report_agent.profiles import ProfileStore
from weekly_report_agent.server import load_environment
from weekly_report_agent.task_queue import TaskQueue


def timestamp(value):
    return (
        datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")
        if value
        else "从未"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="默认读取项目 .env")
    parser.add_argument("--no-env", action="store_true", help="只用已有环境变量")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="列出方案及最近实际提交/成功时间")
    listing.add_argument("--unused-days", type=int, help="仅筛选，不自动删除")
    show = commands.add_parser("show", help="查看指定方案及全部版本元数据")
    show.add_argument("id")
    delete = commands.add_parser("delete", help="核对引用并确认后删除自定义方案全部版本")
    delete.add_argument("id")
    args = parser.parse_args(argv)
    environment = args.env_file or Path(__file__).resolve().parents[1] / ".env"
    if not args.no_env and (args.env_file or environment.is_file()):
        os.environ.update(load_environment(environment))
    library = ProfileStore.from_environment()
    try:
        if args.command == "list":
            if args.unused_days is not None and args.unused_days < 0:
                parser.error("unused-days 不能为负数")
            for item in library.list():
                if (
                    args.unused_days is not None
                    and time.time() - (item["last_used"] or item["created"])
                    < args.unused_days * 86400
                ):
                    continue
                print(
                    f"{item['id']}  {item['name']}  v{item['version']}  "
                    f"使用 {item['usage_count']} 次"
                )
                print(f"  创建 {timestamp(item['created'])}；更新 {timestamp(item['updated'])}")
                print(
                    f"  最近提交 {timestamp(item['last_used'])}；"
                    f"最近成功 {timestamp(item['last_successful'])}"
                )
            return 0
        preset, _, metadata = library.load(args.id)
        if args.command == "show":
            for version in range(1, metadata["version"] + 1):
                print(json.dumps(library.load(args.id, version)[2], ensure_ascii=False, indent=2))
        else:
            print(f"将删除 {preset.name} ({args.id}) 的全部 {metadata['version']} 个版本及模板。")
            print("已生成的独立审核快照保留；排队/执行中的引用会阻止删除。")
            if input("请输入完整方案 ID 确认删除：").strip() != args.id:
                print("未删除。")
                return 0
            library.delete(args.id, TaskQueue.from_environment())
            print("已删除。")
    except (ValueError, OSError, EOFError) as exc:
        parser.exit(1, f"操作失败：{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
