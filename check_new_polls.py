#!/usr/bin/env python3
"""
Detect presidential notices published by the Commission des sondages that this
repository does not track yet, and open one issue per notice.

A notice is considered new when its filename appears neither in polls.csv nor in
any existing `new-poll` issue, open or closed. Dedupe by filename rather than by
catalog size: closing an issue as out of scope is a decision, and re-opening it
on the next run would undo it.

`--since` bounds how far back to look. The catalog holds hundreds of notices the
repository never tracked (popularity barometers, one-off surveys), so without a
floor the first run would file an issue for every one of them. The floor lives in
.poll_detection_since and is never rewritten by this script: filename dedupe is
what keeps runs idempotent, so the floor only has to stay old enough not to hide
a fresh notice. Bump it by hand once a backlog has been triaged.

Usage:
    python check_new_polls.py                 # preview, no write
    python check_new_polls.py --create        # create the issues (needs GITHUB_TOKEN)
    python check_new_polls.py --since 2026-01-01 --limit 5
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Set
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CATALOG_URL = (
    "https://raw.githubusercontent.com/MieuxVoter/sondages-commission-index/refs/heads/main/notices_catalog.csv"
)
CATALOG_REPO_URL = "https://github.com/MieuxVoter/sondages-commission-index"
GITHUB_API = "https://api.github.com"

ROOT = Path(__file__).resolve().parent
POLLS_CSV = ROOT / "polls.csv"
SINCE_FILE = ROOT / ".poll_detection_since"

DEFAULT_LIMIT = 10
ISSUE_LABELS = ["new-poll", "automated"]
FILENAME_IN_ISSUE = re.compile(r"\*\*Fichier PDF à vérifier:\*\* `([^`]+)`")

# Most "Pres" notices are popularity barometers or one-off surveys rather than
# voting-intention polls. The guess only annotates the issue; nothing is filtered
# out on its strength alone.
OUT_OF_SCOPE_HINTS = (
    "popularite",
    "popularité",
    "barometre",
    "baromètre",
    "observatoire",
    "indices",
    "tableau de bord",
    "cote de confiance",
    "les francais et",
    "les français et",
)


def _clean(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #


def fetch_catalog(url: str = CATALOG_URL) -> List[dict]:
    """Return the presidential entries of the notices catalog."""
    with urlopen(url) as response:
        content = response.read().decode("utf-8")
    return parse_catalog(content)


def parse_catalog(content: str) -> List[dict]:
    reader = csv.DictReader(content.splitlines())
    return [row for row in reader if _clean(row, "categorie") == "Pres" and _clean(row, "filename")]


def notice_date(entry: dict) -> Optional[datetime.date]:
    """Return the PDF creation date, the closest thing the catalog has to a publication date."""
    for field in ("pdf creation-date", "http last-modified"):
        raw = _clean(entry, field)
        if not raw:
            continue
        try:
            return datetime.datetime.fromisoformat(raw).date()
        except ValueError:
            continue
    return None


def looks_out_of_scope(entry: dict) -> bool:
    haystack = f"{_clean(entry, 'name')} {_clean(entry, 'filename')}".lower()
    return any(hint in haystack for hint in OUT_OF_SCOPE_HINTS)


# --------------------------------------------------------------------------- #
# What we already know about
# --------------------------------------------------------------------------- #


def tracked_filenames(polls_csv: Path = POLLS_CSV) -> Set[str]:
    """Filenames of the notices already integrated in polls.csv."""
    if not polls_csv.exists():
        return set()
    with polls_csv.open("r", encoding="utf-8") as f:
        return {name for row in csv.DictReader(f) if (name := _clean(row, "filename"))}


def filenames_in_issues(issues: Iterable[dict]) -> Set[str]:
    """Filenames already reported, whatever the state of the issue."""
    found = set()
    for issue in issues:
        match = FILENAME_IN_ISSUE.search(issue.get("body") or "")
        if match:
            found.add(match.group(1).strip())
    return found


def read_since(default: datetime.date) -> datetime.date:
    if SINCE_FILE.exists():
        try:
            return datetime.date.fromisoformat(SINCE_FILE.read_text().strip())
        except ValueError:
            pass
    return default


def select_new_notices(catalog: List[dict], known: Set[str], since: Optional[datetime.date]) -> List[dict]:
    """Return catalog entries that are neither tracked nor already reported, oldest first."""
    selected = []
    for entry in catalog:
        if _clean(entry, "filename") in known:
            continue
        published = notice_date(entry)
        if since and (published is None or published < since):
            continue
        selected.append(entry)
    return sorted(selected, key=lambda e: (notice_date(e) or datetime.date.min, _clean(e, "filename")))


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #


def _github_request(url: str, token: str, method: str = "GET", payload: Optional[dict] = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    if data:
        request.add_header("Content-Type", "application/json")
    with urlopen(request) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def list_poll_issues(repo: str, token: str) -> List[dict]:
    """Every new-poll issue, open and closed alike."""
    issues: List[dict] = []
    page = 1
    while True:
        query = urlencode({"state": "all", "labels": "new-poll", "per_page": 100, "page": page})
        batch = _github_request(f"{GITHUB_API}/repos/{repo}/issues?{query}", token)
        if not batch:
            break
        issues.extend(batch)
        page += 1
    return issues


def build_issue_body(entry: dict, repo: str) -> str:
    filename = _clean(entry, "filename")
    pdf_url = _clean(entry, "url")
    scope_note = ""
    if looks_out_of_scope(entry):
        scope_note = (
            "\n> ⚠️ Le titre ressemble à un baromètre de popularité plutôt qu'à des\n"
            "> intentions de vote. À fermer si la notice ne contient pas de premier\n"
            "> ou de second tour.\n"
        )

    return f"""## Nouveau sondage présidentiel détecté

