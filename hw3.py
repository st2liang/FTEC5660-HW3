#!/usr/bin/env python3
"""FTEC5660 HW3 student starter: an agent that applies 3 of Fisher's 15 points using tools."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sqlite3
import threading
import zlib
from pathlib import Path
from typing import Any


MODEL_NAME = "deepseek-v4-flash"
# The key is never written in the code: it comes from this environment variable,
# either exported in the shell or set in .env (see build_agent).
API_KEY_ENV = "DEEPSEEK_API_KEY"
API_BASE_ENV = "DEEPSEEK_API_BASE"
INTERVIEW_LIMIT = 10
MAX_SQL_ROWS = 50

# Successful live model replies, printed at the end so a run can be checked.
LIVE_MODEL_CALLS = 0
LIVE_MODEL_ATTEMPTS = 0
_ERRORS_REPORTED = 0

# Philip Fisher, "Common Stocks and Uncommon Profits", Part One, Chapter 3.
# For every point, PASS means the company looks good on it. You choose 3
# points and decide yourself what PASS means for each of them.
FISHER_POINTS = {
    1: "Does the company have products or services with sufficient market potential to make possible a sizable increase in sales for at least several years?",
    2: "Does the management have a determination to continue to develop products or processes that will still further increase total sales potentials when the growth potentials of currently attractive product lines have largely been exploited?",
    3: "How effective are the company's research and development efforts in relation to its size?",
    4: "Does the company have an above-average sales organization?",
    5: "Does the company have a worthwhile profit margin?",
    6: "What is the company doing to maintain or improve profit margins?",
    7: "Does the company have outstanding labor and personnel relations?",
    8: "Does the company have outstanding executive relations?",
    9: "Does the company have depth to its management?",
    10: "How good are the company's cost analysis and accounting controls?",
    11: "Are there other aspects of the business, somewhat peculiar to the industry involved, which will give the investor important clues as to how outstanding the company may be in relation to its competition?",
    12: "Does the company have a short-range or long-range outlook in regard to profits?",
    13: "In the foreseeable future will the growth of the company require sufficient equity financing so that the larger number of shares then outstanding will largely cancel the existing stockholders' benefit from this anticipated growth?",
    14: "Does the management talk freely to investors about its affairs when things are going well but 'clam up' when troubles and disappointments occur?",
    15: "Does the company have a management of unquestionable integrity?",
}
VERDICTS = ("PASS", "FAIL")


def load_env_file(path: Path = Path(".env")) -> None:
    """Load the simple KEY=VALUE entries used by this homework."""
    if not path.is_file():
        return
    import os

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# Choose any 3 of the 15 points, e.g. CHOSEN_POINTS = [1, 7, 15]. Your agent
# gives a PASS or FAIL verdict on these 3 points only, with your own rule for each.
CHOSEN_POINTS: list[int] = [1, 2, 7]

# ---------------------------------------------------------------------------
# My rules (see the report in README.md).
#
# 1  Market potential: PASS when FY2025 revenue grew, from FY2023 to FY2025,
#    by at least the median firm in the company's own industry.
# 2  New products: PASS when the share of FY2025 revenue that comes from
#    products launched on or after 2023-01-01 is at least the industry median.
# 7  Labour relations: PASS when the working-conditions interview is clearly
#    favourable (fair pay, low turnover, problems fixed, good morale); FAIL
#    when it is clearly unfavourable (turnover, constant overtime, low morale,
#    strike threats).
#
# Arithmetic is done in SQL or Python, never in the model's head.
# ---------------------------------------------------------------------------

POINT_RULES: dict[int, str] = {
    1: (
        "PASS if FY2025 revenue (the sum of order amounts in 2025) grew from FY2023 to FY2025 "
        "by at least the industry median revenue_growth_2y_pct; otherwise FAIL."
    ),
    2: (
        "PASS if the share of FY2025 revenue that comes from products launched on or after "
        "2023-01-01 is at least the industry median new_product_share_pct; otherwise FAIL."
    ),
    7: (
        "PASS if the working-conditions interview is clearly favourable: fair pay, low turnover, "
        "people stay for years, problems get fixed, good morale. FAIL if it is clearly "
        "unfavourable: high turnover, constant overtime, low morale, strike threats, unfair pay. "
        "A non-answer is not evidence."
    ),
}

# Point 1: growth from FY2023 to FY2025 next to the industry median.
SQL_POINT_1 = """
SELECT c.name AS company_name,
       c.industry AS industry,
       ROUND(SUM(CASE WHEN substr(o.order_date, 1, 4) = '2025' THEN o.amount ELSE 0 END) * 100.0
             / SUM(CASE WHEN substr(o.order_date, 1, 4) = '2023' THEN o.amount ELSE 0 END) - 100, 2)
             AS company_growth_pct,
       m.revenue_growth_2y_pct AS industry_median_growth_pct
