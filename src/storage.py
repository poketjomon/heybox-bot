"""JSONL 文件读写工具"""

import json
import os
from datetime import datetime


def read_jsonl(path):
    """读取 JSONL 文件，返回记录列表"""
    if not os.path.exists(path):
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def write_jsonl(path, records):
    """覆盖写入整个 JSONL 文件"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_jsonl(path, record):
    """追加一条记录到 JSONL 文件"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def update_record(path, link_id, updates):
    """更新指定 link_id 的记录，合并 updates 字段。值为 None 时删除该字段"""
    records = read_jsonl(path)
    for r in records:
        if r.get("link_id") == link_id:
            for k, v in updates.items():
                if v is None:
                    r.pop(k, None)
                else:
                    r[k] = v
            break
    write_jsonl(path, records)


def get_existing_ids(path):
    """获取 JSONL 文件中所有 link_id 的集合"""
    records = read_jsonl(path)
    return {r["link_id"] for r in records if "link_id" in r}


def now_iso():
    """返回当前时间 ISO 格式字符串"""
    return datetime.now().isoformat(timespec="seconds")
