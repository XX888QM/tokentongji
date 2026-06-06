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


def _total_sql(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return (
        f"{p}input_tokens + {p}output_tokens + "
        f"{p}cache_read_tokens + {p}cache_creation_tokens"
    )


def _cost_from_row(r: dict, pricing: dict) -> float:
    return pricing_mod.cost_for(
        r["model"],
        input_tokens=r["input"],
        output_tokens=r["output"],
        cache_read_tokens=r["cache_read"],
        cache_creation_tokens=r["cache_creation"],
        reasoning_tokens=r.get("reasoning", 0),
        pricing=pricing,
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
            s["source"] = r["source"]
            s["project"] = project_display(r["project"])
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


def audit(conn: sqlite3.Connection, pricing: Optional[dict] = None) -> dict:
    """审计数据完整性与统计口径风险。"""
    if pricing is None:
        pricing = pricing_mod.load_pricing()

    meta_data = meta(conn)
    source_rows = conn.execute(
        f"""
        SELECT source,
               COUNT(*) AS records,
               SUM({_total_sql()}) AS total,
               MIN(date_local) AS first_date,
               MAX(date_local) AS last_date
        FROM usage_events
        GROUP BY source
        ORDER BY source
        """
    ).fetchall()
    sources = [
        {
            "source": row["source"],
            "records": int(row["records"] or 0),
            "total": int(row["total"] or 0),
            "first_date": row["first_date"],
            "last_date": row["last_date"],
        }
        for row in source_rows
    ]

    state = conn.execute(
        """
        SELECT COUNT(*) AS files,
               MAX(mtime) AS latest_mtime,
               SUM(size) AS total_size,
               SUM(offset) AS total_offset
        FROM ingest_state
        """
    ).fetchone()

    mixed_rows = conn.execute(
        f"""
        SELECT session_id,
               COUNT(DISTINCT source) AS sources,
               COUNT(DISTINCT model) AS models,
               COUNT(DISTINCT project) AS projects,
               COUNT(*) AS records,
               SUM({_total_sql()}) AS total
        FROM usage_events
        WHERE session_id != ''
        GROUP BY session_id
        HAVING sources > 1 OR models > 1 OR projects > 1
        ORDER BY total DESC
        LIMIT 10
        """
    ).fetchall()
    mixed_sessions = [
        {
            "session_id": row["session_id"],
            "sources": int(row["sources"] or 0),
            "models": int(row["models"] or 0),
            "projects": int(row["projects"] or 0),
            "records": int(row["records"] or 0),
            "total": int(row["total"] or 0),
        }
        for row in mixed_rows
    ]

    distinct_models = [
        row["model"]
        for row in conn.execute(
            "SELECT DISTINCT model FROM usage_events WHERE model != '' ORDER BY model"
        ).fetchall()
    ]
    unknown_models = [
        model for model in distinct_models
        if pricing_mod.is_unknown_model(model, pricing)
    ]

    issues = []
    total_events = meta_data["total_events"]
    if total_events == 0:
        issues.append({"level": "warn", "message": "数据库暂无 usage_events"})
    present_sources = {s["source"] for s in sources}
    for expected in ("claude", "codex"):
        if expected not in present_sources:
            issues.append({"level": "warn", "message": f"暂无 {expected} 数据"})
    if not state["files"]:
        issues.append({"level": "warn", "message": "暂无 ingest_state，可能还没完成首次入库"})
    if mixed_sessions:
        issues.append({
            "level": "info",
            "message": f"发现 {len(mixed_sessions)} 个跨来源/模型/项目会话样例",
        })
    if unknown_models:
        issues.append({
            "level": "warn",
            "message": f"发现 {len(unknown_models)} 个未知模型按 default 估价",
        })

    latest_mtime = state["latest_mtime"]
    return {
        "status": "warn" if any(i["level"] == "warn" for i in issues) else "ok",
        "meta": meta_data,
        "sources": sources,
        "ingest_state": {
            "files": int(state["files"] or 0),
            "latest_mtime": float(latest_mtime or 0),
            "latest_mtime_local": (
                datetime.fromtimestamp(float(latest_mtime), tz=_LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
                if latest_mtime
                else None
            ),
            "total_size": int(state["total_size"] or 0),
            "total_offset": int(state["total_offset"] or 0),
        },
        "mixed_sessions": mixed_sessions,
        "unknown_models": unknown_models,
        "issues": issues,
    }


def session_detail(
    conn: sqlite3.Connection,
    session_id: str,
    period: str = "today",
    pricing: Optional[dict] = None,
) -> dict:
    """返回单个会话的费用构成、日期、模型、项目和来源文件分布。"""
    if pricing is None:
        pricing = pricing_mod.load_pricing()
    start, end = period_range(period)
    base_where = "session_id = ? AND date_local BETWEEN ? AND ?"
    params = (session_id, start, end)

    rows = conn.execute(
        """
        SELECT source, model, project,
               SUM(input_tokens) AS input,
               SUM(output_tokens) AS output,
               SUM(cache_read_tokens) AS cache_read,
               SUM(cache_creation_tokens) AS cache_creation,
               SUM(reasoning_tokens) AS reasoning,
               COUNT(*) AS records,
               MIN(date_local) AS first_date,
               MAX(date_local) AS last_date
        FROM usage_events
        WHERE """ + base_where + """
        GROUP BY source, model, project
        ORDER BY SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) DESC
        """,
        params,
    ).fetchall()

    groups = []
    total = 0
    cost = 0.0
    records = 0
    first_date = None
    last_date = None
    for row in rows:
        r = dict(row)
        rt = _row_total(r)
        rc = _cost_from_row(r, pricing)
        total += rt
        cost += rc
        records += int(r["records"] or 0)
        first_date = r["first_date"] if first_date is None else min(first_date, r["first_date"])
        last_date = r["last_date"] if last_date is None else max(last_date, r["last_date"])
        groups.append({
            "source": r["source"],
            "model": r["model"],
            "project": project_display(r["project"]),
            "cwd": r["project"],
            "input": int(r["input"] or 0),
            "output": int(r["output"] or 0),
            "cache_read": int(r["cache_read"] or 0),
            "cache_creation": int(r["cache_creation"] or 0),
            "reasoning": int(r["reasoning"] or 0),
            "total": rt,
            "cost_usd": round(rc, 4),
            "records": int(r["records"] or 0),
        })

    date_rows = conn.execute(
        f"""
        SELECT date_local AS date,
               SUM({_total_sql()}) AS total,
               COUNT(*) AS records
        FROM usage_events
        WHERE {base_where}
        GROUP BY date_local
        ORDER BY date_local
        """,
        params,
    ).fetchall()
    file_rows = conn.execute(
        f"""
        SELECT source_file,
               SUM({_total_sql()}) AS total,
               COUNT(*) AS records
        FROM usage_events
        WHERE {base_where}
        GROUP BY source_file
        ORDER BY total DESC
        LIMIT 8
        """,
        params,
    ).fetchall()

    return {
        "period": period,
        "session_id": session_id,
        "summary": {
            "total": total,
            "cost_usd": round(cost, 4),
            "records": records,
            "first_date": first_date,
            "last_date": last_date,
        },
        "groups": groups,
        "by_date": [
            {"date": row["date"], "total": int(row["total"] or 0), "records": int(row["records"] or 0)}
            for row in date_rows
        ],
        "source_files": [
            {
                "source_file": row["source_file"],
                "total": int(row["total"] or 0),
                "records": int(row["records"] or 0),
            }
            for row in file_rows
        ],
    }


def insights(conn: sqlite3.Connection, pricing: Optional[dict] = None) -> dict:
    """生成今日异常尖峰解释，保持零依赖。"""
    if pricing is None:
        pricing = pricing_mod.load_pricing()
    today = _today_local()
    start = (today - timedelta(days=7)).isoformat()
    rows = conn.execute(
        f"""
        SELECT date_local AS date,
               SUM({_total_sql()}) AS total
        FROM usage_events
        WHERE date_local >= ?
        GROUP BY date_local
        ORDER BY date_local
        """,
        (start,),
    ).fetchall()
    daily_totals = {row["date"]: int(row["total"] or 0) for row in rows}
    today_key = today.isoformat()
    yesterday_key = (today - timedelta(days=1)).isoformat()
    today_total = daily_totals.get(today_key, 0)
    yesterday_total = daily_totals.get(yesterday_key, 0)
    prior_values = [
        daily_totals.get((today - timedelta(days=i)).isoformat(), 0)
        for i in range(1, 8)
    ]
    prior_nonzero = [v for v in prior_values if v > 0]
    baseline = int(sum(prior_nonzero) / len(prior_nonzero)) if prior_nonzero else 0

    cards = []
    if today_total == 0:
        cards.append({"level": "info", "title": "今日暂无数据", "body": "还没有解析到今天的 token 使用。"})
    else:
        delta = today_total - yesterday_total
        if yesterday_total > 0:
            pct_change = round((delta / yesterday_total) * 100, 1)
            level = "warn" if pct_change >= 50 else "ok"
            cards.append({
                "level": level,
                "title": "今日对比昨日",
                "body": f"{'增加' if delta >= 0 else '减少'} {abs(pct_change)}%，差值 {abs(delta):,} tokens。",
            })
        else:
            cards.append({
                "level": "info",
                "title": "昨日基线为空",
                "body": f"今日已有 {today_total:,} tokens，昨日没有可比数据。",
            })

        if baseline > 0:
            baseline_delta = today_total - baseline
            baseline_pct = round((baseline_delta / baseline) * 100, 1)
            cards.append({
                "level": "warn" if baseline_pct >= 50 else "ok",
                "title": "近 7 日基线",
                "body": f"今日比近 7 日非零均值 {'高' if baseline_delta >= 0 else '低'} {abs(baseline_pct)}%。",
            })

    start_today, end_today = period_range("today")
    top_project = conn.execute(
        f"""
        SELECT project, source,
               SUM({_total_sql()}) AS total
        FROM usage_events
        WHERE date_local BETWEEN ? AND ?
        GROUP BY project, source
        ORDER BY total DESC
        LIMIT 1
        """,
        (start_today, end_today),
    ).fetchone()
    if top_project and top_project["total"]:
        cards.append({
            "level": "info",
            "title": "最大项目贡献",
            "body": f"{project_display(top_project['project'])} / {top_project['source']} 贡献 {int(top_project['total']):,} tokens。",
        })

    top_model = conn.execute(
        f"""
        SELECT model, source,
               SUM(input_tokens) AS input,
               SUM(output_tokens) AS output,
               SUM(cache_read_tokens) AS cache_read,
               SUM(cache_creation_tokens) AS cache_creation,
               SUM(reasoning_tokens) AS reasoning
        FROM usage_events
        WHERE date_local BETWEEN ? AND ?
        GROUP BY model, source
        ORDER BY SUM({_total_sql()}) DESC
        LIMIT 1
        """,
        (start_today, end_today),
    ).fetchone()
    if top_model:
        r = dict(top_model)
        rt = _row_total(r)
        cards.append({
            "level": "info",
            "title": "最大模型贡献",
            "body": f"{r['source']} / {r['model']} 贡献 {rt:,} tokens，估算 ${_cost_from_row(r, pricing):.2f}。",
        })

    cache_row = conn.execute(
        """
        SELECT SUM(input_tokens) AS input,
               SUM(cache_read_tokens) AS cache_read,
               SUM(output_tokens) AS output
        FROM usage_events
        WHERE date_local BETWEEN ? AND ?
        """,
        (start_today, end_today),
    ).fetchone()
    input_total = int((cache_row["input"] or 0) + (cache_row["cache_read"] or 0))
    cache_read = int(cache_row["cache_read"] or 0)
    output_tokens = int(cache_row["output"] or 0)
    cache_ratio = round((cache_read / input_total) * 100, 1) if input_total else 0
    output_ratio = round((output_tokens / max(1, input_total)) * 100, 1) if input_total else 0

    return {
        "date": today_key,
        "metrics": {
            "today_total": today_total,
            "yesterday_total": yesterday_total,
            "seven_day_nonzero_avg": baseline,
            "cache_read_ratio": cache_ratio,
            "output_input_ratio": output_ratio,
        },
        "cards": cards,
    }


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