FROM orders AS o
CROSS JOIN company AS c
CROSS JOIN industry_medians AS m
WHERE m.industry = c.industry
  AND substr(o.order_date, 1, 4) IN ('2023', '2025')
GROUP BY c.name, c.industry, m.revenue_growth_2y_pct
"""

# Point 2: share of FY2025 revenue from products launched in 2023 or later.
SQL_POINT_2 = """
SELECT ROUND(SUM(CASE WHEN p.launch_date >= '2023-01-01' THEN o.amount ELSE 0 END) * 100.0
             / SUM(o.amount), 2) AS company_new_product_share_pct,
       m.new_product_share_pct AS industry_median_new_product_share_pct
FROM orders AS o
JOIN products AS p ON p.product = o.product
CROSS JOIN company AS c
CROSS JOIN industry_medians AS m
WHERE m.industry = c.industry
  AND substr(o.order_date, 1, 4) = '2025'
"""

# Point 7: who to ask, in order. Both roles know about working conditions; the
# second is a fallback in case the first gives a non-answer.
LABOUR_ROLES = ("current_employee", "former_employee")

# How many companies to analyse at the same time (each uses several model calls).
MAX_COMPANIES_IN_FLIGHT = 4

JUDGE_SYSTEM_PROMPT = (
    "You are a careful equity analyst using Philip Fisher's scuttlebutt method. "
    "You judge exactly one question from one interview quote. Use only the quote; ignore the "
    "company name and outside knowledge. "
    "A favourable answer describes fair pay, low turnover, people staying for years, problems "
    "being fixed or good morale -> PASS. An unfavourable answer describes high turnover, people "
    "quitting often, constant overtime, low morale, strike threats or unfair pay -> FAIL. A "
    "non-answer (\"I can't comment\", \"I only see the sales team\") is not evidence. "
    "Examples: \"Pay is fair, people stay for years, and problems get fixed\" -> PASS. "
    "\"Overtime is constant and people quit every month\" -> FAIL. "
    "\"Turnover was terrible; we trained new people every month\" -> FAIL. "
    "\"It is a good place to work; hardly anyone in my team has left\" -> PASS. "
    "Answer with a JSON object only: {\"verdict\": \"PASS\" or \"FAIL\", \"reason\": \"one sentence\"}."
)

REVIEW_SYSTEM_PROMPT = (
    "You are the independent reviewer in a Fisher-style research pipeline. Re-read the interview "
    "quote against the rule and decide point 7 on your own. Keep the draft verdict if it fits the "
    "quote; overturn it only when the quote clearly points the other way. Use only the quote. "
    "Answer with a JSON object only: {\"point7\": \"PASS\" or \"FAIL\", \"reason\": \"one sentence\"}."
)

SUMMARY_SYSTEM_PROMPT = (
    "You write the final research note for one company. The three verdicts are already decided "
    "and are binding: never change or contradict them. Write two or three plain sentences that "
    "explain point 1 with its growth rate and industry median, point 2 with its new-product share "
    "and median, and point 7 with the interview quote. No headings, no JSON, no bullet points."
)


def _parse_sql_table(text: str) -> list[dict[str, str]]:
    """Turn the sql_query pipe table into a list of row dictionaries."""
    if not text or text.startswith("SQL error"):
        return []
    lines = [line for line in str(text).splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    header = [cell.strip() for cell in lines[0].split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        if line.startswith("...") or line.startswith("(no rows"):
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


async def _tool_call(tool: Any, **kwargs: Any) -> str:
    """Invoke one LangChain tool without blocking the event loop."""
    result = await asyncio.to_thread(tool.invoke, kwargs)
    return str(getattr(result, "content", result))


def _is_non_answer(text: str) -> bool:
    """True when an interview answer says the person cannot comment."""
    lowered = text.lower()
    markers = (
        "can't comment", "cannot comment", "can't say", "cannot say", "couldn't say",
        "don't see", "do not see", "only see", "never dealt with", "not my area",
    )
    return any(marker in lowered for marker in markers)


async def _working_conditions_quote(tools: dict[str, Any]) -> tuple[str, str]:
    """Spend one interview (two at most) on Fisher's point 7."""
    quote, role = "", LABOUR_ROLES[0]
    for role in LABOUR_ROLES:
        quote = await _tool_call(tools["interview"], role=role, topic="working_conditions")
        if not _is_non_answer(quote):
            break
    return quote, role


