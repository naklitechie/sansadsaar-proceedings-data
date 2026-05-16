#!/usr/bin/env python3
"""Build persons + tenures into netas.sqlite (Phase 1 of person-centric rebuild).

This is the first stage of the new pipeline. It reads:

  - scripts/recon-netas-out/sansad_terms.json  (per-(mpsno, loksabha) tenures)
  - scripts/recon-netas-out/sansad_master.json (per-mpsno canonical info)
  - tcpd/derived/history/<house>-<mpsno>.json  (TCPD pid links, in-repo snapshot)

Applies the identity-resolution chain (sansad DOB primary, TCPD pid as the
cross-mpsno spline, fallback for the residual), mints stable `n-<int>` pids
via the checked-in person_pid_assignments.json, applies schema.sql to a
fresh netas.sqlite, and populates `persons` and `tenures`.

Not in this script (separate follow-ups):
  - TCPD-RSD historical RS rows         → build_person_master.py --rs-tcpd
  - Pre-LS14 TCPD GE winners backfill   → same script, later flag
  - Contests                            → build_electoral_iv.py
  - Corpora (speeches/questions/bills)  → build_netas.py rewrite

Run:
    python build_person_master.py
    python build_person_master.py --db /tmp/netas-test.sqlite

Identity chain summary (per plan 003):

    sansad DOB+name (66.6% coverage) ──┐
                                       ├─→ pid (n-<int>, stable across runs)
    TCPD pid (41.5%, spline)           ┘
                                       │
    fallback: one person per mpsno ────┘
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
SCHEMA = REPO / "schema.sql"
SANSAD_MASTER = REPO / "scripts" / "recon-netas-out" / "sansad_master.json"
SANSAD_TERMS = REPO / "scripts" / "recon-netas-out" / "sansad_terms.json"
TCPD_HISTORY = REPO / "tcpd" / "derived" / "history"
PID_ASSIGNMENTS = REPO / "person_pid_assignments.json"
DEFAULT_DB = REPO / "docs" / "netas" / "netas.sqlite"


# ── Normalisation ────────────────────────────────────────────────────────


HONORIFIC_TOKENS = {
    "shri", "smt", "shrimati", "dr", "ms", "mr", "mrs", "prof", "adv",
    "sh", "km", "kumari",
}


def norm_name(s: str | None) -> str:
    """Order-insensitive name canonicalisation for *matching*.

    Sansad stores names in three flavours that all refer to the same person:
      "Shri Lalu Prasad"          (full_name_firstlast)
      "Lalu Prasad, Shri"         (sometimes full_name_lastfirst on RS)
      "Joshi, Dr. Murli Manohar"  (lastfirst with trailing honorific in middle)

    After lowercasing, stripping punctuation, dropping honorific tokens, and
    sorting the remaining tokens, all three forms collapse to the same
    string. Sorted-token comparison is intentional — for sansad data we
    treat name as an unordered bag because the upstream representation
    varies per record. DOB then disambiguates same-name people.
    """
    if not s:
        return ""
    s = re.sub(r"[.,;:()\[\]'\"]", " ", s.lower())
    tokens = sorted(t for t in s.split() if t and t not in HONORIFIC_TOKENS)
    return " ".join(tokens)


def norm_dob(dob: str | None) -> str | None:
    """sansad's dd/mm/yyyy → ISO yyyy-mm-dd. None if unparseable."""
    if not dob or not isinstance(dob, str):
        return None
    s = dob.strip()
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo}-{d}"
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s
    return None


def slug_of(name: str) -> str:
    """URL-safe slug of a canonical name. Strips honorifics, lowercases,
    keeps alphanumeric + hyphens."""
    n = norm_name(name)
    n = re.sub(r"[^a-z0-9]+", "-", n).strip("-")
    return n or "anon"


