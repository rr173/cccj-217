"""公共工具：JSON 载入、规范化与身份哈希。"""
from __future__ import annotations

import hashlib
import json
import os


class InputError(Exception):
    """输入文件无法解析或种类不符。"""


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        raise InputError(f"无法读取文件 {path}: {e}")
    except json.JSONDecodeError as e:
        raise InputError(f"文件 {path} 不是合法 JSON: {e}")


def dump_json(obj, path=None):
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False)
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return text


def canonical(obj) -> bytes:
    """规范化 JSON 序列化，用于身份哈希（与文件格式无关，只与内容有关）。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_of(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def rows_field(row_ids, cap=100):
    """把（可能很长的）行 id 列表变成确定性的、可核验的字段。

    plan 与 verify 使用同一函数，保证篡改或截断都会被发现。
    """
    row_ids = sorted(row_ids)
    return {
        "rows": row_ids[:cap],
        "total": len(row_ids),
        "truncated": len(row_ids) > cap,
    }


def expect_rows_field(field):
    return set(field.get("rows", [])), field.get("total"), bool(field.get("truncated"))