async def _gather_evidence(tools: dict[str, Any]) -> dict[str, Any]:
    """Route each point to the right source: SQL for points 1 and 2, interviews for 7."""
    p1_rows = _parse_sql_table(await _tool_call(tools["sql_query"], query=SQL_POINT_1))
    p2_rows = _parse_sql_table(await _tool_call(tools["sql_query"], query=SQL_POINT_2))
    quote, role = await _working_conditions_quote(tools)

    p1 = p1_rows[0] if p1_rows else {}
    p2 = p2_rows[0] if p2_rows else {}
    return {
        "company_name": p1.get("company_name", ""),
        "industry": p1.get("industry", ""),
        "growth_pct": _to_float(p1.get("company_growth_pct")),
        "median_growth_pct": _to_float(p1.get("industry_median_growth_pct")),
        "new_share_pct": _to_float(p2.get("company_new_product_share_pct")),
        "median_new_share_pct": _to_float(p2.get("industry_median_new_product_share_pct")),
        "labour_quote": quote,
        "labour_role": role,
    }


def _threshold_verdict(value: float | None, median: float | None) -> str:
    """Apply a 'at least the industry median' rule; missing evidence fails."""
    if value is None or median is None:
        return "FAIL"
    return "PASS" if value >= median else "FAIL"


def _labour_heuristic_verdict(quote: str) -> str:
    """Offline fallback for point 7, used only when no model is available."""
    if _is_non_answer(quote):
        return "FAIL"  # no positive evidence
    text = quote.lower()
    negative = (
        "quit", "left because", "turnover was terrible", "high turnover", "overtime",
        "strike", "morale is low", "unhappy", "underpaid", "exhaust", "stress",
        "layoff", "no training", "low pay",
    )
    positive = (
        "good place", "stay for years", "fair", "treated workers well", "hardly anyone",
        "very low", "morale is high", "good pay", "recommend", "training were good", "happy",
    )
    negatives = sum(cue in text for cue in negative)
    positives = sum(cue in text for cue in positive)
    return "FAIL" if negatives > positives else "PASS"


def _json_object(text: str) -> dict[str, Any]:
    """Read the first JSON object in a model reply, tolerating code fences."""
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S)
    if fenced:
        stripped = fenced.group(1)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        value = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _verdict(value: Any) -> str | None:
    text = str(value).strip().upper()
    return text if text in VERDICTS else None


def _dotenv_value(key: str, path: Path = Path(".env")) -> str | None:
    """Read one KEY=VALUE from .env without touching os.environ."""
    if not path.is_file():
        return None
    value = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, candidate = line.split("=", 1)
        if name.strip() == key:
            value = candidate.strip().strip("\"'")
    return value or None


def _mask_key(text: str) -> str:
    key = os.environ.get(API_KEY_ENV) or ""
    return text.replace(key, "<key>") if key else text


