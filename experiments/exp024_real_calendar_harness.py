#!/usr/bin/env python3
"""EXP-024 harness -- news protection measured against the REAL historical
MQL5 economic calendar, at ~$3,000 equity. MEASUREMENT, not selection: two arms
(C = no protection, A@real = live's mode A driven by the real calendar) plus two
CONTINUITY ANCHORS (A@P1, A@P2) that must reproduce EXP-023/EXP-025's published
numbers. There is no grid, no candidate, and no config change can follow from
this file.

WHY (2026-08-04): EXP-023 D1 found that `backtest/engine.py` does not model news
protection at all -- every number in `experiments/experiments_log.md` describes
mode C while live runs mode A (at min-lot `watchman/loop._half_volume_rounded`
returns None and CLOSE_HALF_AND_BREAKEVEN recurses into CLOSE_ALL). EXP-023 could
only BRACKET the cost (P1 ~60% of trades affected, P2 ~46%) because no historical
calendar existed (its D2); EXP-025 then showed the trigger LEVEL is not the lever.
The 2026-08-04 calendar DUMP (`mql5/CalendarHistoryDump.mq5`, 73,699 rows back to
2021-07-01) retires D2, so the trigger rate is now measurable rather than assumed.

LIVE SEMANTICS REPRODUCED HERE (cited in the log's EXP-024 section 4):
  * currencies -- `watchman/news_protection._news_incoming` ->
    `council/risk_voice.get_symbol_currencies("XAUUSD") == ("USD",)`. USD only.
  * impact -- `council/mql5_calendar_provider` keeps a row iff
    `event.impact.lower() == "high"`.
  * window -- `_news_incoming` asks for `[now, now + news_window_minutes]` and
    `get_high_impact_events` admits `window_start <= event_time <= window_end`:
    INCLUSIVE both ends and FORWARD-LOOKING ONLY. Protection is active at instant
    `now` iff some high-impact USD event `e` has `now <= e <= now + 30min`, i.e.
    `now` in `[e - 30min, e]`. There is NO post-event window.
  * profit gate -- `check_news_protection` short-circuits below
    `profit_threshold_r = 0.5`, measured against the ORIGINAL stop distance.
  * bar resolution -- live polls every ~5s; the backtest sees H1 bars. A bar with
    open time `t` spanning `[t, t+bar)` is trigger-eligible iff some event `e` has
    `t < e < t + bar + window`, i.e. the eligible poll instants inside the bar form
    a set of positive duration. Declared in the pre-registration, not chosen later.

TIMEZONE NORMALISATION (mandatory, from the 2026-08-04 log NOTE -- implemented,
not re-derived): the dump stamps ALL history as UTC + the CURRENT server offset
(+3), while the H1 bars are stamped in the true per-date server clock (UTC+2 in
US-DST-off, UTC+3 in US-DST-on). So subtract 1h from every event time whose date
falls in a US-DST-OFF period, with the real US rule (second Sunday of March ->
first Sunday of November), not month granularity. Self-check G3: every in-range
high-impact-USD "Nonfarm Payrolls" row must land at 15:30 server afterwards.

EVERYTHING ELSE IS INHERITED, NOT REWRITTEN. The engine bar-loop copy, the
EXP-022 fast-path shim, the conditional/no-reshuffle replay, the metrics and the
fidelity/anchor modes all come from `exp025_news_threshold_harness`, which was
itself proven identical to `backtest.engine.run_backtest` with the news mechanism
off. This file adds exactly one thing: an eligibility PRE-FILTER in front of
EXP-025's own `_news_trigger_price`, so that A@P1/A@P2 traverse literally the same
code path as EXP-025 (that is what makes them usable as continuity anchors).

This file modifies NOTHING under `src/` or `config/`.

MODES:
  --mode calendar     calendar diagnostics + the G3 NFP normalisation self-check
  --mode livecv       live cross-validation gates L1/L2 against the paper journal
  --mode fidelity     news OFF == real engine, trade-for-trade (delegates to EXP-025)
  --mode anchor       mode C on y4/VAL == 254 / PF 1.0961 / +$352.60 / 9.99% maxDD
  --mode conditional  THE MEASUREMENT: affected@real, paired A - C, exit mixes
  --mode portfolio    full-sequence per arm: the portfolio-level parity gap
  --mode pool         pool a conditional output file into Train / Val statistics
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The PRODUCTION parser, so this harness cannot drift from what live reads.
from autotrade.council.mql5_calendar_provider import parse_export_csv  # noqa: E402
from autotrade.council.risk_voice import get_symbol_currencies  # noqa: E402
from autotrade.common.config import load_yaml_config  # noqa: E402
from autotrade.feed.historical import HISTORICAL_DIR  # noqa: E402

import exp025_news_threshold_harness as e25  # noqa: E402

DEFAULT_CALENDAR = Path(
    r"C:\Users\Varintha\AppData\Roaming\MetaQuotes\Terminal\Common\Files"
    r"\AutoTradeNewsCalendarHistory.csv"
)
DEFAULT_JOURNAL = Path(__file__).resolve().parent.parent / "trade_journal_paper_vps_latest.sqlite"

SYMBOL = "XAUUSD"
# config/base.yaml watchman.* -- NOT swept anywhere in this experiment.
NEWS_WINDOW_MINUTES = 30.0
PROFIT_THRESHOLD_R = 0.5

# EXP-023/EXP-025 published numbers this harness must reproduce (gate G4).
ANCHOR_AFFECTED = {
    "P1": {"y1_2021-22": 162, "y2_2022-23": 152, "y3_2023-24": 149, "y4_VAL_2024-25": 154},
    "P2": {"y1_2021-22": 124, "y2_2022-23": 116, "y3_2023-24": 116, "y4_VAL_2024-25": 124},
}
ANCHOR_AVGR_P1 = {"y1_2021-22": 0.501, "y2_2022-23": 0.497, "y3_2023-24": 0.494, "y4_VAL_2024-25": 0.495}

# This experiment's data range. The TEST year is outside it, by pre-registration.
RANGE_START = datetime(2021, 7, 22)
RANGE_END = datetime(2025, 7, 22)


# --------------------------------------------------------------- calendar
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(days=7 * (n - 1))


def us_dst_on(ts: datetime) -> bool:
    """US DST: second Sunday of March (inclusive) -> first Sunday of November
    (exclusive). Sunday == weekday 6. Date granularity, per the pre-registration:
    the residual error is confined to the two transition Sundays, on which the US
    high-impact release calendar is empty."""
    return _nth_weekday(ts.year, 3, 6, 2) <= ts.date() < _nth_weekday(ts.year, 11, 6, 1)


def normalise_event_time(raw: datetime) -> datetime:
    """Dump stamps = UTC + the CURRENT server offset (+3) applied to ALL history.
    Bars are stamped in the true per-date server clock (+2 in US-DST-off, +3 in
    US-DST-on). So DST-off rows are 1h late relative to the bar clock."""
    return raw if us_dst_on(raw) else raw - timedelta(hours=1)


@dataclass
class Calendar:
    events: list[datetime]                 # normalised, sorted, unique, high-impact, symbol currencies
    raw_rows: int = 0
    deduped_rows: int = 0
    matched_rows: int = 0
    currencies: tuple[str, ...] = ()
    nfp_in_range: list[tuple[str, str]] = field(default_factory=list)     # (raw, normalised)
    nfp_out_of_range: list[tuple[str, str]] = field(default_factory=list)

    def active_at(self, now: datetime, window_minutes: float = NEWS_WINDOW_MINUTES) -> list[datetime]:
        """EXACT live semantics: `now <= e <= now + window` (inclusive both ends,
        forward-looking only) -- `news_protection._news_incoming` +
        `MQL5CalendarProvider.get_high_impact_events`."""
        end = now + timedelta(minutes=window_minutes)
        lo = np.searchsorted(self._arr, np.datetime64(now), side="left")
        hi = np.searchsorted(self._arr, np.datetime64(end), side="right")
        return [self.events[i] for i in range(lo, hi)]

    def __post_init__(self) -> None:
        self._arr = np.array([np.datetime64(e) for e in self.events], dtype="datetime64[s]")


def load_calendar(path: Path, symbol: str = SYMBOL) -> Calendar:
    """Read the dump with the PRODUCTION parser, dedup on
    (event_time, currency, importance, event_name) exactly like
    `council/calendar_archive.py`, filter to the symbol's news currencies and
    `impact.lower() == "high"` exactly like `MQL5CalendarProvider`, then apply the
    mandatory DST normalisation."""
    rows = parse_export_csv(path.read_text(encoding="ascii", errors="replace"))
    if rows is None:
        raise SystemExit(f"REFUSED: {path} is structurally unparseable by the production parser.")
    seen: set[tuple[str, str, str, str]] = set()
    deduped = []
    for r in rows:
        key = (r["event_time"], r["currency"], r["importance"], r["event_name"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    currencies = get_symbol_currencies(symbol)
    if not currencies:
        raise SystemExit(f"REFUSED: {symbol} has no news-currency mapping in risk_voice._SYMBOL_CURRENCIES.")

    events: set[datetime] = set()
    nfp_in, nfp_out = [], []
    matched = 0
    for r in deduped:
        if r["currency"] not in currencies:
            continue
        if r["importance"].lower() != "high":
            continue
        raw = datetime.strptime(r["event_time"], "%Y-%m-%d %H:%M:%S")
        norm = normalise_event_time(raw)
        matched += 1
        events.add(norm)
        if r["event_name"] == "Nonfarm Payrolls":
            bucket = nfp_in if RANGE_START <= norm < RANGE_END else nfp_out
            bucket.append((raw.strftime("%Y-%m-%d %H:%M"), norm.strftime("%Y-%m-%d %H:%M")))
    return Calendar(
        events=sorted(events), raw_rows=len(rows), deduped_rows=len(deduped),
        matched_rows=matched, currencies=currencies,
        nfp_in_range=sorted(nfp_in), nfp_out_of_range=sorted(nfp_out),
    )


def check_nfp_gate(cal: Calendar) -> tuple[bool, dict]:
    """Gate G3, pre-registered: every IN-RANGE high-impact-USD `Nonfarm Payrolls`
    row must normalise to exactly 15:30 server. Out-of-range violations are
    reported, not fatal (they cannot touch a number this experiment produces)."""
    bad_in = [p for p in cal.nfp_in_range if not p[1].endswith("15:30")]
    bad_out = [p for p in cal.nfp_out_of_range if not p[1].endswith("15:30")]
    return not bad_in, {
        "nfp_in_range_n": len(cal.nfp_in_range), "nfp_in_range_violations": bad_in,
        "nfp_out_of_range_n": len(cal.nfp_out_of_range), "nfp_out_of_range_violations": bad_out,
    }


def eligible_bar_times(df: pd.DataFrame, cal: Calendar,
                       window_minutes: float = NEWS_WINDOW_MINUTES) -> tuple[frozenset, int]:
    """Bar open times whose bar is TRIGGER-ELIGIBLE. A bar `[t, t+bar)` is eligible
    iff the eligible poll instants inside it (`[e - window, e]` for some event `e`)
    form a set of positive duration, i.e. iff `t < e < t + bar + window`. Returns
    (set of pd.Timestamp taken from df itself, bar_minutes actually inferred)."""
    times = pd.to_datetime(df["time"]).to_numpy(dtype="datetime64[s]")
    diffs = np.diff(times).astype("timedelta64[m]").astype(int)
    diffs = diffs[diffs > 0]
    bar_minutes = int(np.bincount(diffs).argmax()) if len(diffs) else 60
    span = np.timedelta64(int(bar_minutes + window_minutes), "m")

    mask = np.zeros(len(times), dtype=bool)
    for e in cal.events:
        ev = np.datetime64(e, "s")
        lo = np.searchsorted(times, ev - span, side="right")   # t > e - (bar + window)
        hi = np.searchsorted(times, ev, side="left")           # t < e
        if hi > lo:
            mask[lo:hi] = True
    stamps = pd.to_datetime(df["time"])
    return frozenset(stamps[mask].tolist()), bar_minutes


# ------------------------------------------------- the one added mechanism
@dataclass(frozen=True)
class NewsSim24:
    """Duck-typed stand-in for EXP-025's `NewsSim` (`_step_position` only reads
    `.mode` and hands the object to `_news_trigger_price`). `eligible_times=None`
    reproduces EXP-025's arms byte-for-byte; a non-None set adds the real-calendar
    pre-filter and nothing else."""

    mode: str = "off"                      # "off" | "close_all"
    profit_threshold_r: float = PROFIT_THRESHOLD_R
    hours: frozenset[int] | None = None    # P2 hour mask (EXP-025's proxy)
    eligible_times: frozenset | None = None
    name: str = "C_none"

    @property
    def label(self) -> str:
        return self.name


_E25_TRIGGER_PRICE = e25._news_trigger_price


def _news_trigger_price24(pos, bar, news):
    """The ONLY new mechanism in EXP-024: a real-calendar eligibility pre-filter in
    front of EXP-025's own, already-validated trigger-price function. With
    `eligible_times is None` this is EXP-025 verbatim -- which is what makes the
    A@P1/A@P2 continuity anchors meaningful."""
    elig = getattr(news, "eligible_times", None)
    if elig is not None and bar["time"] not in elig:
        return None
    return _E25_TRIGGER_PRICE(pos, bar, news)


e25._news_trigger_price = _news_trigger_price24


# ------------------------------------------------------------------ modes
def _windows(args):
    if args.window in (None, "all"):
        return e25.TRAIN_VAL
    return [y for y in e25.TRAIN_VAL if y[0] == args.window]


def _arms(args, elig: frozenset) -> list[NewsSim24]:
    out = [NewsSim24(mode="off", name="C_none")]
    wanted = [a.strip() for a in args.arms.split(",")]
    if "real" in wanted:
        out.append(NewsSim24(mode="close_all", eligible_times=elig, name="A_real"))
    if "p1" in wanted:
        out.append(NewsSim24(mode="close_all", name="A_P1"))
    if "p2" in wanted:
        out.append(NewsSim24(mode="close_all", hours=e25.NEWSHOURS, name="A_P2"))
    return out


def mode_calendar(df, cfg, args) -> int:
    cal = load_calendar(Path(args.calendar))
    ok, nfp = check_nfp_gate(cal)
    print("CAL " + json.dumps({
        "path": str(args.calendar), "raw_rows": cal.raw_rows, "deduped_rows": cal.deduped_rows,
        "dup_rate_pct": round(100.0 * (cal.raw_rows - cal.deduped_rows) / max(cal.raw_rows, 1), 3),
        "currencies": list(cal.currencies), "high_impact_rows": cal.matched_rows,
        "unique_event_times": len(cal.events),
        "first": str(cal.events[0]), "last": str(cal.events[-1]),
    }), flush=True)
    print("G3_NFP " + json.dumps({"pass": ok, **nfp}), flush=True)
    if not ok:
        print("STOP: G3 failed -- in-range NFP rows do not normalise to 15:30 server.", flush=True)
        return 1

    in_range = [e for e in cal.events if RANGE_START <= e < RANGE_END]
    hours: dict[str, int] = {}
    for e in in_range:
        hours[e.strftime("%H:%M")] = hours.get(e.strftime("%H:%M"), 0) + 1
    top = dict(sorted(hours.items(), key=lambda kv: -kv[1])[:14])
    hour_only: dict[int, int] = {}
    for e in in_range:
        hour_only[e.hour] = hour_only.get(e.hour, 0) + 1
    in_p2 = sum(n for h, n in hour_only.items() if h in e25.NEWSHOURS)
    print("CALRANGE " + json.dumps({
        "range": [str(RANGE_START), str(RANGE_END)], "unique_event_times_in_range": len(in_range),
        "top_times": top, "by_hour": dict(sorted(hour_only.items())),
        "pct_of_events_inside_P2_hours": round(100.0 * in_p2 / max(len(in_range), 1), 2),
    }), flush=True)

    for name, s, e in _windows(args):
        sub = e25.slice_year(df, s, e)
        elig, bar_min = eligible_bar_times(sub, cal)
        print("ELIG " + json.dumps({
            "window": name, "bars": len(sub), "bar_minutes": bar_min,
            "eligible_bars": len(elig),
            "eligible_bars_pct": round(100.0 * len(elig) / max(len(sub), 1), 2),
        }), flush=True)
    return 0


def mode_livecv(df, cfg, args) -> int:
    """Live cross-validation gates L1 / L2 (log section 8). Directional by
    pre-registration: L1 (reconstruction active where live demonstrably did NOT
    fire) is a hard STOP; L2 misses are the documented, unmodelled fail-safe
    channel and are reported unless the majority miss."""
    cal = load_calendar(Path(args.calendar))
    con = sqlite3.connect(f"file:{args.journal}?mode=ro", uri=True)
    rows = list(con.execute(
        "select id, direction, entry_time, entry_price, exit_time, exit_price, exit_reason, r_multiple "
        "from trade_records order by entry_time"
    ))
    con.close()
    print("LIVE_JOURNAL " + json.dumps({"path": str(args.journal), "trades": len(rows)}), flush=True)

    def _ts(s: str) -> datetime:
        return datetime.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S")

    # --- L2: every live news_protection exit should be reconstructed ACTIVE.
    l2 = []
    for r in rows:
        if r[6] != "news_protection":
            continue
        t = _ts(r[4])
        ev = cal.active_at(t)
        l2.append({"id": r[0], "exit_time": str(t), "r": round(r[7], 4),
                   "reconstructed_active": bool(ev),
                   "events_in_window": [str(x) for x in ev]})
    hits = sum(1 for x in l2 if x["reconstructed_active"])
    l2_pass = len(l2) > 0 and hits >= (len(l2) + 1) // 2
    print("L2 " + json.dumps({"n_live_news_exits": len(l2), "reconstructed_active": hits,
                              "pass": l2_pass, "detail": l2}), flush=True)

    # --- L1: the +0.5R-passing trade whose window live demonstrably left alone.
    l1_detail = []
    l1_pass = True
    for r in rows:
        if r[6] == "news_protection" or r[7] is None or r[7] <= PROFIT_THRESHOLD_R:
            continue
        entry_t, exit_t = _ts(r[2]), _ts(r[4])
        entry_p, exit_p = r[3], r[5]
        one_r = abs(exit_p - entry_p) / abs(r[7])
        lvl = entry_p + one_r * PROFIT_THRESHOLD_R if r[1] == "BUY" else entry_p - one_r * PROFIT_THRESHOLD_R
        touch = _first_touch_m1(SYMBOL, r[1], lvl, entry_t, exit_t) if not args.no_mt5 else None
        start = touch or entry_t
        # scan every minute from the first +0.5R touch to the exit
        active_from = None
        t = start
        while t <= exit_t:
            if cal.active_at(t):
                active_from = t
                break
            t += timedelta(minutes=1)
        quiet_minutes = int(((active_from or exit_t) - start).total_seconds() // 60)
        entry = {"id": r[0], "r": round(r[7], 4), "exit_reason": r[6],
                 "half_r_level": round(lvl, 2),
                 "first_touch_m1": str(touch) if touch else None,
                 "scan_start": str(start), "exit_time": str(exit_t),
                 "reconstructed_active_from": str(active_from) if active_from else None,
                 "quiet_minutes_at_or_above_0.5R": quiet_minutes}
        # L1 fails only if the reconstruction is active from the very first
        # instant the position was protectable and live still did not act.
        if active_from is not None and active_from <= start:
            l1_pass = False
            entry["L1_violation"] = True
        l1_detail.append(entry)
    print("L1 " + json.dumps({"pass": l1_pass, "detail": l1_detail}), flush=True)

    ok = l1_pass and l2_pass
    print("LIVECV " + json.dumps({"L1_pass": l1_pass, "L2_pass": l2_pass, "overall": ok}), flush=True)
    return 0 if ok else 1


def _first_touch_m1(symbol, direction, level, start, end):
    """First minute at which price touched `level`, from the terminal's own M1
    history. Read-only, used ONLY by the live cross-validation gate (the paper
    window is outside every backtest window). Returns None if MT5 is unavailable."""
    try:
        import MetaTrader5 as mt5
        from datetime import timezone
    except Exception:
        return None
    if not mt5.initialize():
        return None
    try:
        rates = mt5.copy_rates_range(
            symbol, mt5.TIMEFRAME_M1,
            start.replace(tzinfo=timezone.utc), (end + timedelta(minutes=5)).replace(tzinfo=timezone.utc),
        )
    finally:
        mt5.shutdown()
    if rates is None or not len(rates):
        return None
    d = pd.DataFrame(rates)
    d["t"] = pd.to_datetime(d["time"], unit="s")
    d = d[d["t"] >= pd.Timestamp(start)]
    hit = d[d["high"] >= level] if direction == "BUY" else d[d["low"] <= level]
    return None if hit.empty else hit.iloc[0]["t"].to_pydatetime()


def mode_conditional(df, cfg, args) -> int:
    cal = load_calendar(Path(args.calendar))
    ok, nfp = check_nfp_gate(cal)
    if not ok:
        print("STOP: G3 failed " + json.dumps(nfp), flush=True)
        return 1
    bt = e25.build_bt_config(cfg, equity=args.equity, commission=args.commission, swap=not args.no_swap)

    for name, s, e in _windows(args):
        sub = e25.slice_year(df, s, e)
        elig, bar_min = eligible_bar_times(sub, cal)
        e25.install_fast_path(sub, cfg["global"]["swing_pivot_bars"])
        try:
            base_infos, base_origins = e25._run_with_origins(sub, bt, NewsSim24(mode="off"))
            # G5, inherited: the conditional replay must reproduce mode C exactly.
            selfcheck = [
                e25.replay_one(sub, SYMBOL, e25._SPEC, bt, NewsSim24(mode="off"), t, o).trade
                for t, o in zip(base_infos, base_origins)
            ]
            assert selfcheck == [t.trade for t in base_infos], "conditional replay diverged from mode C"

            for sim in _arms(args, elig):
                if sim.mode == "off":
                    continue
                infos = [e25.replay_one(sub, SYMBOL, e25._SPEC, bt, sim, t, o)
                         for t, o in zip(base_infos, base_origins)]
                aff = [k for k, x in enumerate(infos) if x.news_triggered]
                a_r = [infos[k].trade.r_multiple for k in aff]
                c_r = [base_infos[k].trade.r_multiple for k in aff]
                print("COND " + json.dumps({
                    "window": name, "arm": sim.label,
                    "bars": len(sub), "eligible_bars": len(elig),
                    "eligible_bars_pct": round(100.0 * len(elig) / max(len(sub), 1), 2),
                    "trades_total": len(base_infos),
                    "affected_n": len(aff),
                    "affected_pct": round(100.0 * len(aff) / max(len(base_infos), 1), 2),
                    "C_on_affected": e25.subset_stats([base_infos[k].trade for k in aff]),
                    "A_on_affected": e25.subset_stats([infos[k].trade for k in aff]),
                    "C_exit_mix": e25.exit_mix([base_infos[k] for k in aff]),
                    "A_exit_mix": e25.exit_mix([infos[k] for k in aff]),
                    "dR_A_minus_C": e25.paired_delta(c_r, a_r),
                }), flush=True)
        finally:
            e25.uninstall_fast_path()
    return 0


def mode_portfolio(df, cfg, args) -> int:
    cal = load_calendar(Path(args.calendar))
    bt = e25.build_bt_config(cfg, equity=args.equity, commission=args.commission, swap=not args.no_swap)
    for name, s, e in _windows(args):
        sub = e25.slice_year(df, s, e)
        elig, _ = eligible_bar_times(sub, cal)
        e25.install_fast_path(sub, cfg["global"]["swing_pivot_bars"])
        try:
            for sim in _arms(args, elig):
                infos = e25.run_backtest_news(sub, SYMBOL, e25._SPEC, bt, sim)
                trades = [i.trade for i in infos]
                print("PORT " + json.dumps({
                    "window": name, "arm": sim.label, **e25.cell(trades, args.equity),
                    "news_fired": sum(1 for i in infos if i.news_triggered),
                    "exit_mix": e25.exit_mix(infos),
                }), flush=True)
        finally:
            e25.uninstall_fast_path()
    return 0


def mode_pool(args) -> int:
    rows = []
    for line in Path(args.infile).read_text(encoding="utf-8").splitlines():
        if line.startswith("COND "):
            rows.append(json.loads(line[5:]))

    def _pool(records):
        n = sum(r["n"] for r in records)
        if n < 2:
            return {"n": n, "note": "insufficient"}
        sd = sum(r["sum_d"] for r in records)
        sd2 = sum(r["sum_d2"] for r in records)
        mean = sd / n
        var = (sd2 - n * mean * mean) / (n - 1)
        se = math.sqrt(max(var, 0.0) / n)
        return {"n": n, "mean_dR": round(mean, 4), "se_dR": round(se, 4),
                "t": round(mean / se, 3) if se else None,
                "ci95": [round(mean - 1.96 * se, 4), round(mean + 1.96 * se, 4)] if se else None,
                "below_100_floor": n < 100}

    for arm in sorted({r["arm"] for r in rows}):
        for split, wins in (("TRAIN", e25.TRAIN_WINDOWS), ("VAL", e25.VAL_WINDOWS)):
            sel = [r for r in rows if r["arm"] == arm and r["window"] in wins]
            if not sel:
                continue
            recs = [r["dR_A_minus_C"] for r in sel if r["dR_A_minus_C"].get("n", 0) >= 2]
            tot_trades = sum(r["trades_total"] for r in sel)
            tot_aff = sum(r["affected_n"] for r in sel)
            print("POOL " + json.dumps({
                "arm": arm, "split": split, "windows": [r["window"] for r in sel],
                "trades_total": tot_trades, "affected_n": tot_aff,
                "affected_pct": round(100.0 * tot_aff / max(tot_trades, 1), 2),
                **(_pool(recs) if recs else {"n": 0, "note": "no paired records"}),
            }), flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", required=True,
                   choices=["calendar", "livecv", "fidelity", "anchor", "conditional", "portfolio", "pool"])
    p.add_argument("--calendar", default=str(DEFAULT_CALENDAR))
    p.add_argument("--journal", default=str(DEFAULT_JOURNAL))
    p.add_argument("--arms", default="real,p1,p2", help="subset of real,p1,p2 (C is always included)")
    p.add_argument("--equity", type=float, default=3000.0)
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--window", default=None,
                   help="y1_2021-22 | y2_2022-23 | y3_2023-24 | y4_VAL_2024-25 | all")
    p.add_argument("--no-swap", action="store_true")
    p.add_argument("--no-mt5", action="store_true", help="livecv: skip the M1 first-touch lookup")
    p.add_argument("--infile", default=None, help="--mode pool: conditional output file to aggregate")
    args = p.parse_args()

    if args.window is not None and args.window.startswith("y5"):
        print("REFUSED: EXP-024 is pre-registered Train+Val only; the Test year stays untouched.", flush=True)
        return 2

    if args.mode == "pool":
        return mode_pool(args)

    cfg = load_yaml_config("base")
    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])

    if args.mode == "fidelity":
        return e25.mode_fidelity(df, cfg, args)
    if args.mode == "anchor":
        return e25.mode_anchor(df, cfg, args)
    if args.mode == "calendar":
        return mode_calendar(df, cfg, args)
    if args.mode == "livecv":
        return mode_livecv(df, cfg, args)
    if args.mode == "conditional":
        return mode_conditional(df, cfg, args)
    return mode_portfolio(df, cfg, args)


if __name__ == "__main__":
    sys.exit(main())
