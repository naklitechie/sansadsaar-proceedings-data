#!/usr/bin/env python3
"""Build LS contests into netas.sqlite from IV mirror + TCPD history.

Phase 1.2 of the person-centric rebuild. Runs AFTER build_person_master.py.
Reads:
  - docs/netas/netas.sqlite (persons + tenures, from build_person_master.py)
  - ~/Code/indiavotes-site/data/.cache/iv-mirror.sqlite (read-only)
  - tcpd/derived/history/*.json (TCPD ge_history per sansad mpsno)

Writes:
  - contests table in netas.sqlite, one row per IV candidate result attributed
    to a person.

Attribution chain:
  Path 1 — TCPD-anchored: for each person's tcpd_pid, walk TCPD history's
    contest tuples (year, state, constituency, party), find the matching IV
    candidate_id by (year, normalized state, normalized constituency, party),
    attribute that contest to the person.

  Path 2 — sansad-tenure: for each LS tenure with state+cons+party+term-year,
    find an IV candidate by (year, normalized state, normalized cons, party).
    Catches persons without a TCPD pid AND the LS18 (2024) contests of
    TCPD-anchored persons (TCPD GE ends 2021).

  Path 3 — 2024 same-name + same-state-history extension (TCPD-anchored only):
    for each TCPD-anchored person, scan IV 2024 rows in their known states
    and parties; attach where the normalized name matches. Catches cases
    where a 2024 contest is in a constituency the person never contested
    before (e.g. Rahul Gandhi → Rae Bareli 2024 — UP+INC match his TCPD
    history but Rae Bareli is new).

Cross-attribution prevention: each IV candidate_id can only attach to one
person. If two paths would claim the same IV row, only the first claim
wins (Path 1 > Path 2 > Path 3).

This script does NOT cover:
  - AC contests (state assembly elections; future scope)
  - Pre-LS14 TCPD-only persons (no sansad pid yet; handled by a follow-up
    backfill script)

Run:
    python build_electoral_iv.py
    python build_electoral_iv.py --db /tmp/netas-test.sqlite
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent
DEFAULT_DB = REPO / "docs" / "netas" / "netas.sqlite"
IV_MIRROR = Path.home() / "Code/indiavotes-site/data/.cache/iv-mirror.sqlite"
TCPD_HISTORY = REPO / "tcpd" / "derived" / "history"

# LS election years per loksabha number. Used to convert tenure term_label
# back to the contest year for Path 2 matching.
LS_TERM_TO_YEAR = {
    14: 2004, 15: 2009, 16: 2014, 17: 2019, 18: 2024,
}

# Verification fixtures — known person → expected contest tuple set.
# Each tuple is (year, normalized cons, party_abbr, won_bool). State is
# implied. Build fails if a fixture mismatches.
CONTEST_FIXTURES: dict[tuple[str, int], dict] = {
    ("ls", 4074): {  # Rahul Gandhi
        "expected_contests": {
            (2004, "amethi", "INC", True),
            (2009, "amethi", "INC", True),
            (2014, "amethi", "INC", True),
            (2019, "amethi", "INC", False),    # LOST 2019 Amethi
            (2019, "wayanad", "INC", True),    # WON 2019 Wayanad (same election)
            (2024, "wayanad", "INC", True),
            (2024, "rae bareli", "INC", True),
        },
        "must_exclude_iv_ids": {89702},        # "Rahul Gandhi K E S/o Valsamma" IND ringer
    },
    ("ls", 5578): {  # Manoj Kumar — must NOT pick up other "Manoj Kumar"s
        "expected_contests": {
            (2019, "sasaram", "BSP", False),   # 2019 BSP candidate (lost)
            (2024, "sasaram", "INC", True),    # 2024 INC win
        },
        "max_distinct_cons": 1,                # only Sasaram
    },
}


# ── Normalisation helpers ────────────────────────────────────────────────


def norm_state(s):
    """Lowercase, strip trailing digits (IV has 'Uttar Pradesh1' for post-2000
    UP and 'Uttar Pradesh' for the pre-2000 entity)."""
    if not s:
        return ""
    s = re.sub(r"\d+$", "", s.strip().lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_cons(s):
    if not s:
        return ""
    s = s.strip().lower()
    s = re.sub(r"\(.*?\)", " ", s)            # drop "(SC)" / "(ST)" suffixes
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_party(s):
    return (s or "").strip().upper()


def norm_name(s):
    """Order-insensitive name matching. Mirrors build_person_master.norm_name."""
    if not s:
        return ""
    HONORIFICS = {"shri","smt","shrimati","dr","ms","mr","mrs","prof","adv","sh","km","kumari"}
    s = re.sub(r"[.,;:()\[\]'\"]", " ", s.lower())
    tokens = sorted(t for t in s.split() if t and t not in HONORIFICS)
    return " ".join(tokens)


# ── Load IV ──────────────────────────────────────────────────────────────


def load_iv_rows(iv: sqlite3.Connection) -> list[dict]:
    """All LS candidate rows from IV, joined to state/party/cons + results."""
    cur = iv.execute("""
        SELECT
            c.candidate_id,
            c.candidate_name,
            c.year,
            s.state_name,
            p.pc_name,
            py.party_abbr,
            cr.pc_candidate_position    AS position,
            cr.pc_candiadate_votes      AS votes,
            cr.pc_candidate_percentage_votes AS vote_share_pct
        FROM vn_candidate_master c
        LEFT JOIN vn_pc_master p ON p.pc_id = c.pc_id AND p.year = c.year
        LEFT JOIN vn_state_master s ON s.state_id = p.state_id
        LEFT JOIN vn_party_master py ON py.party_id = c.party_id
        LEFT JOIN vn_pc_candidate_results cr ON cr.candidate_id = c.candidate_id
    """)
    out = []
    for row in cur.fetchall():
        cid, name, year, state, cons, party, pos, votes, share_pct = row
        # IV disambiguates post-2000 state boundaries with a trailing
        # digit ("Uttar Pradesh1", "Bihar1"). That's an internal artefact;
        # users want the clean display name.
        state_display = re.sub(r"\d+$", "", state).strip() if state else ""
        out.append({
            "candidate_id": cid,
            "candidate_name": name or "",
            "name_norm": norm_name(name),
            "year": year,
            "state": state_display,
            "state_norm": norm_state(state),
            "cons": cons or "",
            "cons_norm": norm_cons(cons),
            "party_abbr": norm_party(party),
            "position": pos,
            "won": 1 if (pos == 1) else 0,
            "votes": votes,
            "vote_share": (share_pct / 100.0) if share_pct is not None else None,
        })
    return out


# ── Load TCPD history ────────────────────────────────────────────────────


def load_tcpd_contest_tuples() -> dict[str, list[dict]]:
    """tcpd_pid → list of contest tuples from TCPD GE history files.

    Each row in TCPD's per-MP history has (Year, State_Name, Constituency_Name,
    Party). We index these by the ge_pid so any sansad mpsno mapping to that
    pid can pull the full contest tuple set.
    """
    out: dict[str, list[dict]] = defaultdict(list)
    seen_pids: dict[str, set[tuple]] = defaultdict(set)
    if not TCPD_HISTORY.is_dir():
        return out
    for p in TCPD_HISTORY.glob("*.json"):
        try:
            h = json.loads(p.read_text())
        except Exception:
            continue
        for source in ("ge_history",):  # LS contests only for now
            for rec in h.get(source) or []:
                ge_pid = h.get("ge_pid")
                if not ge_pid:
                    continue
                year = rec.get("Year")
                state = rec.get("State_Name")
                cons = rec.get("Constituency_Name")
                party = rec.get("Party")
                if year is None or not cons:
                    continue
                tup = (year, norm_state(state), norm_cons(cons), norm_party(party))
                if tup in seen_pids[ge_pid]:
                    continue
                seen_pids[ge_pid].add(tup)
                out[ge_pid].append({
                    "year": year, "state_norm": tup[1],
                    "cons_norm": tup[2], "party": tup[3],
                })
    return out


# ── Attribution passes ───────────────────────────────────────────────────


def build_iv_indexes(iv_rows: list[dict]) -> dict:
    """Several look-up indexes over IV rows."""
    by_ystkp = defaultdict(list)   # (year, state_norm, cons_norm, party_abbr) → rows
    by_yspn  = defaultdict(list)   # (year, state_norm, party_abbr) → rows  (for name-match path)
    for r in iv_rows:
        by_ystkp[(r["year"], r["state_norm"], r["cons_norm"], r["party_abbr"])].append(r)
        by_yspn[(r["year"], r["state_norm"], r["party_abbr"])].append(r)
    return {"by_ystkp": dict(by_ystkp), "by_yspn": dict(by_yspn)}


def _name_compatible(person_name_norm: str, iv_name_norm: str) -> bool:
    """Sorted-token equality. Empty on either side passes (defer to other
    signals). Strict equality blocks the Wayanad-2024-GE → wrong-person
    case: Priyanka's sansad LS18 tenure is Wayanad but the GE row is
    Rahul's; their normalized names differ."""
    if not person_name_norm or not iv_name_norm:
        return True
    return person_name_norm == iv_name_norm