def sha256_short(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# ── Loaders ──────────────────────────────────────────────────────────────


def load_sansad_terms() -> dict[tuple[str, int], list[dict]]:
    """Return {(house, mpsno): [unique tenure records by loksabha]}.

    sansad_terms.json sometimes contains near-duplicates of the same
    (mpsno, loksabha) — we dedupe and keep one per (house, mpsno, loksabha).
    """
    raw = json.loads(SANSAD_TERMS.read_text())
    by_key: dict[tuple[str, int], dict[int | None, dict]] = defaultdict(dict)
    for rec in raw:
        h = rec.get("house")
        m = rec.get("mpsno")
        ls = rec.get("loksabha")  # None for RS records
        if h is None or m is None:
            continue
        key = (h, int(m))
        # Use the freshest of any duplicates: prefer Sitting > others.
        existing = by_key[key].get(ls)
        if existing is None or _term_priority(rec) > _term_priority(existing):
            by_key[key][ls] = rec
    return {k: list(v.values()) for k, v in by_key.items()}


def _term_priority(rec: dict) -> int:
    s = (rec.get("status") or "").lower()
    if s == "sitting":
        return 3
    if s in {"former", "retirement"}:
        return 2
    if rec.get("sitting") is True:
        return 1
    return 0


def load_sansad_master() -> dict[tuple[str, int], dict]:
    raw = json.loads(SANSAD_MASTER.read_text())
    out: dict[tuple[str, int], dict] = {}
    for rec in raw:
        h = rec.get("house")
        m = rec.get("mpsno")
        if h is None or m is None:
            continue
        out[(h, int(m))] = rec
    return out


def build_tcpd_link_index() -> dict[tuple[str, int], str]:
    """Walk tcpd/derived/history/*.json and extract per-mpsno TCPD pids.

    Each history file is {mpsno, house, ge_pid, ae_pid, ge_history, ae_history}.
    We prefer ge_pid (LS-relevant); fall back to ae_pid for AC-only matches.
    """
    out: dict[tuple[str, int], str] = {}
    if not TCPD_HISTORY.is_dir():
        return out
    for p in TCPD_HISTORY.glob("*.json"):
        try:
            h = json.loads(p.read_text())
        except Exception:
            continue
        m = h.get("mpsno")
        house = h.get("house")
        if m is None or house is None:
            continue
        pid = h.get("ge_pid") or h.get("ae_pid")
        if pid:
            out[(house, int(m))] = pid
    return out


# ── Identity resolution ──────────────────────────────────────────────────


class UnionFind:
    """Tiny union-find for grouping mpsnos that resolve to the same person."""

    def __init__(self) -> None:
        self.parent: dict[tuple[str, int], tuple[str, int]] = {}

    def find(self, x: tuple[str, int]) -> tuple[str, int]:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: tuple[str, int], b: tuple[str, int]) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def resolve_persons(
    terms: dict[tuple[str, int], list[dict]],
    master: dict[tuple[str, int], dict],
    tcpd: dict[tuple[str, int], str],
) -> list[dict]:
    """Group mpsnos into persons. Returns list of person dicts."""
    uf = UnionFind()
    for key in terms:
        uf.find(key)

    # Pre-compute (norm_name, dob) for each mpsno. A single mpsno can have
    # multiple tenure records with inconsistent DOB presence; scan all
    # records for any non-null DOB before discarding.
    name_dob: dict[tuple[str, int], tuple[str, str | None]] = {}
    for key, recs in terms.items():
        nname = ""
        dob: str | None = None
        for rec in recs:
            if not nname:
                raw = rec.get("full_name_firstlast") or rec.get("full_name_lastfirst") or ""
                nname = norm_name(raw)
            if dob is None:
                dob = norm_dob(rec.get("dob"))
            if nname and dob:
                break
        name_dob[key] = (nname, dob)

    # Merge by sansad (norm_name, dob) — primary spline.
    by_dob: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    for key, (nname, dob) in name_dob.items():
        if dob and nname:
            by_dob[(nname, dob)].append(key)
    for group in by_dob.values():
        for other in group[1:]:
            uf.union(group[0], other)

    # Merge by TCPD pid — secondary spline. The TCPD linker is fuzzy and
    # frequently maps different sansad MPs to the same pid (verified
    # collisions: GEUP48277 = Karan Bhushan Singh + Brijbhushan Sharan
    # Singh; GEBR2137 = two unrelated "Veena Devi"s with different DOBs).
    # Gate the merge on name+DOB compatibility:
    #   - if both have DOB and DOBs differ → REFUSE
    #   - if both have norm_name and norm_names differ → REFUSE
    # When in doubt, don't merge — false merges are worse than missed ones.
    by_tcpd: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for key, pid in tcpd.items():
        if key in terms:
            by_tcpd[pid].append(key)
    refused = 0
    for pid, group in by_tcpd.items():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            an, ad = name_dob[a]
            for b in group[i + 1:]:
                bn, bd = name_dob[b]
                if an and bn and an != bn:
                    refused += 1
                    continue
                if ad and bd and ad != bd:
                    refused += 1
                    continue
                uf.union(a, b)
    if refused:
        print(f"  TCPD-pid merge refused for {refused} pair(s) "
              f"with conflicting name/DOB (probable upstream linker fuzz)")

    # Group by representative.
    groups: dict[tuple[str, int], list[tuple[str, int]]] = defaultdict(list)
    for key in terms:
        groups[uf.find(key)].append(key)

    persons = []
    for rep, members in groups.items():
        person = _build_person(rep, members, terms, master, tcpd)
        persons.append(person)
    return persons


