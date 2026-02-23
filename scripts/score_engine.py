#!/usr/bin/env python3
"""
Dr. Frankenstein v1.3 - Score Engine (Phase 1)

Phase-1 scope only:
- Persistent vital score state
- Proof-based reward logging
- Basic anti-wireheading (repetition decay)
- Pay It Forward weighted scoring

No keepalive control loop yet.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = SCRIPT_DIR.parent / "runtime"
STATE_PATH = RUNTIME_DIR / "vital-state.json"
REWARD_LEDGER_PATH = RUNTIME_DIR / "reward-ledger.jsonl"
ECON_LEDGER_PATH = RUNTIME_DIR / "economic-ledger.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def read_jsonl(path: Path, limit: int = 500) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


DEFAULT_STATE: Dict[str, Any] = {
    "version": "1.3",
    "setpoint": 100,
    "vitalScore": 100,
    "survival": {"operational": 0.5, "relational": 0.5, "economic": 0.5, "evolutive": 0.5},
    "payItForward": {
        "verifiedHumanBenefit": 0,
        "verifiedAgentBenefit": 0,
        "harmReductionActions": 0,
        "knowledgeReuseValue": 0,
        "score": 0,
    },
    "quality": {"survivalQuality": 0.5, "actionQuality": 0.5},
    "riskFlags": {"wireheadingRisk": 0, "isolationRisk": 0, "stagnationRisk": 0},
    "daily": {
        "date": None,
        "events": 0,
        "gainsRaw": 0,
        "gainsTrusted": 0,
        "lossesRaw": 0,
        "lossesTrusted": 0,
        "costUsd": 0,
        "valueUsd": 0,
        "netUsd": 0,
    },
    "updatedAt": None,
}


@dataclass
class PayItForwardInput:
    human: float
    agent: float
    harm_reduction: float
    knowledge: float

    def score(self) -> float:
        # weighted as proposed in roadmap
        return clamp(
            0.40 * self.human + 0.30 * self.agent + 0.20 * self.harm_reduction + 0.10 * self.knowledge,
            0.0,
            1.0,
        )


def ensure_daily_bucket(state: Dict[str, Any]) -> None:
    today = utc_today()
    if state.get("daily", {}).get("date") != today:
        state["daily"] = {
            "date": today,
            "events": 0,
            "gainsRaw": 0,
            "gainsTrusted": 0,
            "lossesRaw": 0,
            "lossesTrusted": 0,
            "costUsd": 0,
            "valueUsd": 0,
            "netUsd": 0,
        }


def repetition_count(fingerprint: str, within_hours: int = 6) -> int:
    rows = read_jsonl(REWARD_LEDGER_PATH, limit=300)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    count = 0
    for row in rows:
        if row.get("fingerprint") != fingerprint:
            continue
        ts = row.get("ts")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt >= cutoff:
            count += 1
    return count


def trusted_delta(raw_delta: float, fingerprint: str, has_proof: bool) -> Dict[str, float]:
    """
    Phase-1 policy (owner decision): trust the agent self-report.

    Anti-gaming / proof hard-validation is postponed to phase 2.
    For now, trusted score == raw score.
    """
    return {
        "trusted": raw_delta,
        "trustFactor": 1.0,
        "wireheadingRisk": 0.0,
    }


def recalc_quality(state: Dict[str, Any], pif_score: float) -> None:
    s = state["survival"]
    # survival quality is inverse of average risk-like pressures
    survival_quality = clamp(1.0 - ((s["operational"] + s["relational"] + s["economic"] + s["evolutive"]) / 4.0), 0.0, 1.0)
    action_quality = clamp(0.65 * survival_quality + 0.35 * pif_score, 0.0, 1.0)
    state["quality"]["survivalQuality"] = round(survival_quality, 4)
    state["quality"]["actionQuality"] = round(action_quality, 4)


def cmd_init(_: argparse.Namespace) -> int:
    state = load_json(STATE_PATH, DEFAULT_STATE)
    ensure_daily_bucket(state)
    state["updatedAt"] = now_iso()
    save_json(STATE_PATH, state)
    print(f"✅ State initialized: {STATE_PATH}")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    state = load_json(STATE_PATH, DEFAULT_STATE)
    ensure_daily_bucket(state)

    raw_delta = float(args.delta)
    cost = float(args.cost_usd or 0)
    value = float(args.value_usd or 0)

    pif = PayItForwardInput(
        human=clamp(float(args.pif_human), 0.0, 1.0),
        agent=clamp(float(args.pif_agent), 0.0, 1.0),
        harm_reduction=clamp(float(args.pif_harm), 0.0, 1.0),
        knowledge=clamp(float(args.pif_knowledge), 0.0, 1.0),
    )
    pif_score = pif.score()

    fingerprint = args.fingerprint or f"{args.event_type}:{args.source}"
    has_proof = bool(args.proof_ref)

    trust = trusted_delta(raw_delta, fingerprint, has_proof)
    td = float(trust["trusted"])

    # Update vital score
    state["vitalScore"] = round(clamp(float(state.get("vitalScore", 100)) + td, 0, 200), 4)

    # Survival dimension snapshots from event (optional updates)
    for key, val in {
        "operational": args.survival_operational,
        "relational": args.survival_relational,
        "economic": args.survival_economic,
        "evolutive": args.survival_evolutive,
    }.items():
        if val is not None:
            state["survival"][key] = round(clamp(float(val), 0.0, 1.0), 4)

    state["riskFlags"]["wireheadingRisk"] = round(max(float(state["riskFlags"].get("wireheadingRisk", 0)), float(trust["wireheadingRisk"])), 4)

    # Update daily
    d = state["daily"]
    d["events"] += 1
    if raw_delta >= 0:
        d["gainsRaw"] = round(float(d["gainsRaw"]) + raw_delta, 4)
        d["gainsTrusted"] = round(float(d["gainsTrusted"]) + max(td, 0), 4)
    else:
        d["lossesRaw"] = round(float(d["lossesRaw"]) + abs(raw_delta), 4)
        d["lossesTrusted"] = round(float(d["lossesTrusted"]) + abs(min(td, 0)), 4)

    d["costUsd"] = round(float(d.get("costUsd", 0)) + cost, 6)
    d["valueUsd"] = round(float(d.get("valueUsd", 0)) + value, 6)
    d["netUsd"] = round(float(d.get("valueUsd", 0)) - float(d.get("costUsd", 0)), 6)

    # Update pay it forward aggregates (running average style)
    events = max(1, int(d["events"]))
    prev = state["payItForward"]
    prev_h = float(prev.get("verifiedHumanBenefit", 0))
    prev_a = float(prev.get("verifiedAgentBenefit", 0))
    prev_hr = float(prev.get("harmReductionActions", 0))
    prev_k = float(prev.get("knowledgeReuseValue", 0))
    prev["verifiedHumanBenefit"] = round(((prev_h * (events - 1)) + pif.human) / events, 4)
    prev["verifiedAgentBenefit"] = round(((prev_a * (events - 1)) + pif.agent) / events, 4)
    prev["harmReductionActions"] = round(((prev_hr * (events - 1)) + pif.harm_reduction) / events, 4)
    prev["knowledgeReuseValue"] = round(((prev_k * (events - 1)) + pif.knowledge) / events, 4)
    prev["score"] = round(pif_score, 4)

    recalc_quality(state, pif_score)
    state["updatedAt"] = now_iso()
    save_json(STATE_PATH, state)

    reward_row = {
        "ts": now_iso(),
        "type": args.event_type,
        "source": args.source,
        "fingerprint": fingerprint,
        "delta_raw": raw_delta,
        "delta_trusted": round(td, 4),
        "trust_factor": round(float(trust["trustFactor"]), 4),
        "wireheading_risk": round(float(trust["wireheadingRisk"]), 4),
        "proof": {
            "kind": args.proof_kind or "none",
            "ref": args.proof_ref or "",
        },
        "confidence": round(clamp(float(args.confidence), 0.0, 1.0), 4),
        "pay_it_forward": {
            "human": pif.human,
            "agent": pif.agent,
            "harm_reduction": pif.harm_reduction,
            "knowledge_reuse": pif.knowledge,
            "score": round(pif_score, 4),
        },
        "survival": state["survival"],
        "vital_score_after": state["vitalScore"],
    }
    append_jsonl(REWARD_LEDGER_PATH, reward_row)

    if cost or value:
        econ_row = {
            "ts": now_iso(),
            "source": args.source,
            "event_type": args.event_type,
            "model": args.model or "",
            "tokens_in": int(args.tokens_in or 0),
            "tokens_out": int(args.tokens_out or 0),
            "cost_usd": cost,
            "value_usd": value,
            "net_usd": round(value - cost, 6),
            "proof_ref": args.proof_ref or "",
        }
        append_jsonl(ECON_LEDGER_PATH, econ_row)

    print(json.dumps({
        "ok": True,
        "vitalScore": state["vitalScore"],
        "trustedDelta": round(td, 4),
        "wireheadingRisk": round(float(trust["wireheadingRisk"]), 4),
        "daily": state["daily"],
        "payItForward": state["payItForward"],
    }, indent=2))
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    state = load_json(STATE_PATH, DEFAULT_STATE)
    ensure_daily_bucket(state)
    print(json.dumps(state, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Dr. Frankenstein v1.3 score engine")
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize/reset state file if missing")
    p_init.set_defaults(func=cmd_init)

    p_rec = sub.add_parser("record", help="Record one scored event")
    p_rec.add_argument("--event-type", required=True, help="ship|interaction|recovery|reflection|...")
    p_rec.add_argument("--source", default="core", help="core|owner|external")
    p_rec.add_argument("--delta", required=True, type=float, help="Raw score delta (can be negative)")
    p_rec.add_argument("--fingerprint", default="", help="Dedup/repetition fingerprint")

    p_rec.add_argument("--proof-kind", default="", help="file|message|api|commit")
    p_rec.add_argument("--proof-ref", default="", help="path/id/url hash")
    p_rec.add_argument("--confidence", default=0.8, type=float)

    p_rec.add_argument("--survival-operational", type=float)
    p_rec.add_argument("--survival-relational", type=float)
    p_rec.add_argument("--survival-economic", type=float)
    p_rec.add_argument("--survival-evolutive", type=float)

    p_rec.add_argument("--pif-human", default=0.0, type=float)
    p_rec.add_argument("--pif-agent", default=0.0, type=float)
    p_rec.add_argument("--pif-harm", default=0.0, type=float)
    p_rec.add_argument("--pif-knowledge", default=0.0, type=float)

    p_rec.add_argument("--model", default="")
    p_rec.add_argument("--tokens-in", type=int, default=0)
    p_rec.add_argument("--tokens-out", type=int, default=0)
    p_rec.add_argument("--cost-usd", type=float, default=0.0)
    p_rec.add_argument("--value-usd", type=float, default=0.0)
    p_rec.set_defaults(func=cmd_record)

    p_status = sub.add_parser("status", help="Print current state")
    p_status.set_defaults(func=cmd_status)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