def _report_model_failure(context: str, exc: Exception) -> None:
    """Print the first few model errors instead of hiding them behind the fallback."""
    global _ERRORS_REPORTED
    _ERRORS_REPORTED += 1
    if _ERRORS_REPORTED > 3 and os.environ.get("HW3_DEBUG") != "1":
        return
    message = _mask_key(f"{type(exc).__name__}: {exc}")
    cause = exc.__cause__ or exc.__context__
    if cause is not None and str(cause) and str(cause) != str(exc):
        message += _mask_key(f" | cause: {type(cause).__name__}: {cause}")
    print(f"Model call failed [{context}]: {message[:800]}")
    if _ERRORS_REPORTED == 1:
        print(
            "  Hints: 401 = bad or expired key; 400 or 'model not found' = wrong model name; "
            "402/429 = billing or rate limit; connection/TLS = network, proxy or certificates. "
            "Set HW3_DEBUG=1 to print every error."
        )
        if "takes no keyword arguments" in message:
            print(
                "  This is the old brotli + httpx2 decoder bug. The agent already forces gzip; "
                "if it persists run: pip install -U \"httpx2>=2.16\"."
            )


async def _model_text(model: Any, system_prompt: str, user_prompt: str) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    global LIVE_MODEL_CALLS, LIVE_MODEL_ATTEMPTS
    LIVE_MODEL_ATTEMPTS += 1
    response = await model.ainvoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    )
    LIVE_MODEL_CALLS += 1
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = " ".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    return str(content).strip()


def _evidence_block(evidence: dict[str, Any]) -> str:
    growth = evidence.get("growth_pct")
    median_growth = evidence.get("median_growth_pct")
    new_share = evidence.get("new_share_pct")
    median_new = evidence.get("median_new_share_pct")
    return (
        f"Company: {evidence.get('company_name') or 'unknown'} "
        f"(industry: {evidence.get('industry') or 'unknown'}).\n"
        f"Point 1 - FY2023 to FY2025 revenue growth: {growth}%; "
        f"industry median revenue_growth_2y_pct: {median_growth}%.\n"
        f"Point 2 - share of FY2025 revenue from products launched since 2023-01-01: "
        f"{new_share}%; industry median new_product_share_pct: {median_new}%.\n"
        f"Point 7 - interview ({evidence.get('labour_role')}): {evidence.get('labour_quote')}\n"
    )


def _lookup(data: dict[str, Any], *names: str) -> Any:
    """Read a JSON field while tolerating case, spaces and underscores in its name."""
    wanted = {name.lower().replace("_", "").replace(" ", "") for name in names}
    for key, value in data.items():
        if str(key).lower().replace("_", "").replace(" ", "") in wanted:
            return value
    return None


async def _judge_labour(model: Any, evidence: dict[str, Any]) -> str:
    """Model verdict for the qualitative point 7."""
    quote = evidence.get("labour_quote", "")
    if _is_non_answer(quote):
        return "FAIL"  # nobody who knows the topic gave evidence
    if model is None:
        return _labour_heuristic_verdict(quote)
    user_prompt = (
        f"Fisher's point 7: {FISHER_POINTS[7]}\n"
        f"My rule: {POINT_RULES[7]}\n"
        f"Interview quote: {quote}\n"
        "Give the verdict and one sentence of reasoning as JSON."
    )
    try:
        reply = await _model_text(model, JUDGE_SYSTEM_PROMPT, user_prompt)
        return _verdict(_lookup(_json_object(reply), "verdict")) or _labour_heuristic_verdict(quote)
    except Exception as exc:
        _report_model_failure("point 7 judge", exc)
        return _labour_heuristic_verdict(quote)


async def _review_labour(model: Any, evidence: dict[str, Any], draft: str) -> str | None:
    """Independent second opinion on point 7; None means the reviewer could not answer."""
    if model is None:
        return None
    user_prompt = (
        f"{_evidence_block(evidence)}\n"
        f"My rule: {POINT_RULES[7]}\n"
        f"Draft verdict: {draft}\n"
        "Check the quote and return JSON with point7."
    )
    try:
        reply = await _model_text(model, REVIEW_SYSTEM_PROMPT, user_prompt)
        return _verdict(_lookup(_json_object(reply), "point7", "verdict"))
    except Exception as exc:
        _report_model_failure("point 7 review", exc)
        return None


