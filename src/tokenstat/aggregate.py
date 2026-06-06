"""聚合查询：把 usage_events 汇总成仪表盘三个 API 需要的结构。

口径：
- 按 date_local（Asia/Shanghai 本地日）分桶。
- 归一化总量 total = input + output + cache_read + cache_creation
  （reasoning 是 output 的子集，不计入 total，避免重复）。
- 费用按 pricing 单价、逐 (source, model) 行计算后再汇总。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

from . import pricing as pricing_mod
from .models import _LOCAL_TZ, project_display


def _today_local() -> date:
    return datetime.now(tz=_LOCAL_TZ).date()


def period_range(period: str, today: Optional[date] = None) -> tuple[str, str]:
    """返回 (start_date, end_date) 闭区间，均为本地 YYYY-MM-DD。"""
    t = today or _today_local()
    if period == "today":
        return t.isoformat(), t.isoformat()
    if period == "week":
        monday = t - timedelta(days=t.weekday())
        return monday.isoformat(), t.isoformat()
    if period == "month":
        first = t.replace(day=1)
        return first.isoformat(), t.isoformat()
    if period == "year":
        first = t.replace(month=1, day=1)
        return first.isoformat(), t.isoformat()
    raise ValueError(f"未知 period: {period!r}")


def _row_total(r: dict) -> int:
    return (
        r["input"]
        + r["output"]
        + r["cache_read"]
        + r["cache_creation"]
    )


def _grouped(
    conn: sqlite3.Connection, start: str, end: str
) -> list[dict]:
    """按 (source, model, project) 汇总区间内 token。"""
    cur = conn.execute(
        """
        SELECT source, model, project,
               SUM(input_tokens)          AS input,
               SUM(output_tokens)         AS output,
               SUM(cache_read_tokens)     AS cache_read,
               SUM(cache_creation_tokens) AS cache_creation,
               SUM(reasoning_tokens)      AS reasoning
        FROM usage_events
        WHERE date_local BETWEEN ? AND ?
        GROUP BY source, model, project
        """,
        (start, end),
    )
    return [dict(row) for row in cur.fetchall()]


def _period_summary(conn: sqlite3.Connection, period: str, pricing: dict) -> dict:
    start, end = period_range(period)
    rows = _grouped(conn, start, end)
    total = 0
    cost = 0.0
    by_source: dict[str, dict] = {}
    for r in rows:
        rt = _row_total(r)
        rc = pricing_mod.cost_for(
            r["model"],
            input_tokens=r["input"],
            output_tokens=r["output"],
            cache_read_tokens=r["cache_read"],
            cache_creation_tokens=r["cache_creation"],
            reasoning_tokens=r["reasoning"],
            pricing=pricing,
        )
        total += rt
        cost += rc
        src = by_source.setdefault(r["source"], {"total": 0, "cost_usd": 0.0})
        src["total"] += rt
        src["cost_usd"] += rc
    for s in by_source.values():
        s["cost_usd"] = round(s["cost_usd"], 4)
    return {
        "total": total,
        "cost_usd": round(cost, 4),
        "by_source": by_source,
    }


def summary(conn: sqlite3.Connection, pricing: Optional[dict] = None) -> dict:
    if pricing is None:
        pricing = pricing_mod.load_pricing()
    return {
        "periods": {
            "today": _period_summary(conn, "today", pricing),
            "week": _period_summary(conn, "week", pricing),
            "month": _period_summary(conn, "month", pricing),
            "year": _period_summary(conn, "year", pricing),
        },
        "pricing_note": pricing_mod.pricing_note(pricing),
    }


def daily(conn: sqlite3.Connection, days: int = 30) -> dict:
    """近 N 天每日总 token，按来源拆 claude/codex；补齐缺失日期为 0。"""
    today = _today_local()
    start = (today - timedelta(days=days - 1)).isoformat()
    cur = conn.execute(
        """
        SELECT date_local, source,
               SUM(input_tokens + output_tokens + cache_read_tokens
                   + cache_creation_tokens) AS total
        FROM usage_events
        WHERE date_local >= ?
        GROUP BY date_local, source
        """,
        (start,),
    )
    table: dict[str, dict[str, int]] = {}
    for row in cur.fetchall():
        d = table.setdefault(row["date_local"], {"claude": 0, "codex": 0})
        d[row["source"]] = d.get(row["source"], 0) + int(row["total"])

    out = []
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).isoformat()
        rec = table.get(d, {"claude": 0, "codex": 0})
        out.append({"date": d, "claude": rec.get("claude", 0), "codex": rec.get("codex", 0)})
    return {"days": out}


def breakdown(
    conn: sqlite3.Connection, period: str, pricing: Optional[dict] = None
) -> dict:
    if pricing is None:
        pricing = pricing_mod.load_pricing()
    start, end = period_range(period)
    rows = _grouped(conn, start, end)

    # 按 (source, model) 合并
    model_map: dict[tuple, dict] = {}
    proj_map: dict[tuple, dict] = {}
    for r in rows:
        rt = _row_total(r)
        rc = pricing_mod.cost_for(
            r["model"],
            input_tokens=r["input"],
            output_tokens=r["output"],
            cache_read_tokens=r["cache_read"],
            cache_creation_tokens=r["cache_creation"],
            reasoning_tokens=r["reasoning"],
            pricing=pricing,
        )
        mk = (r["source"], r["model"])
        m = model_map.setdefault(
            mk,
            {
                "source": r["source"],
                "model": r["model"],
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_creation": 0,
                "total": 0,
                "cost_usd": 0.0,
            },
        )
        m["input"] += r["input"]
        m["output"] += r["output"]
        m["cache_read"] += r["cache_read"]
        m["cache_creation"] += r["cache_creation"]
        m["total"] += rt
        m["cost_usd"] += rc

        pk = (r["source"], r["project"])
        p = proj_map.setdefault(
            pk,
            {
                "source": r["source"],
                "project": project_display(r["project"]),
                "cwd": r["project"],
                "total": 0,
                "cost_usd": 0.0,
            },
        )
        p["total"] += rt
        p["cost_usd"] += rc

    by_model = sorted(model_map.values(), key=lambda x: x["total"], reverse=True)
    by_project = sorted(proj_map.values(), key=lambda x: x["total"], reverse=True)
    for m in by_model:
        m["cost_usd"] = round(m["cost_usd"], 4)
    for p in by_project:
        p["cost_usd"] = round(p["cost_usd"], 4)

    return {"period": period, "by_model": by_model, "by_project": by_project}


def top_sessions(
    conn: sqlite3.Connection,
    period: str,
    pricing: Optional[dict] = None,
    limit: int = 10,
) -> dict:
    """按会话聚合，返回费用最高的 Top N 会话。"""
    if pricing is None:
        pricing = pricing_mod.load_pricing()
    start, end = period_range(period)
    cur = conn.execute(
        """
        SELECT session_id, source, model, project,
               SUM(input_tokens)          AS input,
               SUM(output_tokens)         AS output,
               SUM(cache_read_tokens)     AS cache_read,
               SUM(cache_creation_tokens) AS cache_creation,
               SUM(reasoning_tokens)      AS reasoning,
               MIN(date_local)            AS date,
               COUNT(*)                   AS records
        FROM usage_events
        WHERE date_local BETWEEN ? AND ?
          AND session_id != ''
        GROUP BY session_id, source, model, project
        """,
        (start, end),
    )
    sess_map: dict[str, dict] = {}
    for row in cur.fetchall():
        r = dict(row)
        sid = r["session_id"]
        rt = _row_total(r)
        rc = pricing_mod.cost_for(
            r["model"],
            input_tokens=r["input"],
            output_tokens=r["output"],
            cache_read_tokens=r["cache_read"],
            cache_creation_tokens=r["cache_creation"],
            reasoning_tokens=r["reasoning"],
            pricing=pricing,
        )
        if sid not in sess_map:
            sess_map[sid] = {
                "session_id": sid,
                "source": r["source"],
                "project": project_display(r["project"]),
                "date": r["date"],
                "total": 0,
                "cost_usd": 0.0,
                "records": 0,
                "model": r["model"],
                "_top_tokens": rt,
            }
        s = sess_map[sid]
        s["total"] += rt
        s["cost_usd"] += rc
        s["records"] += r["records"]
        if rt > s["_top_tokens"]:
            s["model"] = r["model"]
            s["_top_tokens"] = rt
        if r["date"] < s["date"]:
            s["date"] = r["date"]

    result = []
    for s in sorted(sess_map.values(), key=lambda x: x["cost_usd"], reverse=True)[:limit]:
        result.append({
            "session_id": s["session_id"],
            "source": s["source"],
            "model": s["model"],
            "project": s["project"],
            "date": s["date"],
            "total": s["total"],
            "cost_usd": round(s["cost_usd"], 4),
            "records": s["records"],
        })
    return {"period": period, "sessions": result}


def meta(conn: sqlite3.Connection) -> dict:
    """轻量统计信息：总事件数、日期范围。"""
    cur = conn.execute(
        "SELECT COUNT(*) c, MIN(date_local) lo, MAX(date_local) hi FROM usage_events"
    )
    row = cur.fetchone()
    return {
        "total_events": int(row["c"] or 0),
        "date_range": [row["lo"], row["hi"]],
    }
