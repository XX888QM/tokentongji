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

import fcntl
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from . import config, db
from .models import CATEGORY_OBSERVER, SOURCE_CLAUDE, SOURCE_CODEX, SOURCE_OPENCLAW
from .parsers import claude as claude_parser
from .parsers import codex as codex_parser
from .parsers import opencode as opencode_parser
from .parsers import openclaw as openclaw_parser
from .parsers import hermes as hermes_parser
from .parsers import grok as grok_parser

MAX_LINE_BYTES = 50 * 1024 * 1024
_HEAD_SIG_BYTES = 256


def claude_files() -> Iterator[Path]:
    root = config.CLAUDE_PROJECTS_DIR
    if root.is_dir():
        yield from root.rglob("*.jsonl")


def codex_files() -> Iterator[Path]:
    for root in config.CODEX_SESSION_DIRS:
        if root.is_dir():
            yield from root.rglob("*.jsonl")


def claude_mem_codex_usage_files() -> Iterator[Path]:
    root = config.CLAUDE_MEM_CODEX_USAGE_DIR
    if root.is_dir():
        yield from sorted(root.glob("codex-usage-*.jsonl"))


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


def openclaw_sqlite_files() -> Iterator[Path]:
    root = config.OPENCLAW_AGENTS_DIR
    if not root.is_dir():
        return
    for path in sorted(root.glob("*/agent/openclaw-agent.sqlite")):
        if path.is_file():
            yield path


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