def _combine_labour_verdicts(judge: str, reviewer: str | None, heuristic: str) -> str:
    """Majority of the judge, the independent reviewer and the cue-word check.

    A single bad model reply can no longer flip a clear quote: the other two
    signals outvote it. Ties cannot occur with three PASS/FAIL votes.
    """
    votes = [judge, reviewer, heuristic]
    votes = [vote for vote in votes if vote in VERDICTS]
    for candidate in (judge, reviewer):
        if candidate in VERDICTS and votes.count(candidate) >= 2:
            return candidate
    return votes[0] if votes else "FAIL"


def _fallback_summary(evidence: dict[str, Any], points: dict[int, str]) -> str:
    name = evidence.get("company_name") or "The company"
    quote = evidence.get("labour_quote") or "no interview answer"
    return (
        f"{name} grew revenue from FY2023 to FY2025 by {evidence.get('growth_pct')}% against an "
        f"industry median of {evidence.get('median_growth_pct')}% (point 1: {points.get(1)}). "
        f"Products launched since 2023 provided {evidence.get('new_share_pct')}% of FY2025 revenue "
        f"against a median of {evidence.get('median_new_share_pct')}% (point 2: {points.get(2)}). "
        f"On working conditions a {str(evidence.get('labour_role', 'staff')).replace('_', ' ')} "
        f"said: {quote} (point 7: {points.get(7)})."
    )


async def _write_summary(model: Any, evidence: dict[str, Any], points: dict[int, str]) -> str:
    """Write the note from the final verdicts so prose cannot contradict them."""
    fallback = _fallback_summary(evidence, points)
    if model is None:
        return fallback
    user_prompt = (
        f"{_evidence_block(evidence)}\n"
        f"Final verdicts: point 1 {points[1]}, point 2 {points[2]}, point 7 {points[7]}.\n"
        "Write two or three sentences explaining these verdicts."
    )
    try:
        text = await _model_text(model, SUMMARY_SYSTEM_PROMPT, user_prompt)
        return text.strip() or fallback
    except Exception as exc:
        _report_model_failure("summary", exc)
        return fallback


async def _analyze_company(
    model: Any, folder_name: str, tools: list[Any]
) -> dict[str, Any]:
    tools_by_name = {getattr(tool, "name", str(index)): tool for index, tool in enumerate(tools)}
    evidence = await _gather_evidence(tools_by_name)
    quote = evidence.get("labour_quote", "")

    judge = await _judge_labour(model, evidence)
    reviewer = await _review_labour(model, evidence, judge)
    points = {
        1: _threshold_verdict(evidence["growth_pct"], evidence["median_growth_pct"]),
        2: _threshold_verdict(evidence["new_share_pct"], evidence["median_new_share_pct"]),
        7: _combine_labour_verdicts(judge, reviewer, _labour_heuristic_verdict(quote)),
    }
    summary = await _write_summary(model, evidence, points)
    if not evidence.get("company_name"):
        summary = f"{folder_name}: {summary}"
    return {"points": points, "summary": summary}