def _build_person(
    rep: tuple[str, int],
    members: list[tuple[str, int]],
    terms: dict[tuple[str, int], list[dict]],
    master: dict[tuple[str, int], dict],
    tcpd: dict[tuple[str, int], str],
) -> dict:
    # Canonical info: prefer the most recent tenure across all members.
    tenure_records: list[tuple[tuple[str, int], dict]] = []
    for key in members:
        for rec in terms[key]:
            tenure_records.append((key, rec))
    # Sort by (loksabha desc, sitting desc) to pick the canonical record.
    tenure_records.sort(
        key=lambda kr: (kr[1].get("loksabha") or 0, _term_priority(kr[1])),
        reverse=True,
    )
    canonical_key, canonical_rec = tenure_records[0]

    canonical_name = (
        canonical_rec.get("full_name_firstlast")
        or canonical_rec.get("full_name_lastfirst")
        or "Unknown"
    )
    canonical_name = re.sub(r"\s+", " ", canonical_name).strip()
    dob = norm_dob(canonical_rec.get("dob"))
    for _, rec in tenure_records:
        # Prefer any non-null DOB found across the group.
        if dob is None:
            dob = norm_dob(rec.get("dob"))

    gender = canonical_rec.get("gender")
    if isinstance(gender, str):
        g = gender.strip().upper()
        gender = {"MALE": "M", "FEMALE": "F"}.get(g, g) if g else None

    image_url = master.get(canonical_key, {}).get("image_url")
    tcpd_pids = sorted({tcpd[k] for k in members if k in tcpd})
    tcpd_pid = tcpd_pids[0] if tcpd_pids else None

    has_dob = dob is not None
    has_tcpd = tcpd_pid is not None
    if has_dob and has_tcpd:
        identity_source = "both"
        anchor = "sansad_dob"
        confidence = 1.0
    elif has_dob:
        identity_source = "dob_only"
        anchor = "sansad_dob"
        confidence = 0.9
    elif has_tcpd:
        identity_source = "tcpd_only"
        anchor = "tcpd_pid"
        confidence = 0.8
    else:
        identity_source = "fallback"
        anchor = "fallback"
        confidence = 0.5

    return {
        "merge_key": _merge_key(members),
        "canonical_name": canonical_name,
        "slug": slug_of(canonical_name),
        "dob": dob,
        "gender": gender,
        "image_url": image_url,
        "identity_source": identity_source,
        "tcpd_pid": tcpd_pid,
        "sansad_dob_hash": sha256_short(f"{norm_name(canonical_name)}|{dob}") if has_dob else None,
        "anchor_signal": anchor,
        "confidence": confidence,
        "members": sorted(members),
        "tenure_records": [
            {
                "house": k[0],
                "mpsno": k[1],
                "record": rec,
            }
            for k, rec in tenure_records
        ],
    }