**Fichier PDF à vérifier:** `{filename}`

**URL PDF:** {pdf_url}
{scope_note}
### Informations complémentaires
- **Nom:** {_clean(entry, "name")}
- **Année:** {_clean(entry, "year")}
- **Date de création:** {_clean(entry, "pdf creation-date")}

### Ressources
- 📁 [Voir dans le catalogue]({CATALOG_REPO_URL})
- 📄 [Télécharger le PDF]({pdf_url})
- 📖 [Guide d'ajout de sondage](https://github.com/{repo}/blob/main/COMMENT_AJOUTER_UN_SONDAGE.md)

### À faire
- [ ] Vérifier le PDF `{filename}`
- [ ] Extraire les données du sondage
- [ ] Créer le fichier `polls/<poll_id>.csv`
- [ ] Ajouter les métadonnées dans `polls.csv`
- [ ] Vérifier que les candidats existent dans `candidats.csv`
- [ ] Tester avec `pytest`
- [ ] Vérifier le merge avec `python merge.py`

---
*Issue créée automatiquement par le workflow check-new-polls*
"""


def create_issue(entry: dict, repo: str, token: str) -> dict:
    payload = {
        "title": f"📊 Nouveau sondage: {_clean(entry, 'name')}",
        "body": build_issue_body(entry, repo),
        "labels": ISSUE_LABELS,
    }
    return _github_request(f"{GITHUB_API}/repos/{repo}/issues", token, method="POST", payload=payload)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--create", action="store_true", help="actually create the issues")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="max issues per run")
    parser.add_argument("--since", help="ignore notices published before this ISO date")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")

    if args.create and not (token and args.repo):
        print("❌ --create needs GITHUB_TOKEN and --repo (or GITHUB_REPOSITORY)")
        return 1

    since = datetime.date.fromisoformat(args.since) if args.since else read_since(datetime.date.today())
    print(f"📥 Catalogue : {CATALOG_URL}")
    try:
        catalog = fetch_catalog()
    except (HTTPError, OSError) as e:
        print(f"❌ Catalogue injoignable : {e}")
        return 1
    print(f"📊 Notices présidentielles au catalogue : {len(catalog)}")

    known = tracked_filenames()
    print(f"📁 Notices déjà intégrées dans polls.csv : {len(known)}")

    if token and args.repo:
        try:
            issues = list_poll_issues(args.repo, token)
            reported = filenames_in_issues(issues)
            print(f"🎫 Notices déjà signalées par une issue : {len(reported)} (sur {len(issues)} issues)")
            known |= reported
        except (HTTPError, OSError) as e:
            print(f"⚠️  Issues illisibles ({e}), dédoublonnage sur polls.csv seulement")

    new_notices = select_new_notices(catalog, known, since)
    print(f"✨ Nouvelles notices depuis {since.isoformat()} : {len(new_notices)}")

    if not new_notices:
        return 0

    batch = new_notices[: args.limit]
    if len(new_notices) > len(batch):
        print(f"ℹ️  {len(new_notices) - len(batch)} notice(s) reportée(s) au prochain passage (--limit {args.limit})")

    created = 0
    for entry in batch:
        published = notice_date(entry)
        hint = " (hors périmètre ?)" if looks_out_of_scope(entry) else ""
        label = f"{published} {_clean(entry, 'filename')}{hint}"
        if not args.create:
            print(f"  · {label}")
            continue
        try:
            issue = create_issue(entry, args.repo, token)
            print(f"  ✅ #{issue['number']} {label}")
            created += 1
        except (HTTPError, OSError) as e:
            print(f"  ❌ {label} : {e}")

    if not args.create:
        print("\n💡 Aperçu seulement. Relancer avec --create pour ouvrir les issues.")
        return 0

    print(f"\n🎉 {created} issue(s) créée(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