def build_agent() -> Any:
    """Create the DeepSeek chat model once.

    The key is read from ``KEY=VALUE`` lines in ``.env`` (gitignored) or from the
    ``DEEPSEEK_API_KEY`` environment variable; when both exist and differ the
    ``.env`` value wins, so a stale export cannot shadow the key you just saved.
    It is never read from the source code. ``DEEPSEEK_API_BASE`` is optional and
    lets you point at a different endpoint. Without a key the pipeline still runs
    with the deterministic fallback, so check the printed mode line below.
    """
    load_env_file()
    dotenv_key = _dotenv_value(API_KEY_ENV)
    exported_key = os.environ.get(API_KEY_ENV)
    if dotenv_key:
        if exported_key and exported_key != dotenv_key:
            print(
                f"Note: {API_KEY_ENV} in .env differs from the one exported in the shell; "
                "using the .env value."
            )
        os.environ[API_KEY_ENV] = dotenv_key
        key_source = ".env"
    elif exported_key:
        key_source = "the environment"
    else:
        print(f"Agent mode: deterministic fallback ({API_KEY_ENV} is not set).")
        return {"model": None}

    try:
        from langchain_deepseek import ChatDeepSeek

        model = ChatDeepSeek(
            model=MODEL_NAME,
            temperature=0,
            max_retries=2,
            timeout=45,
            # Some environments ship an old `brotli` whose Decompressor.process()
            # rejects the keyword that httpx2 passes it, which crashes every call
            # with "process() takes no keyword arguments". Asking for gzip/deflate
            # keeps the Brotli decoder out of the path on old and new versions.
            default_headers={"Accept-Encoding": "gzip, deflate"},
        )
    except Exception as exc:
        print(f"Agent mode: deterministic fallback (could not create {MODEL_NAME}: {exc}).")
        return {"model": None}

    base = os.environ.get(API_BASE_ENV) or "https://api.deepseek.com/v1"
    print(f"Agent mode: live {MODEL_NAME} at {base} ({API_KEY_ENV} from {key_source}).")
    return {"model": model}


async def analyze_companies(agent: Any, companies: dict[str, list[Any]]) -> dict[str, dict[str, Any]]:
    """Run the per-company pipeline (routing, judging, reflection) with a concurrency cap."""
    global LIVE_MODEL_CALLS, LIVE_MODEL_ATTEMPTS, _ERRORS_REPORTED
    LIVE_MODEL_CALLS = 0
    LIVE_MODEL_ATTEMPTS = 0
    _ERRORS_REPORTED = 0
    if isinstance(agent, dict):
        model = agent.get("model")
    else:
        model = getattr(agent, "model", None)
    semaphore = asyncio.Semaphore(MAX_COMPANIES_IN_FLIGHT)

    async def run_one(name: str, tools: list[Any]) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                return name, await _analyze_company(model, name, tools)
            except Exception as exc:  # one bad company must not kill the run
                return name, {
                    "points": {point: "FAIL" for point in CHOSEN_POINTS},
                    "summary": f"Analysis failed for {name}: {exc}",
                }

    pairs = await asyncio.gather(*(run_one(name, tools) for name, tools in companies.items()))
    if model is not None:
        print(
            f"Live model calls: {LIVE_MODEL_CALLS}/{LIVE_MODEL_ATTEMPTS} succeeded "
            "(a working key should give about 3 per company)."
        )
        if LIVE_MODEL_CALLS == 0:
            print(
                "WARNING: no model call succeeded, so the results above come from the "
                "deterministic fallback; fix the error printed above and rerun."
            )
    return dict(pairs)


# Everything below is provided runner/scoring code. No edits are needed.
# Your code must read company data only through these tools: do not open
# company.db or interviews.dat directly.

INTERVIEWS_USED: dict[str, int] = {}


def company_folders(folder: Path) -> list[Path]:
    """Return one sub-folder per company directly inside *folder*, sorted by name."""
    return sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))


