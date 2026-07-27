"""聚合查询：把 usage_events 汇总成仪表盘三个 API 需要的结构。

口径：
- 按 date_local（Asia/Shanghai 本地日）分桶。
- 归一化总量 total = input + output + cache_read + cache_creation。
  Codex 的 reasoning 是 output 子集，不重复计；Opencode 的 reasoning 独立于
  output，上屏与计费时并入 output。
- 费用按计价日和单次请求上下文档位分桶后计算，再汇总；不能把多次短请求
  加总成一次长上下文请求。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

from . import config
from . import pricing as pricing_mod
from .models import CATEGORY_OBSERVER, SOURCE_CODEX, SOURCE_OPENCODE, VALID_SOURCES, _LOCAL_TZ, project_display


# claude-mem 不是独立的模型供应商：它实际消耗 Codex 额度，但在仪表盘中必须
# 从直接使用 Codex 的记录中拆开，才能既看见来源又不重复计数。
CLAUDE_MEM_DISPLAY_SOURCE = "claude_mem"
DISPLAY_SOURCES = (*VALID_SOURCES, CLAUDE_MEM_DISPLAY_SOURCE)


def _today_local() -> date:
    return datetime.now(tz=_LOCAL_TZ).date()


def period_range(period: str, today: Optional[date] = None) -> tuple[str, str]:
    """返回 (start_date, end_date) 闭区间，均为本地 YYYY-MM-DD。"""
    t = today or _today_local()
    if period == "today":
        return t.isoformat(), t.isoformat()
    if period == "yesterday":
        y = t - timedelta(days=1)
        return y.isoformat(), y.isoformat()
    if period == "week":
        start = t - timedelta(days=6)
        return start.isoformat(), t.isoformat()
    if period == "month":
        first = t.replace(day=1)
        return first.isoformat(), t.isoformat()
    if period == "all":
        # 累计：不按日历年限制，用远早于任何真实数据的哨兵日期兜底下界
        return "2000-01-01", t.isoformat()
    raise ValueError(f"未知 period: {period!r}")


def _row_total(r: dict) -> int:
    return (
        r["input"]
        + _row_output(r)
        + r["cache_read"]
        + r["cache_creation"]
    )


def _row_output(r: dict) -> int:
    output = int(r["output"] or 0)
    if r.get("source") == SOURCE_OPENCODE:
        output += int(r.get("reasoning") or 0)
    return output


def _total_sql(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    source_col = f"{p}source"
    return (
        f"{p}input_tokens + {p}output_tokens + "
        f"{p}cache_read_tokens + {p}cache_creation_tokens + "
        f"CASE WHEN {source_col} = '{SOURCE_OPENCODE}' THEN {p}reasoning_tokens ELSE 0 END"
    )


def _cost_from_row(r: dict, pricing: dict) -> float:
    priced_at = date.fromisoformat(r["pricing_date"]) if r.get("pricing_date") else None
    threshold = pricing_mod.long_context_threshold_for_model(r["model"], pricing, priced_at)
    long_context = bool(r.get(f"context_gt_{threshold}", 0)) if threshold else False
    return pricing_mod.cost_for(
        r["model"],
        input_tokens=r["input"],
        output_tokens=_row_output(r),
        cache_read_tokens=r["cache_read"],
        cache_creation_tokens=r["cache_creation"],
        reasoning_tokens=r.get("reasoning", 0),
        pricing=pricing,
        long_context=long_context,
        priced_at=priced_at,
    )


def _pricing_context_sql(pricing: dict) -> tuple[str, str]:
    """按所有已知阈值给逐请求来源分桶，避免聚合后误升长上下文价。

    只有 `request_prompt_tokens` 非空的行才能判档。旧库迁移时仅把确认为
    单次调用的 Grok/OpenClaw/OpenCode 行安全回填；Codex/Hermes 的累计口径保持
    NULL，绝不拿汇总 token 猜上下文大小。
    """
    fields = []
    groups = []
    for threshold in pricing_mod.long_context_thresholds(pricing):
        expr = f"CASE WHEN request_prompt_tokens > {threshold} THEN 1 ELSE 0 END"
        fields.append(f"{expr} AS context_gt_{threshold}")
        groups.append(expr)
    return ", ".join(fields), ", ".join(groups)


def _claude_mem_sql(alias: str = "") -> str:
    """返回识别 claude-mem Codex 用量的 SQL CASE 表达式。"""
    prefix = f"{alias}." if alias else ""
    return (
        f"CASE WHEN {prefix}source = '{SOURCE_CODEX}' "
        f"AND {prefix}category = '{CATEGORY_OBSERVER}' "
        f"AND {prefix}dedup_key LIKE 'claude-mem-codex:%' THEN 1 ELSE 0 END"
    )


def _collector_from_row(row: dict) -> Optional[str]:
    return "claude-mem" if row.get("claude_mem") else None


def _display_source_from_row(row: dict) -> str:
    return CLAUDE_MEM_DISPLAY_SOURCE if row.get("claude_mem") else row["source"]


def _display_source_label(row: dict) -> str:
    return "claude-mem（Codex）" if row.get("claude_mem") else row["source"]


def _grouped(
    conn: sqlite3.Connection, start: str, end: str, pricing: dict
) -> list[dict]:
    """按 (source, model, project, claude-mem 标记) 汇总区间内 token。"""
    context_select, context_group = _pricing_context_sql(pricing)
    context_select = f", {context_select}" if context_select else ""
    context_group = f", {context_group}" if context_group else ""
    claude_mem_sql = _claude_mem_sql()
    cur = conn.execute(
        f"""
        SELECT date_local AS pricing_date, source, model, project,
               {claude_mem_sql} AS claude_mem{context_select},
               SUM(input_tokens)          AS input,
               SUM(output_tokens)         AS output,
               SUM(cache_read_tokens)     AS cache_read,
               SUM(cache_creation_tokens) AS cache_creation,
               SUM(reasoning_tokens)      AS reasoning,
               COUNT(*)                   AS records
        FROM usage_events
        WHERE date_local BETWEEN ? AND ?
        GROUP BY date_local, source, model, project, {claude_mem_sql}{context_group}
        """,
        (start, end),
    )
    return [dict(row) for row in cur.fetchall()]


def _period_summary(conn: sqlite3.Connection, period: str, pricing: dict) -> dict:
    start, end = period_range(period)
    rows = _grouped(conn, start, end, pricing)
    total = 0
    cost = 0.0
    by_source: dict[str, dict] = {}
    by_display_source: dict[str, dict] = {}
    claude_mem_total = 0
    claude_mem_records = 0
    for r in rows:
        rt = _row_total(r)
        rc = _cost_from_row(r, pricing)
        total += rt
        cost += rc
        src = by_source.setdefault(r["source"], {"total": 0, "cost_usd": 0.0})
        src["total"] += rt
        src["cost_usd"] += rc
        display_source = _display_source_from_row(r)
        display = by_display_source.setdefault(display_source, {"total": 0, "cost_usd": 0.0})
        display["total"] += rt
        display["cost_usd"] += rc
        if r["claude_mem"]:
            claude_mem_total += rt
            claude_mem_records += int(r["records"] or 0)
    for source_map in (by_source, by_display_source):
        for source in source_map.values():
            source["cost_usd"] = round(source["cost_usd"], 4)
    return {
        "total": total,
        "cost_usd": round(cost, 4),
        "by_source": by_source,
        "by_display_source": by_display_source,
        "claude_mem": {
            "total": claude_mem_total,
            "records": claude_mem_records,
        },
    }


def summary(conn: sqlite3.Connection, pricing: Optional[dict] = None) -> dict:
    if pricing is None:
        pricing = pricing_mod.load_pricing()
    return {
        "periods": {
            "today": _period_summary(conn, "today", pricing),
            "yesterday": _period_summary(conn, "yesterday", pricing),
            "week": _period_summary(conn, "week", pricing),
            "month": _period_summary(conn, "month", pricing),
            "all": _period_summary(conn, "all", pricing),
        },
        "pricing_note": pricing_mod.pricing_note(pricing),
    }


def daily(conn: sqlite3.Connection, days: int = 30) -> dict:
    """近 N 天每日总 token，按展示来源拆分；补齐缺失日期为 0。"""
    today = _today_local()
    start = (today - timedelta(days=days - 1)).isoformat()
    claude_mem_sql = _claude_mem_sql()
    cur = conn.execute(
        f"""
        SELECT date_local, source, {claude_mem_sql} AS claude_mem,
               SUM({_total_sql()}) AS total
        FROM usage_events
        WHERE date_local >= ?
        GROUP BY date_local, source, {claude_mem_sql}
        """,
        (start,),
    )
    table: dict[str, dict[str, int]] = {}
    for row in cur.fetchall():
        rec = dict(row)
        d = table.setdefault(rec["date_local"], {source: 0 for source in DISPLAY_SOURCES})
        source = _display_source_from_row(rec)
        d[source] = d.get(source, 0) + int(rec["total"])

    out = []
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).isoformat()
        rec = {source: 0 for source in DISPLAY_SOURCES}
        rec.update(table.get(d, {}))
        out.append({"date": d, **rec, "total": sum(rec.values())})
    active_sources = [
        source for source in DISPLAY_SOURCES
        if any(day.get(source, 0) for day in out)
    ]
    return {"days": out, "sources": active_sources}


def breakdown(
    conn: sqlite3.Connection, period: str, pricing: Optional[dict] = None
) -> dict:
    if pricing is None:
        pricing = pricing_mod.load_pricing()
    start, end = period_range(period)
    rows = _grouped(conn, start, end, pricing)

    # claude-mem 的实际调用来源仍是 Codex；明细中单独列出，避免和普通 Codex
    # 同模型行混在一起而看不出它的消耗。两组仍只各算一次，合计不变。
    model_map: dict[tuple, dict] = {}
    proj_map: dict[tuple, dict] = {}
    for r in rows:
        rt = _row_total(r)
        rc = _cost_from_row(r, pricing)
        collector = _collector_from_row(r)
        mk = (r["source"], r["model"], collector)
        m = model_map.setdefault(
            mk,
            {
                "source": r["source"],
                "collector": collector,
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
        m["output"] += _row_output(r)
        m["cache_read"] += r["cache_read"]
        m["cache_creation"] += r["cache_creation"]
        m["total"] += rt
        m["cost_usd"] += rc

        pk = (r["source"], r["project"], collector)
        p = proj_map.setdefault(
            pk,
            {
                "source": r["source"],
                "collector": collector,
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
    # 权威总额：在逐行 round 之前用原始值求和。两表是同一批记录的两种切法，
    # 若各自逐行 round 后再累加，舍入累积方向不同会让两个合计差几分钱。
    total_cost_usd = sum(m["cost_usd"] for m in by_model)
    total_tokens = sum(m["total"] for m in by_model)
    for m in by_model:
        m["cost_usd"] = round(m["cost_usd"], 4)
    for p in by_project:
        p["cost_usd"] = round(p["cost_usd"], 4)

    return {
        "period": period,
        "by_model": by_model,
        "by_project": by_project,
        "total_cost_usd": round(total_cost_usd, 4),
        "total_tokens": total_tokens,
    }


def export_rows(
    conn: sqlite3.Connection, period: str, pricing: Optional[dict] = None
) -> list[dict]:
    """导出当前周期的最细分组（来源/采集方/模型/项目），避免双重合计。"""
    if pricing is None:
        pricing = pricing_mod.load_pricing()
    start, end = period_range(period)
    rows = _grouped(conn, start, end, pricing)
    out_map: dict[tuple, dict] = {}
    for row in rows:
        collector = _collector_from_row(row)
        key = (row["source"], collector, row["model"], row["project"])
        out = out_map.setdefault(key, {
            "source": row["source"],
            "collector": collector,
            "model": row["model"],
            "project": row["project"],
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "total": 0,
            "cost_usd": 0.0,
        })
        out["input"] += int(row["input"] or 0)
        out["output"] += _row_output(row)
        out["cache_read"] += int(row["cache_read"] or 0)
        out["cache_creation"] += int(row["cache_creation"] or 0)
        out["total"] += _row_total(row)
        out["cost_usd"] += _cost_from_row(row, pricing)
    for row in out_map.values():
        row["cost_usd"] = round(row["cost_usd"], 4)
    return sorted(out_map.values(), key=lambda row: row["total"], reverse=True)


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
    context_select, context_group = _pricing_context_sql(pricing)
    context_select = f", {context_select}" if context_select else ""
    context_group = f", {context_group}" if context_group else ""
    claude_mem_sql = _claude_mem_sql()
    cur = conn.execute(
        f"""
        SELECT date_local AS pricing_date, session_id, source, model, project,
               {claude_mem_sql} AS claude_mem{context_select},
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
        GROUP BY date_local, session_id, source, model, project, {claude_mem_sql}{context_group}
        """,
        (start, end),
    )
    sess_map: dict[str, dict] = {}
    for row in cur.fetchall():
        r = dict(row)
        sid = r["session_id"]
        rt = _row_total(r)
        rc = _cost_from_row(r, pricing)
        collector = _collector_from_row(r)
        if sid not in sess_map:
            sess_map[sid] = {
                "session_id": sid,
                "source": r["source"],
                "collector": collector,
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
            s["collector"] = collector
            s["project"] = project_display(r["project"])
            s["_top_tokens"] = rt
        if r["date"] < s["date"]:
            s["date"] = r["date"]

    result = []
    for s in sorted(sess_map.values(), key=lambda x: x["cost_usd"], reverse=True)[:limit]:
        result.append({
            "session_id": s["session_id"],
            "source": s["source"],
            "collector": s["collector"],
            "model": s["model"],
            "project": s["project"],
            "date": s["date"],
            "total": s["total"],
            "cost_usd": round(s["cost_usd"], 4),
            "records": s["records"],
        })
    return {"period": period, "sessions": result}


def audit(
    conn: sqlite3.Connection,
    pricing: Optional[dict] = None,
    activity_dates: Optional[dict[str, str]] = None,
) -> dict:
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
    activity_dates = activity_dates or {}
    sources = []
    for row in source_rows:
        last_date = row["last_date"]
        sources.append({
            "source": row["source"],
            "records": int(row["records"] or 0),
            "total": int(row["total"] or 0),
            "first_date": row["first_date"],
            "last_date": last_date,
            # 累计会话来源可有“最近活动”而无新的 token 归档日；两者不能混写。
            "activity_last_date": activity_dates.get(row["source"], last_date),
        })

    state = conn.execute(
        """
        SELECT COUNT(*) AS files,
               MAX(mtime) AS latest_mtime,
               SUM(size) AS total_size,
               SUM(offset) AS total_offset
        FROM ingest_state
        """
    ).fetchone()

    # 只看近 90 天，避免随库无限增长的全表扫描（mixed_sessions 无自然时间边界）
    mixed_cutoff = (_today_local() - timedelta(days=90)).isoformat()
    mixed_rows = conn.execute(
        f"""
        SELECT session_id,
               COUNT(DISTINCT source) AS sources,
               COUNT(DISTINCT model) AS models,
               COUNT(DISTINCT project) AS projects,
               COUNT(*) AS records,
               SUM({_total_sql()}) AS total
        FROM usage_events
        WHERE session_id != '' AND date_local >= ?
        GROUP BY session_id
        HAVING sources > 1 OR models > 1 OR projects > 1
        ORDER BY total DESC
        LIMIT 10
        """,
        (mixed_cutoff,),
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

    # 同时检查绝对新鲜度与来源间相对落后，避免所有采集一起停摆时仍显示正常。
    dated = [s for s in sources if s["records"] and s["activity_last_date"]]
    if dated:
        newest = max(s["activity_last_date"] for s in dated)
        newest_d = date.fromisoformat(newest)
        overall_lag = (_today_local() - newest_d).days
        if overall_lag >= config.STALE_SOURCE_DAYS:
            issues.append({
                "level": "warn",
                "message": f"全部来源已 {overall_lag} 天无新数据（最新 {newest}）",
            })
        # 单来源落后于最新来源：分不清是采集故障还是用户没用该工具，只作 info 提示，
        # 不升级为 warn（不触发「需关注」）。全部来源一起停摆才是真故障，见上面 overall_lag。
        for s in dated:
            lag = (newest_d - date.fromisoformat(s["activity_last_date"])).days
            if lag >= config.STALE_SOURCE_DAYS:
                issues.append({
                    "level": "info",
                    "message": f"{s['source']} 已 {lag} 天无新数据（最后 {s['activity_last_date']}）",
                })
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
    context_select, context_group = _pricing_context_sql(pricing)
    context_select = f", {context_select}" if context_select else ""
    context_group = f", {context_group}" if context_group else ""
    claude_mem_sql = _claude_mem_sql()

    rows = conn.execute(
        f"""
        SELECT date_local AS pricing_date, source, model, project,
               {claude_mem_sql} AS claude_mem{context_select},
               SUM(input_tokens) AS input,
               SUM(output_tokens) AS output,
               SUM(cache_read_tokens) AS cache_read,
               SUM(cache_creation_tokens) AS cache_creation,
               SUM(reasoning_tokens) AS reasoning,
               COUNT(*) AS records,
               MIN(date_local) AS first_date,
               MAX(date_local) AS last_date
        FROM usage_events
        WHERE {base_where}
        GROUP BY date_local, source, model, project, {claude_mem_sql}{context_group}
        ORDER BY SUM({_total_sql()}) DESC
        """,
        params,
    ).fetchall()

    group_map: dict[tuple, dict] = {}
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
        collector = _collector_from_row(r)
        key = (r["source"], collector, r["model"], r["project"])
        group = group_map.setdefault(key, {
            "source": r["source"],
            "collector": collector,
            "model": r["model"],
            "project": project_display(r["project"]),
            "cwd": r["project"],
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "reasoning": 0,
            "total": 0,
            "cost_usd": 0.0,
            "records": 0,
        })
        group["input"] += int(r["input"] or 0)
        group["output"] += _row_output(r)
        group["cache_read"] += int(r["cache_read"] or 0)
        group["cache_creation"] += int(r["cache_creation"] or 0)
        group["reasoning"] += int(r["reasoning"] or 0)
        group["total"] += rt
        group["cost_usd"] += rc
        group["records"] += int(r["records"] or 0)

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

    groups = sorted(group_map.values(), key=lambda group: group["total"], reverse=True)
    for group in groups:
        group["cost_usd"] = round(group["cost_usd"], 4)

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
        elif baseline == 0:
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
    claude_mem_sql = _claude_mem_sql()
    top_project = conn.execute(
        f"""
        SELECT project, source, {claude_mem_sql} AS claude_mem,
               SUM({_total_sql()}) AS total
        FROM usage_events
        WHERE date_local BETWEEN ? AND ?
        GROUP BY project, source, {claude_mem_sql}
        ORDER BY total DESC
        LIMIT 1
        """,
        (start_today, end_today),
    ).fetchone()
    if top_project and top_project["total"]:
        project_row = dict(top_project)
        cards.append({
            "level": "info",
            "title": "最大项目贡献",
            "body": f"{project_display(project_row['project'])} / {_display_source_label(project_row)} 贡献 {int(project_row['total']):,} tokens。",
        })

    top_model = conn.execute(
        f"""
        SELECT model, source, {claude_mem_sql} AS claude_mem,
               SUM(input_tokens) AS input,
               SUM(output_tokens) AS output,
               SUM(cache_read_tokens) AS cache_read,
               SUM(cache_creation_tokens) AS cache_creation,
               SUM(reasoning_tokens) AS reasoning
        FROM usage_events
        WHERE date_local BETWEEN ? AND ?
        GROUP BY model, source, {claude_mem_sql}
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
            "body": f"{_display_source_label(r)} / {r['model']} 贡献 {rt:,} tokens。",
        })

    # 后台消耗占比：observer(claude-mem)/subagent 混在总量里，用户此前无法区分。
    cat_rows = conn.execute(
        f"""
        SELECT category, SUM({_total_sql()}) AS total
        FROM usage_events
        WHERE date_local BETWEEN ? AND ?
        GROUP BY category
        """,
        (start_today, end_today),
    ).fetchall()
    cat_totals = {row["category"]: int(row["total"] or 0) for row in cat_rows}
    cat_sum = sum(cat_totals.values())
    if cat_sum > 0:
        bg = cat_totals.get("observer", 0) + cat_totals.get("subagent", 0)
        bg_pct = round(bg / cat_sum * 100, 1)
        cards.append({
            "level": "info",
            "title": "后台消耗占比",
            "body": (
                f"今日 observer+subagent 占 {bg_pct}%"
                f"（主交互 {round(cat_totals.get('main', 0) / cat_sum * 100, 1)}%）。"
                if bg else "今日全部为主交互消耗，无后台工具/子代理占用。"
            ),
        })

    cache_row = conn.execute(
        """
        SELECT SUM(input_tokens) AS input,
               SUM(cache_read_tokens) AS cache_read,
               SUM(output_tokens + CASE WHEN source = ? THEN reasoning_tokens ELSE 0 END) AS output
        FROM usage_events
        WHERE date_local BETWEEN ? AND ?
        """,
        (SOURCE_OPENCODE, start_today, end_today),
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