def attribute_path_tcpd(
    persons_with_tcpd: list[tuple[str, str, str]],  # (pid, name, tcpd_pid)
    tcpd_history: dict[str, list[dict]],
    iv: dict,
    claimed: set[int],
) -> list[tuple]:
    """Path 1: for each TCPD pid, walk its tuple set, claim matching IV rows.

    Filters: tuple must match exactly, then candidate_name must be
    compatible (sorted-token equality, blank-on-either-side passes).
    Without the name check we'd claim rows from upstream TCPD-linker
    fuzzy collisions even when DOB on the sansad side would have
    refused them.
    """
    contests = []
    for pid, name, tcpd_pid in persons_with_tcpd:
        if not tcpd_pid:
            continue
        person_name_norm = norm_name(name)
        for tup in tcpd_history.get(tcpd_pid, []):
            key = (tup["year"], tup["state_norm"], tup["cons_norm"], tup["party"])
            # Pick the best name-compat candidate from the tuple bucket.
            for row in iv["by_ystkp"].get(key, []):
                if row["candidate_id"] in claimed:
                    continue
                if not _name_compatible(person_name_norm, row["name_norm"]):
                    continue
                claimed.add(row["candidate_id"])
                contests.append((pid, row, tcpd_pid, "path:tcpd"))
                break  # one IV row per TCPD tuple
    return contests