def make_tools(company_folder: Path) -> list[Any]:
    """Build the four tools for one company."""
    from langchain_core.tools import tool

    name = company_folder.name
    db_uri = f"{(company_folder / 'company.db').resolve().as_uri()}?mode=ro"
    answers = json.loads(zlib.decompress((company_folder / "interviews.dat").read_bytes()))
    documents = {p.name: p for p in sorted(company_folder.glob("*.md"))}
    lock = threading.Lock()
    INTERVIEWS_USED[name] = 0

    @tool
    def list_documents() -> str:
        """List this company's documents (annual letter, earnings calls, news) with their dates."""
        lines = []
        for doc_name, path in documents.items():
            text = path.read_text(encoding="utf-8").splitlines()
            dated = next((line for line in text if line.startswith("Date:")), "")
            lines.append(f"{doc_name}: {text[0].lstrip('# ')} ({dated or 'undated'})")
        return "\n".join(lines)

    @tool
    def read_document(name: str) -> str:
        """Return the full text of one document named by list_documents, e.g. 'annual_letter_2025.md'."""
        path = documents.get(name.strip())
        if path is None:
            return f"No document named {name!r}. Available: {', '.join(documents)}"
        return path.read_text(encoding="utf-8")

    @tool
    def sql_query(query: str) -> str:
        """Run one read-only SQLite query on this company's database and return at most 50 rows.

        Amounts are in USD thousands. Dates are 'YYYY-MM-DD', months 'YYYY-MM', quarters like '2025Q3'.
        Tables:
          company(name, industry)
          industry_medians(industry, revenue_growth_2y_pct, new_product_share_pct, new_product_sales_per_rd, operating_margin_pct)
          orders(order_id, order_date, product, customer, amount, note)
          products(product, launch_date)
          monthly_costs(month, cost_of_sales, selling_admin, research_development)
          quarterly_cash(quarter, operating_cash_flow, capex, cash_end)
          capex_plan(year, planned_capex)
          returns(month, units_shipped, units_returned)
          insider_trades(trade_date, person, title, side, shares, price)
        Use SUM, GROUP BY and WHERE to compute totals rather than reading raw rows.
        """
        try:
            con = sqlite3.connect(db_uri, uri=True)
            try:
                cursor = con.execute(query)
                columns = [c[0] for c in cursor.description or []]
                rows = cursor.fetchmany(MAX_SQL_ROWS + 1)
            finally:
                con.close()
        except sqlite3.Error as exc:
            return f"SQL error: {exc}"
        if not columns:
            return "Query returned no columns."
        fmt = lambda v: f"{v:.2f}" if isinstance(v, float) else str(v)  # noqa: E731
        out = [" | ".join(columns)] + [" | ".join(fmt(v) for v in row) for row in rows[:MAX_SQL_ROWS]]
        if len(rows) > MAX_SQL_ROWS:
            out.append(f"... more rows not shown (limit {MAX_SQL_ROWS}); aggregate or add LIMIT/OFFSET.")
        if not rows:
            out.append("(no rows)")
        return "\n".join(out)

    @tool
    def interview(role: str, topic: str) -> str:
        """Interview one person about the company (at most 10 interviews per company).

        role: customer, current_employee, former_employee, competitor or supplier.
        topic: sales_team, working_conditions, top_management, decision_making or competitive_edge.
        People who would not know about a topic say so; the README lists who knows what.
        """
        role, topic = role.strip().lower(), topic.strip().lower()
        if role not in answers:
            return f"Unknown role {role!r}. Roles: {', '.join(answers)}. (Not counted.)"
        if topic not in answers[role]:
            return f"Unknown topic {topic!r}. Topics: {', '.join(answers[role])}. (Not counted.)"
        with lock:
            if INTERVIEWS_USED[name] >= INTERVIEW_LIMIT:
                return f"Interview limit reached: {INTERVIEW_LIMIT} interviews per company."
            INTERVIEWS_USED[name] += 1
        return answers[role][topic]

    return [list_documents, read_document, sql_query, interview]


def normalize_verdict(value: Any) -> str | None:
    """Accept PASS/FAIL (any case) or a boolean."""
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    text = str(getattr(value, "content", value)).strip().upper()
    return text if text in VERDICTS else None


def check_chosen_points(chosen: Any) -> list[int]:
    """CHOSEN_POINTS must name 3 different points between 1 and 15."""
    try:
        points = sorted({int(p) for p in chosen})
    except (TypeError, ValueError):
        points = []
    if len(points) != 3 or len(chosen) != 3 or not all(p in FISHER_POINTS for p in points):
        raise SystemExit("Set CHOSEN_POINTS at the top of hw3.py to 3 different point numbers "
                         f"between 1 and 15, e.g. [1, 7, 15] (now: {chosen!r}).")
    return points


def normalize_points(points: Any, chosen: list[int]) -> dict[int, str | None]:
    """Map each chosen point number to PASS/FAIL, or None when missing/invalid."""
    points = points if isinstance(points, dict) else {}
    by_number: dict[int, Any] = {}
    for key, value in points.items():
        try:
            by_number[int(key)] = value
        except (TypeError, ValueError):
            continue
    return {p: normalize_verdict(by_number.get(p)) for p in chosen}