def _file_head_sig(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(_HEAD_SIG_BYTES).hex()
    except OSError:
        return ""


def _head_rewritten(state: dict | None, path: Path) -> bool:
    if not state:
        return False
    prev = (state.get("ctx") or {}).get("_head_sig")
    if not prev:
        return False
    return prev != _file_head_sig(path)


def _ingest_file(conn, path: Path, source: str, default_model: str) -> int:
    """增量解析单个文件，返回新增或更新记录数。"""
    try:
        st = path.stat()
    except OSError:
        return 0
    source_file = str(path)
    state = db.get_ingest_state(conn, source_file)
    start_offset, reset_ctx = _should_read(state, st.st_ino, st.st_size, st.st_mtime)
    if _head_rewritten(state, path):
        start_offset, reset_ctx = 0, True

    # 无新增则快速跳过
    if state is not None and start_offset == state["offset"] and st.st_size == state["offset"] and not reset_ctx:
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
                try:
                    if source == SOURCE_CLAUDE:
                        parsed = claude_parser.parse_records(obj, source_file, line_start)
                        if len(parsed) > 1:
                            legacy_claude_keys.add(obj["message"]["id"])
                        recs.extend(parsed)
                        continue
                    if source == SOURCE_CODEX:
                        rec = codex_parser.process_record(obj, source_file, line_start, cstate)
                    else:
                        rec = openclaw_parser.parse_record(obj, source_file, line_start)
                except (AttributeError, OverflowError, TypeError, ValueError):
                    continue
                if rec is not None:
                    recs.append(rec)
    except OSError:
        return 0

    added = 0
    if legacy_claude_keys:
        recs = [r for r in recs if r.dedup_key not in legacy_claude_keys]
        # 删旧临时行 + 写新 iteration 行必须原子生效（commit=False 合并成一次
        # commit）：分两次提交的话，进程恰好在两次 commit 之间被杀会留下"旧行
        # 已删、新行未写"的短暂空窗，该消息的用量会短暂从统计里消失（下一轮
        # ingest 会重新处理同一行补齐，只是重启前有个偏低的窗口）。
        db.delete_dedup_keys(conn, SOURCE_CLAUDE, legacy_claude_keys, commit=False)
    if source == SOURCE_CLAUDE:
        # 旧副本可能在 fallback 明细之后才被扫描；base key 不冲突却会重复计数。
        recs = [
            r for r in recs
            if ":iteration:" in r.dedup_key or not db.has_claude_iterations(conn, r.dedup_key)
        ]
    if recs:
        on_conflict = "max" if source == SOURCE_CLAUDE else "ignore"
        added += db.insert_records(conn, recs, on_conflict=on_conflict, commit=False)
    if legacy_claude_keys or recs:
        conn.commit()

    new_ctx = cstate.to_ctx() if cstate is not None else {}
    new_ctx["_head_sig"] = _file_head_sig(path)
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


def _ingest_grok(conn, path=None, observer: bool = False) -> int:
    """增量解析 Grok unified.jsonl；model/cwd 按 sid carry-forward 并持久化到 ctx。"""
    path = path or config.GROK_LOG_PATH
    if not path.is_file():
        return 0
    try:
        st = path.stat()
    except OSError:
        return 0

    source_file = str(path)
    state = db.get_ingest_state(conn, source_file)
    start_offset, reset_ctx = _should_read(state, st.st_ino, st.st_size, st.st_mtime)
    old_ctx = (state["ctx"] or {}) if state else {}
    if _head_rewritten(state, path):
        start_offset, reset_ctx = 0, True

    if state is not None and start_offset == state["offset"] and st.st_size == state["offset"] and not reset_ctx:
        return 0

    # inode/截断重置 offset，但 sid→model/cwd 字典要留下，否则 Grok 默默掉 unknown
    if reset_ctx:
        ctx = {"models": old_ctx.get("models") or {}, "cwds": old_ctx.get("cwds") or {}}
    else:
        ctx = old_ctx
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
                try:
                    rec = grok_parser.process_record(obj, source_file, line_start, gstate)
                    if rec is not None:
                        if observer:
                            rec = replace(
                                rec,
                                project="claude-mem",
                                category=CATEGORY_OBSERVER,
                                dedup_key=f"claude-mem-grok:{rec.dedup_key}",
                            )
                        recs.append(rec)
                except (AttributeError, OverflowError, TypeError, ValueError):
                    continue
    except OSError:
        return 0

    added = 0
    if recs:
        added = db.insert_records(conn, recs, on_conflict="ignore")

    grok_ctx = gstate.to_ctx()
    grok_ctx["_head_sig"] = _file_head_sig(path)
    db.set_ingest_state(
        conn,
        source_file,
        inode=st.st_ino,
        offset=consumed,
        size=st.st_size,
        mtime=st.st_mtime,
        ctx=grok_ctx,
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
    if _head_rewritten(state, path):
        start_offset, reset_ctx = 0, True

    if state is not None and start_offset == state["offset"] and st.st_size == state["offset"] and not reset_ctx:
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
                try:
                    rec = openclaw_parser.parse_v3_record(obj, source_file, line_start, ctx)
                except (AttributeError, OverflowError, TypeError, ValueError):
                    continue
                if rec is not None:
                    recs.append(rec)
    except OSError:
        return 0

    added = 0
    if recs:
        added += db.insert_records(conn, recs, on_conflict="ignore")

    ctx["_head_sig"] = _file_head_sig(path)
    db.set_ingest_state(
        conn, source_file,
        inode=st.st_ino, offset=consumed,
        size=st.st_size, mtime=st.st_mtime,
        ctx=ctx,
    )
    return added


def _ingest_openclaw_sqlite(conn) -> int:
    """全表重扫各 agent 的 openclaw-agent.sqlite；dedup_key 与 v3 jsonl 相同。"""
    added = 0
    for path in openclaw_sqlite_files():
        try:
            records = openclaw_parser.fetch_records(path)
            if records:
                session_records = {}
                seen_keys = set()
                for record in records:
                    if record.session_id and record.dedup_key not in seen_keys:
                        seen_keys.add(record.dedup_key)
                        session_records.setdefault(record.session_id, []).append(record)
                trajectory_totals = db.openclaw_trajectory_totals(conn, session_records)
                covered_sessions = set()
                for session_id, old_total in trajectory_totals.items():
                    running_total = 0
                    for record in session_records.get(session_id, []):
                        running_total += record.total_tokens
                        if running_total == old_total:
                            covered_sessions.add(session_id)
                            break
                blocked_sessions = set(trajectory_totals) - covered_sessions
                records = [record for record in records if record.session_id not in blocked_sessions]
                if not records:
                    continue
                try:
                    db.delete_openclaw_trajectory_sessions(
                        conn, covered_sessions, commit=False
                    )
                    added += db.insert_records(conn, records, on_conflict="ignore", commit=False)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except (OSError, sqlite3.Error, OverflowError, TypeError, ValueError, AttributeError, UnicodeDecodeError):
            continue
    return added


def _is_codex_fork_file(path: Path) -> bool:
    """任一 session_meta 带 forked_from_id 就要清 replay。父/子 meta 会交错，不能只看第一条。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if "forked_from_id" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "session_meta":
                    continue
                payload = obj.get("payload")
                if isinstance(payload, dict) and payload.get("forked_from_id"):
                    return True
    except OSError:
        return False
    return False


def reset_codex_fork_sessions(conn) -> dict:
    """删掉 fork session 的 usage_events + ingest_state，让新 parser 重扫。

    只动磁盘上仍能看到 forked_from_id 的文件；已删源文件的历史行无法复核，不动。
    """
    names = []
    seen = set()
    for path in codex_files():
        if path.name in seen:
            continue
        if not _is_codex_fork_file(path):
            continue
        seen.add(path.name)
        names.append(path.name)
    deleted = 0
    for name in names:
        cur = conn.execute(
            "DELETE FROM usage_events WHERE source = ? AND ("
            "source_file LIKE ? OR source_file = ?)",
            (SOURCE_CODEX, f"%/{name}", name),
        )
        deleted += cur.rowcount
        conn.execute(
            "DELETE FROM ingest_state WHERE source_file LIKE ? OR source_file = ?",
            (f"%/{name}", name),
        )
    conn.commit()
    return {"files": len(names), "deleted_events": deleted}


def run_once(reset_codex_forks: bool = False) -> dict:
    """扫描全部数据源，增量入库一次。返回统计 dict。"""
    config.ensure_data_dir()
    lock_fd = os.open(config.DATA_DIR / "ingest.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return {"files_scanned": 0, "records_added": 0, "error": "another ingest is running"}
    conn = db.get_conn(config.DB_PATH)
    db.init_db(conn)
    reset = None
    if reset_codex_forks:
        reset = reset_codex_fork_sessions(conn)
    default_model = codex_parser.read_default_model()
    files_scanned = 0
    records_added = 0
    errors = []

    def _safe(name, fn):
        try:
            return fn()
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            return 0

    try:
        for path in claude_files():
            records_added += _safe(f"claude:{path.name}", lambda p=path: _ingest_file(conn, p, SOURCE_CLAUDE, default_model))
            files_scanned += 1
        for path in codex_files():
            records_added += _safe(f"codex:{path.name}", lambda p=path: _ingest_file(conn, p, SOURCE_CODEX, default_model))
            files_scanned += 1
        for path in claude_mem_codex_usage_files():
            records_added += _safe(f"claude-mem:{path.name}", lambda p=path: _ingest_file(conn, p, SOURCE_CODEX, default_model))
            files_scanned += 1
        records_added += _safe("opencode", lambda: _ingest_opencode(conn))
        records_added += _safe("hermes", lambda: _ingest_hermes(conn))
        sqlite_paths = list(openclaw_sqlite_files())
        # 孤立 trajectory 仍是唯一数据源，有 sqlite 也不能全局停吃；
        # 配对删除和 sqlite 前缀和去重挡住双计。
        for path in openclaw_files():
            records_added += _safe(f"openclaw:{path.name}", lambda p=path: _ingest_file(conn, p, SOURCE_OPENCLAW, ""))
            files_scanned += 1
        v3_paths = list(openclaw_v3_files())
        for path in v3_paths:
            records_added += _safe(f"openclaw-v3:{path.name}", lambda p=path: _ingest_openclaw_v3_file(conn, p))
            files_scanned += 1
        try:
            db.delete_openclaw_cross_format_duplicates(conn, (str(p) for p in v3_paths))
        except Exception as exc:
            errors.append(f"openclaw-dedup: {exc}")
        records_added += _safe("openclaw-sqlite", lambda: _ingest_openclaw_sqlite(conn))
        files_scanned += len(sqlite_paths)
        for path, observer in (
            (config.GROK_LOG_PATH, False),
            (config.CLAUDE_MEM_GROK_LOG_PATH, True),
        ):
            if path.is_file():
                records_added += _safe(
                    f"grok:{path.name}",
                    lambda p=path, obs=observer: _ingest_grok(conn, p, obs),
                )
                files_scanned += 1
    finally:
        conn.close()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    result = {"files_scanned": files_scanned, "records_added": records_added}
    if reset is not None:
        result["reset_codex_forks"] = reset
    if errors:
        result["errors"] = errors
    return result


if __name__ == "__main__":
    import sys
    import time

    t0 = time.time()
    result = run_once(reset_codex_forks="--reset-codex-forks" in sys.argv)
    result["elapsed_sec"] = round(time.time() - t0, 1)
    print(json.dumps(result, ensure_ascii=False))