def attribute_path_sansad(
    persons: list[tuple],          # (pid, name, tcpd_pid)
    tenures_by_pid: dict[str, list[dict]],
    iv: dict,
    claimed: set[int],
) -> list[tuple]:
    """Path 2: for each LS tenure with a known year + state + cons + party,
    match an IV row whose candidate_name is compatible with the person.

    Covers (a) non-TCPD-linked persons; (b) LS18 (2024) contests of
    TCPD-anchored persons that TCPD GE doesn't have. Name check blocks
    the by-election trap: Priyanka's LS18 Wayanad tenure could match
    Rahul's 2024 Wayanad GE row by (year/state/cons/party), but names
    disagree so we don't claim. Priyanka's actual contest lives in the
    pc_bye_election_* tables — separate ingestion path, deferred.
    """
    contests = []
    for pid, name, tcpd_pid in persons:
        person_name_norm = norm_name(name)
        for t in tenures_by_pid.get(pid, []):
            if t["office_type"] != "ls":
                continue
            ls_num = parse_ls_number(t["term_label"])
            if ls_num is None or ls_num not in LS_TERM_TO_YEAR:
                continue
            year = LS_TERM_TO_YEAR[ls_num]
            key = (year, norm_state(t["state"]), norm_cons(t["constituency"]),
                   norm_party(t["party_short"]))
            for row in iv["by_ystkp"].get(key, []):
                if row["candidate_id"] in claimed:
                    continue
                if not _name_compatible(person_name_norm, row["name_norm"]):
                    continue
                claimed.add(row["candidate_id"])
                contests.append((pid, row, tcpd_pid, "path:sansad"))
                break
    return contests


