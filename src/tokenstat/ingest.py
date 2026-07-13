"""增量入库：按字节 offset 断点续读两边日志，解析后写 SQLite。

健壮性（recon 实测）：
- 逐行流式读，单行 >50MB 跳过（防 76MB 巨行撑内存）。
- 只消费完整行（以 \\n 结尾）；正在写入的尾部残行不消费，下轮再读。
- stat 校验：inode 变 / size < 已读 offset → 文件重建或被截断，从头重读。
- 坏行 try/except 跳过，不中断整文件。
- Codex carry-forward 上下文(cur_model/cur_cwd/prev_total)持久化到 ingest_state.ctx，
  跨增量批次延续差分。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from . import config, db
from .models import SOURCE_CLAUDE, SOURCE_CODEX, SOURCE_OPENCLAW
from .parsers import claude as claude_parser
from .parsers import codex as codex_parser
from .parsers import opencode as opencode_parser
from .parsers import openclaw as openclaw_parser
from .parsers import hermes as hermes_parser
from .parsers import grok as grok_parser

MAX_LINE_BYTES = 50 * 1024 * 1024


def claude_files() -> Iterator[Path]:
    root = config.CLAUDE_PROJECTS_DIR
    if root.is_dir():
        yield from root.rglob("*.jsonl")


def codex_files() -> Iterator[Path]:
    for root in config.CODEX_SESSION_DIRS:
        if root.is_dir():
            yield from root.rglob("*.jsonl")


def openclaw_files() -> Iterator[Path]:
    root = config.OPENCLAW_SESSION_DIR
    if root.is_dir():
        yield from root.glob("*.trajectory.jsonl")


def openclaw_v3_files() -> Iterator[Path]:
    root = config.OPENCLAW_SESSION_DIR
    if root.is_dir():
        for p in root.glob("*.jsonl"):
            if "trajectory" not in p.name:
                yield p


def _should_read(state: dict | None, inode: int, size: int, mtime: float):
    """返回 (start_offset, reset_ctx)。决定续读还是从头读。"""
    if state is None:
        return 0, True
    if state["inode"] != inode or size < state["offset"]:
        # 文件被重建/截断 → 从头读，重置 ctx
        return 0, True
    if size == state["offset"] and mtime <= state["mtime"]:
        # 无新增
        return state["offset"], False
    return state["offset"], False


def _ingest_file(conn, path: Path, source: str, default_model: str) -> int:
    """增量解析单个文件，返回新增或更新记录数。"""
    try:
        st = path.stat()
    except OSError:
        return 0
    source_file = str(path)
    state = db.get_ingest_state(conn, source_file)
    start_offset, reset_ctx = _should_read(state, st.st_ino, st.st_size, st.st_mtime)

    # 无新增则快速跳过
    if state is not None and start_offset == state["offset"] and st.st_size == state["offset"]:
        return 0

    ctx = {} if reset_ctx else (state["ctx"] if state else {})
    cstate = (
        codex_parser.CodexState.from_ctx(ctx, default_model)
        if source == SOURCE_CODEX
        else None
    )

    recs = []
    legacy_claude_keys = set()
    consumed = start_offset
    pos = start_offset

    try:
        with open(path, "rb") as fh:
            fh.seek(start_offset)
            for raw in fh:
                line_start = pos
                pos += len(raw)
                if not raw.endswith(b"\n"):
                    break  # 尾部残行，不消费
                consumed = pos
                if len(raw) > MAX_LINE_BYTES:
                    continue
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if source == SOURCE_CLAUDE:
                    parsed = claude_parser.parse_records(obj, source_file, line_start)
                    if len(parsed) > 1:
                        legacy_claude_keys.add(obj["message"]["id"])
                    recs.extend(parsed)
                    continue
                elif source == SOURCE_CODEX:
                    rec = codex_parser.process_record(obj, source_file, line_start, cstate)
                else:
                    rec = openclaw_parser.parse_record(obj, source_file, line_start)
                if rec is not None:
                    recs.append(rec)
    except OSError:
        return 0

    added = 0
    if legacy_claude_keys:
        recs = [r for r in recs if r.dedup_key not in legacy_claude_keys]
        db.delete_dedup_keys(conn, SOURCE_CLAUDE, legacy_claude_keys)
    if recs:
        on_conflict = "max" if source == SOURCE_CLAUDE else "ignore"
        added += db.insert_records(conn, recs, on_conflict=on_conflict)

    new_ctx = cstate.to_ctx() if cstate is not None else {}
    db.set_ingest_state(
        conn,
        source_file,
        inode=st.st_ino,
        offset=consumed,
        size=st.st_size,
        mtime=st.st_mtime,
        ctx=new_ctx,
    )
    return added


def _ingest_opencode(conn) -> int:
    """增量同步 opencode SQLite 数据库，返回新增记录数。"""
    db_path = config.OPENCODE_DB_PATH
    if not db_path.is_file():
        return 0

    state_key = opencode_parser.OPENCODE_STATE_KEY
    state = db.get_ingest_state(conn, state_key)
    since_ts_ms = int((state["ctx"] or {}).get("last_ts_ms", 0)) if state else 0

    records, max_ts_ms = opencode_parser.fetch_records(db_path, since_ts_ms)

    added = 0
    if records:
        added = db.insert_records(conn, records, on_conflict="ignore")

    db.set_ingest_state(
        conn,
        state_key,
        inode=0,
        offset=0,
        size=0,
        mtime=0.0,
        ctx={"last_ts_ms": max_ts_ms},
    )
    return added


def _ingest_hermes(conn) -> int:
    """全量重扫 Hermes sessions 表；session 行随进行原地增长，没有稳定游标可用，
    靠 dedup_key + on_conflict='max' 幂等吸收增长，不会重复计数。"""
    db_path = config.HERMES_STATE_DB
    if not db_path.is_file():
        return 0
    records = hermes_parser.fetch_records(db_path)
    if not records:
        return 0
    return db.insert_records(conn, records, on_conflict="replace")


def _ingest_grok(conn) -> int:
    """增量解析 Grok unified.jsonl；model/cwd 按 sid carry-forward 并持久化到 ctx。"""
    path = config.GROK_LOG_PATH
    if not path.is_file():
        return 0
    try:
        st = path.stat()
    except OSError:
        return 0

    source_file = str(path)
    state = db.get_ingest_state(conn, source_file)
    start_offset, reset_ctx = _should_read(state, st.st_ino, st.st_size, st.st_mtime)

    if state is not None and start_offset == state["offset"] and st.st_size == state["offset"]:
        return 0

    ctx = {} if reset_ctx else ((state["ctx"] or {}) if state else {})
    gstate = grok_parser.GrokState.from_ctx(ctx)

    recs = []
    consumed = start_offset
    pos = start_offset

    try:
        with open(path, "rb") as fh:
            fh.seek(start_offset)
            for raw in fh:
                line_start = pos
                pos += len(raw)
                if not raw.endswith(b"\n"):
                    break
                consumed = pos
                if len(raw) > MAX_LINE_BYTES:
                    continue
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                rec = grok_parser.process_record(obj, source_file, line_start, gstate)
                if rec is not None:
                    recs.append(rec)
    except OSError:
        return 0

    added = 0
    if recs:
        added = db.insert_records(conn, recs, on_conflict="ignore")

    db.set_ingest_state(
        conn,
        source_file,
        inode=st.st_ino,
        offset=consumed,
        size=st.st_size,
        mtime=st.st_mtime,
        ctx=gstate.to_ctx(),
    )
    return added


def _ingest_openclaw_v3_file(conn, path: Path) -> int:
    """增量解析 openclaw v3 session 文件，ctx 跨批次持久化。"""
    try:
        st = path.stat()
    except OSError:
        return 0
    source_file = str(path)
    state = db.get_ingest_state(conn, source_file)
    start_offset, reset_ctx = _should_read(state, st.st_ino, st.st_size, st.st_mtime)

    if state is not None and start_offset == state["offset"] and st.st_size == state["offset"]:
        return 0

    ctx = {} if reset_ctx else ((state["ctx"] or {}) if state else {})

    recs = []
    consumed = start_offset
    pos = start_offset

    try:
        with open(path, "rb") as fh:
            fh.seek(start_offset)
            for raw in fh:
                line_start = pos
                pos += len(raw)
                if not raw.endswith(b"\n"):
                    break
                consumed = pos
                if len(raw) > MAX_LINE_BYTES:
                    continue
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                rec = openclaw_parser.parse_v3_record(obj, source_file, line_start, ctx)
                if rec is not None:
                    recs.append(rec)
    except OSError:
        return 0

    added = 0
    if recs:
        added += db.insert_records(conn, recs, on_conflict="ignore")

    db.set_ingest_state(
        conn, source_file,
        inode=st.st_ino, offset=consumed,
        size=st.st_size, mtime=st.st_mtime,
        ctx=ctx,
    )
    return added


def run_once() -> dict:
    """扫描全部数据源，增量入库一次。返回统计 dict。"""
    config.ensure_data_dir()
    conn = db.get_conn(config.DB_PATH)
    db.init_db(conn)
    default_model = codex_parser.read_default_model()
    files_scanned = 0
    records_added = 0
    try:
        for path in claude_files():
            records_added += _ingest_file(conn, path, SOURCE_CLAUDE, default_model)
            files_scanned += 1
        for path in codex_files():
            records_added += _ingest_file(conn, path, SOURCE_CODEX, default_model)
            files_scanned += 1
        records_added += _ingest_opencode(conn)
        records_added += _ingest_hermes(conn)
        for path in openclaw_files():
            records_added += _ingest_file(conn, path, SOURCE_OPENCLAW, "")
            files_scanned += 1
        for path in openclaw_v3_files():
            records_added += _ingest_openclaw_v3_file(conn, path)
            files_scanned += 1
        db.delete_openclaw_cross_format_duplicates(conn)
        if config.GROK_LOG_PATH.is_file():
            records_added += _ingest_grok(conn)
            files_scanned += 1
    finally:
        conn.close()
    return {"files_scanned": files_scanned, "records_added": records_added}


if __name__ == "__main__":
    import time

    t0 = time.time()
    result = run_once()
    result["elapsed_sec"] = round(time.time() - t0, 1)
    print(json.dumps(result, ensure_ascii=False))