def _merge_key(members: list[tuple[str, int]]) -> str:
    parts = [f"{h}-{m}" for h, m in sorted(members)]
    return "sansad:" + ",".join(parts)


# ── Pid assignment (persisted) ───────────────────────────────────────────


def load_pid_assignments() -> dict:
    if not PID_ASSIGNMENTS.exists():
        return {"next_id": 1, "by_merge_key": {}, "by_pid": {}}
    return json.loads(PID_ASSIGNMENTS.read_text())


def save_pid_assignments(assignments: dict) -> None:
    PID_ASSIGNMENTS.write_text(json.dumps(assignments, indent=2, ensure_ascii=False) + "\n")


def assign_pids(persons: list[dict], assignments: dict) -> None:
    """Mint stable pids. Mutates `persons` (adds .pid) and `assignments`.

    Append-only: an existing merge_key keeps its pid forever. New
    merge_keys mint the next available `n-<int>`. We also record
    provenance so future code can reason about merges/splits.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for person in persons:
        mk = person["merge_key"]
        if mk in assignments["by_merge_key"]:
            pid = assignments["by_merge_key"][mk]
        else:
            pid = f"n-{assignments['next_id']}"
            assignments["next_id"] += 1
            assignments["by_merge_key"][mk] = pid
            assignments["by_pid"][pid] = {
                "mint_date": today,
                "identity_source": person["identity_source"],
                "anchor_signal": person["anchor_signal"],
                "merge_key": mk,
                "deprecated_pids": [],
                "split_into": [],
            }
        person["pid"] = pid


# ── DB writers ───────────────────────────────────────────────────────────


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    schema = SCHEMA.read_text()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema)


def write_persons(conn: sqlite3.Connection, persons: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO persons
           (pid, canonical_name, slug, dob, gender, image_url,
            identity_source, tcpd_pid, sansad_dob_hash, anchor_signal, confidence)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                p["pid"],
                p["canonical_name"],
                p["slug"],
                p["dob"],
                p["gender"],
                p["image_url"],
                p["identity_source"],
                p["tcpd_pid"],
                p["sansad_dob_hash"],
                p["anchor_signal"],
                p["confidence"],
            )
            for p in persons
        ],
    )


def write_tenures(conn: sqlite3.Connection, persons: list[dict]) -> None:
    rows = []
    for p in persons:
        seen_keys = set()
        for tr in p["tenure_records"]:
            rec = tr["record"]
            office_type = tr["house"]
            jurisdiction = "IN"
            loksabha = rec.get("loksabha")
            term_label = f"LS{loksabha}" if office_type == "ls" and loksabha else (
                f"RS-{rec.get('term_start','?')}-{rec.get('term_end','?')}"
                if office_type == "rs"
                else None
            )
            external_id = str(tr["mpsno"])
            # Dedupe within a person — sansad sometimes lists same
            # (house, mpsno, loksabha) twice in the source.
            dedup_k = (office_type, external_id, term_label)
            if dedup_k in seen_keys:
                continue
            seen_keys.add(dedup_k)

            state = rec.get("state")
            constituency = rec.get("constituency")
            party_short = rec.get("party_short")
            sitting_int = 1 if (
                rec.get("sitting") is True or
                (rec.get("status") or "").lower() == "sitting"
            ) else 0
            status = rec.get("status")
            row_hash_input = f"{office_type}|{jurisdiction}|{external_id}|{term_label}|{p['pid']}"
            source_row_hash = sha256_short(row_hash_input)

            rows.append(
                (
                    p["pid"],
                    office_type,
                    jurisdiction,
                    term_label,
                    external_id,
                    rec.get("term_start"),
                    rec.get("term_end"),
                    sitting_int,
                    status,
                    state,
                    constituency,
                    party_short,
                    source_row_hash,
                )
            )
    conn.executemany(
        """INSERT INTO tenures
           (pid, office_type, jurisdiction, term_label, external_id,
            term_start, term_end, sitting, status, state, constituency,
            party_short, source_row_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def write_build_meta(conn: sqlite3.Connection, info: dict[str, str]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO build_meta (key, value) VALUES (?, ?)",
        list(info.items()),
    )


# ── Verification gate (per plan 003) ─────────────────────────────────────


def verify_sanity(db_path: Path, persons: list[dict]) -> list[str]:
    """Return a list of warnings about persons that fail sanity checks.

    Spec from plan 003 verification gate:
      - >2 distinct parties AND >5 distinct constituencies AND >20 contests
      - contests spanning >50 years
    Phase 1 only has tenures (no contests yet), so just check tenure-level
    pathologies for now: a person with >10 tenures, or >5 distinct
    parties, etc.
    """
    warnings: list[str] = []
    for p in persons:
        recs = p["tenure_records"]
        if len(recs) > 12:
            warnings.append(
                f"pid={p['pid']} ({p['canonical_name']}) has {len(recs)} tenure rows "
                f"— suspicious, please review"
            )
        parties = {r["record"].get("party_short") for r in recs if r["record"].get("party_short")}
        if len(parties) > 5:
            warnings.append(
                f"pid={p['pid']} ({p['canonical_name']}) has {len(parties)} distinct "
                f"parties across tenures — suspicious"
            )
    return warnings


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"output SQLite path (default: {DEFAULT_DB})")
    parser.add_argument("--dry-run", action="store_true",
                        help="don't write DB or pid assignments")
    args = parser.parse_args()

    print(f"Loading sansad_terms.json …")
    terms = load_sansad_terms()
    print(f"  {len(terms)} unique (house, mpsno) keys; "
          f"{sum(len(v) for v in terms.values())} tenure rows after dedup")

    print(f"Loading sansad_master.json …")
    master = load_sansad_master()
    print(f"  {len(master)} mpsno records")

    print(f"Indexing TCPD links from tcpd/derived/history/ …")
    tcpd = build_tcpd_link_index()
    print(f"  {len(tcpd)} sansad mpsnos linked to a TCPD pid")

    print("Resolving persons …")
    persons = resolve_persons(terms, master, tcpd)
    print(f"  {len(persons)} unique persons resolved from {len(terms)} mpsnos")

    # Bucket distribution
    from collections import Counter
    buckets = Counter(p["identity_source"] for p in persons)
    for b in ("both", "dob_only", "tcpd_only", "fallback"):
        n = buckets.get(b, 0)
        print(f"    {b:10}  {n:5}  ({n/len(persons)*100:5.1f}%)")

    multi = sum(1 for p in persons if len(p["members"]) > 1)
    print(f"  Cross-mpsno merges: {multi} persons span multiple mpsnos")

    print("Assigning stable pids …")
    assignments = load_pid_assignments()
    assign_pids(persons, assignments)
    if not args.dry_run:
        save_pid_assignments(assignments)
    print(f"  {len(assignments['by_pid'])} total pids in assignment file "
          f"(next: n-{assignments['next_id']})")

    warnings = verify_sanity(args.db, persons)
    if warnings:
        print(f"\nVERIFICATION GATE — {len(warnings)} warning(s):")
        for w in warnings[:20]:
            print(f"  ⚠ {w}")
        if len(warnings) > 20:
            print(f"  … and {len(warnings)-20} more")

    if args.dry_run:
        print("\n[dry-run] no DB write")
        return 0

    print(f"Writing {args.db} …")
    init_db(args.db)
    with sqlite3.connect(args.db) as conn:
        write_persons(conn, persons)
        write_tenures(conn, persons)
        write_build_meta(
            conn,
            {
                "schema_version": "0.1",
                "stage": "persons+tenures",
                "built_at": datetime.now(timezone.utc).isoformat(),
                "persons_count": str(len(persons)),
                "tenures_count": str(sum(len(p["tenure_records"]) for p in persons)),
                "sansad_terms_input": str(SANSAD_TERMS.relative_to(REPO)),
            },
        )

    print("\nDone.")
    print(f"  persons:  {len(persons)}")
    print(f"  tenures:  {sum(len(p['tenure_records']) for p in persons)}")
    print(f"  warnings: {len(warnings)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