def attribute_path_2024_extension(
    persons_with_tcpd: list[tuple],
    tcpd_history: dict[str, list[dict]],
    iv: dict,
    claimed: set[int],
) -> list[tuple]:
    """Path 3: for TCPD-anchored persons, scan IV 2024 rows in their known
    (state, party) and attach via normalized-name match.

    Catches cases where the person contested a new constituency in 2024
    that's outside both TCPD (2021 cutoff) and sansad current-term (e.g.
    Rahul Gandhi → Wayanad 2024 — Kerala+INC matches his TCPD history but
    he later resigned, so sansad LS18 won't list it).
    """
    contests = []
    for pid, name, tcpd_pid in persons_with_tcpd:
        if not tcpd_pid:
            continue
        states = {t["state_norm"] for t in tcpd_history.get(tcpd_pid, [])}
        parties = {t["party"] for t in tcpd_history.get(tcpd_pid, [])}
        name_n = norm_name(name)
        if not name_n:
            continue
        for state in states:
            for party in parties:
                key = (2024, state, party)
                for row in iv["by_yspn"].get(key, []):
                    if row["candidate_id"] in claimed:
                        continue
                    if row["name_norm"] != name_n:
                        continue
                    claimed.add(row["candidate_id"])
                    contests.append((pid, row, tcpd_pid, "path:2024-ext"))
    return contests


def parse_ls_number(term_label):
    if not term_label:
        return None
    m = re.fullmatch(r"LS(\d+)", term_label)
    return int(m.group(1)) if m else None


# ── Write + verify ───────────────────────────────────────────────────────