def read_ground_truth(folder: Path) -> dict[str, dict[str, Any]]:
    """Read the expected 15 point verdicts per company."""
    path = folder / "ground_truth.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def point_errors(points: dict[int, str | None], expected: dict[str, Any] | None) -> tuple[int, str]:
    """Count correct point verdicts and describe the wrong ones."""
    if expected is None:
        return 0, ""
    wrong = []
    for p in points:
        truth = expected["points"][str(p)]
        if points[p] != truth:
            wrong.append(f"{p}: expected {truth}, predicted {points[p] or 'missing'}")
    if all(v is None for v in points.values()):
        return 0, "all point verdicts missing"
    return len(points) - len(wrong), "; ".join(wrong)


def balanced_point_accuracy(
    analyses: dict[str, Any], truth: dict[str, dict[str, Any]], names: list[str], chosen: list[int]
) -> float:
    """Mean of the accuracy on true-PASS points and on true-FAIL points.

    Most verdicts are PASS, so answering PASS everywhere gets a high plain
    accuracy but only 50% balanced accuracy. Grading uses the balanced number.
    """
    hits = {"PASS": [0, 0], "FAIL": [0, 0]}
    for name in names:
        if name not in truth:
            continue
        entry = analyses.get(name)
        points = normalize_points(entry.get("points") if isinstance(entry, dict) else None, chosen)
        for p in chosen:
            expected = truth[name]["points"][str(p)]
            hits[expected][0] += points[p] == expected
            hits[expected][1] += 1
    rates = [h / n for h, n in hits.values() if n]
    return sum(rates) / len(rates) if rates else 0.0


def write_results(
    names: list[str], analyses: dict[str, Any], truth: dict[str, dict[str, Any]], chosen: list[int]
) -> tuple[Path, int]:
    """Write results.csv and return the number of correct point verdicts."""
    output = Path("results.csv")
    correct_points = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["company", "points", "points_correct", "point_errors", "interviews_used", "summary"])
        for name in names:
            entry = analyses.get(name)
            entry = entry if isinstance(entry, dict) else {}
            points = normalize_points(entry.get("points"), chosen)
            expected = truth.get(name)
            n_points, errors = point_errors(points, expected)
            correct_points += n_points
            writer.writerow([
                name,
                " ".join(f"{p}:{points[p] or 'missing'}" for p in chosen),
                f"{n_points}/{len(chosen)}" if expected else "",
                errors,
                INTERVIEWS_USED.get(name, 0),
                str(entry.get("summary", "")).strip(),
            ])
    return output, correct_points


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run FTEC5660 HW3 on company data folders")
    parser.add_argument(
        "--data-folder",
        required=True,
        type=Path,
        help="folder containing one sub-folder per company",
    )
    return parser.parse_args()


async def run(folder: Path) -> int:
    folders = company_folders(folder)
    if not folders:
        raise SystemExit(f"no company folders found in {folder}")

    chosen = check_chosen_points(CHOSEN_POINTS)
    load_env_file()
    companies = {path.name: make_tools(path) for path in folders}
    agent = build_agent()
    analyses = await analyze_companies(agent, companies)
    if not isinstance(analyses, dict):
        raise TypeError("analyze_companies() must return a dictionary")

    truth = read_ground_truth(folder)
    output, correct_points = write_results(list(companies), analyses, truth, chosen)
    summary = f" Your points: {', '.join(map(str, chosen))}."
    if truth:
        summary += (
            f" Point accuracy: {correct_points}/{len(companies) * len(chosen)}"
            f" (balanced: {balanced_point_accuracy(analyses, truth, list(companies), chosen):.1%})."
        )
    print(f"Processed {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}. Wrote {output}.{summary}")
    return 0


def main() -> int:
    args = parse_args()
    if not args.data_folder.is_dir():
        raise SystemExit(f"not a folder: {args.data_folder}")
    return asyncio.run(run(args.data_folder))


if __name__ == "__main__":
    raise SystemExit(main())