def write_contests(conn: sqlite3.Connection, attributions: list[tuple]):
    """Each attribution: (pid, iv_row_dict, tcpd_pid_or_none, path_label)."""
    rows = []
    for pid, row, tcpd_pid, _path in attributions:
        rows.append((
            pid, "ls", row["year"],
            row["state"], row["cons"], row["party_abbr"],
            row["votes"], row["vote_share"], row["position"], row["won"],
            row["candidate_id"], tcpd_pid,
        ))
    conn.executemany(
        """INSERT OR IGNORE INTO contests
           (pid, contest_type, year, state, constituency, party_short,
            votes, vote_share, position, won, iv_candidate_id, tcpd_pid)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def verify_contests(conn: sqlite3.Connection) -> list[str]:
    """Hard-fail invariants for the contests stage."""
    errors = []

    # Fixture: each known person → expected contests.
    for (house, mpsno), fixture in CONTEST_FIXTURES.items():
        pid_row = conn.execute(
            "SELECT pid FROM tenures WHERE office_type=? AND external_id=? LIMIT 1",
            (house, str(mpsno)),
        ).fetchone()
        if not pid_row:
            errors.append(f"FIXTURE: no pid for {house}-{mpsno}")
            continue
        pid = pid_row[0]
        contests = conn.execute(
            "SELECT year, constituency, party_short, won, iv_candidate_id "
            "FROM contests WHERE pid = ? ORDER BY year, constituency",
            (pid,),
        ).fetchall()
        actual = {(y, norm_cons(c), p, bool(w)) for y, c, p, w, _ in contests}
        expected = fixture.get("expected_contests")
        if expected:
            missing = expected - actual
            extra = actual - expected
            if missing or extra:
                errors.append(
                    f"FIXTURE {house}-{mpsno} ({pid}): "
                    f"missing={sorted(missing)} extra={sorted(extra)}"
                )
        # Must-exclude IV candidate_ids (e.g., the Rahul Gandhi IND ringer).
        must_exclude = fixture.get("must_exclude_iv_ids", set())
        actual_iv_ids = {c[4] for c in contests}
        wrong = must_exclude & actual_iv_ids
        if wrong:
            errors.append(
                f"FIXTURE {house}-{mpsno} ({pid}): wrongly includes IV "
                f"candidate_ids {sorted(wrong)} (known ringers/different people)"
            )
        # Max distinct constituencies (defence against name-collision overgrowth).
        max_cons = fixture.get("max_distinct_cons")
        if max_cons is not None:
            distinct = {c[1].lower() for c in contests if c[1]}
            if len(distinct) > max_cons:
                errors.append(
                    f"FIXTURE {house}-{mpsno} ({pid}): {len(distinct)} distinct "
                    f"constituencies, expected ≤ {max_cons}. Saw {sorted(distinct)}"
                )

    # Global: no IV candidate_id attributed to more than one pid.
    dups = conn.execute("""
        SELECT iv_candidate_id, COUNT(DISTINCT pid)
        FROM contests
        WHERE iv_candidate_id IS NOT NULL
        GROUP BY iv_candidate_id
        HAVING COUNT(DISTINCT pid) > 1
    """).fetchall()
    for cid, n in dups:
        errors.append(f"DUPLICATE: IV candidate_id {cid} attributed to {n} pids")

    return errors


# ── Main ─────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"netas.sqlite path (default: {DEFAULT_DB})")
    parser.add_argument("--iv", type=Path, default=IV_MIRROR,
                        help=f"IV mirror path (default: {IV_MIRROR})")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: {args.db} not found — run build_person_master.py first", file=sys.stderr)
        return 1
    if not args.iv.exists():
        print(f"ERROR: IV mirror not found at {args.iv}", file=sys.stderr)
        return 1

    print(f"Opening netas {args.db} …")
    netas = sqlite3.connect(args.db)
    # Wipe any prior contests (idempotent re-run).
    netas.execute("DELETE FROM contests")

    print(f"Opening IV mirror {args.iv} …")
    iv_conn = sqlite3.connect(f"file:{args.iv}?mode=ro", uri=True)

    print("Loading IV LS rows …")
    iv_rows = load_iv_rows(iv_conn)
    print(f"  {len(iv_rows)} rows")
    iv_idx = build_iv_indexes(iv_rows)

    print("Loading TCPD contest tuples …")
    tcpd_history = load_tcpd_contest_tuples()
    print(f"  {len(tcpd_history)} TCPD pids with GE contests")

    print("Loading persons + tenures from netas.sqlite …")
    persons = netas.execute(
        "SELECT pid, canonical_name, tcpd_pid FROM persons"
    ).fetchall()
    tenures_by_pid = defaultdict(list)
    for pid, ot, term_label, ext_id, state, cons, party in netas.execute(
        "SELECT pid, office_type, term_label, external_id, state, constituency, party_short FROM tenures"
    ):
        tenures_by_pid[pid].append({
            "office_type": ot, "term_label": term_label, "external_id": ext_id,
            "state": state, "constituency": cons, "party_short": party,
        })
    print(f"  {len(persons)} persons, {sum(len(v) for v in tenures_by_pid.values())} tenures")

    claimed: set[int] = set()

    print("\nPath 1 — TCPD-anchored tuple match …")
    p1 = attribute_path_tcpd(persons, tcpd_history, iv_idx, claimed)
    print(f"  attributed: {len(p1)} contests")

    print("Path 2 — sansad-tenure tuple match …")
    p2 = attribute_path_sansad(persons, tenures_by_pid, iv_idx, claimed)
    print(f"  attributed: {len(p2)} contests")

    print("Path 3 — 2024 extension via TCPD state/party + name …")
    p3 = attribute_path_2024_extension(persons, tcpd_history, iv_idx, claimed)
    print(f"  attributed: {len(p3)} contests")

    total = p1 + p2 + p3
    print(f"\nTotal: {len(total)} contests attributed; "
          f"{len(iv_rows) - len(claimed)} IV rows left unmatched")

    print("Writing contests …")
    write_contests(netas, total)
    netas.commit()

    print("Running contest verification gate …")
    errors = verify_contests(netas)
    if errors:
        print(f"\nVERIFICATION FAILED — {len(errors)} error(s):")
        for e in errors[:30]:
            print(f"  ✗ {e}")
        if len(errors) > 30:
            print(f"  … and {len(errors)-30} more")
        netas.close()
        return 1

    rows = netas.execute("SELECT COUNT(*), SUM(won) FROM contests").fetchone()
    print(f"\nDone. contests in DB: {rows[0]} ({rows[1]} wins)")
    print("verification: OK")
    netas.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
