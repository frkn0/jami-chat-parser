import argparse
import csv
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

RS = "\x1e"  # record separator between commits
FS = "\x1f"  # field separator between fields within a commit

# Fields pulled straight from git. %B is the "raw" full commit message body,
# which for Jami is the JSON payload itself.
LOG_FORMAT = f"%H{FS}%P{FS}%an{FS}%ae{FS}%aI{FS}%cI{FS}%B{RS}"


def run_git_log(repo_path: Path):
    cmd = [
        "git", "-C", str(repo_path),
        "log",
        "--all",
        "--reverse",
        f"--pretty=format:{LOG_FORMAT}",
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git log failed in {repo_path}: {result.stderr.strip()}")
    return result.stdout


def fmt_duration(ms_str):
    try:
        ms = int(ms_str)
    except (TypeError, ValueError):
        return "", ""
    seconds = ms / 1000.0
    td = timedelta(seconds=round(seconds))
    return str(td), f"{seconds:.3f}"


def classify(body_raw: str):
    body = body_raw.strip()

    row = {
        "kind": "unparsed",
        "subtype": "",
        "summary": "",
        "peer_to": "",
        "duration_hms": "",
        "duration_sec": "",
        "content": "",
        "file_name": "",
        "file_size": "",
        "file_sha3": "",
        "raw_json": "",
    }

    if not body:
        row["kind"] = "empty"
        return row

    parsed = None
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if parsed is None:
        if body.lower().startswith("merge"):
            row["kind"] = "merge"
            row["summary"] = body.splitlines()[0]
        else:
            row["kind"] = "text_raw"
            row["subtype"] = "non-json message body"
            row["content"] = body
            row["summary"] = (body[:80] + "…") if len(body) > 80 else body
        return row

    if not isinstance(parsed, dict):
        row["kind"] = "unparsed"
        row["summary"] = str(parsed)[:120]
        row["raw_json"] = json.dumps(parsed, ensure_ascii=False)
        return row

    row["raw_json"] = json.dumps(parsed, ensure_ascii=False)
    row["peer_to"] = parsed.get("to", "")
    msg_type = parsed.get("type", "")

    if msg_type == "application/call-history+json":
        row["kind"] = "call"
        duration_raw = parsed.get("duration", "0")
        hms, secs = fmt_duration(duration_raw)
        row["duration_hms"] = hms
        row["duration_sec"] = secs
        reason = parsed.get("reason", "")
        try:
            dur_ms = int(duration_raw)
        except (TypeError, ValueError):
            dur_ms = 0

        if reason == "missed":
            row["subtype"] = "missed"
            row["summary"] = "Missed call"
        elif reason == "busy":
            row["subtype"] = "busy"
            row["summary"] = "Busy / declined"
        elif dur_ms == 0:
            row["subtype"] = reason or "no-answer"
            row["summary"] = f"Call not connected ({reason or 'unknown reason'})"
        else:
            row["subtype"] = "completed"
            row["summary"] = f"Call completed, duration {hms}"

    elif msg_type == "text/plain":
        row["kind"] = "message"
        row["content"] = parsed.get("body", "")
        row["summary"] = (row["content"][:80] + "…") if len(row["content"]) > 80 else row["content"]

    elif msg_type == "application/data-transfer+json":
        row["kind"] = "file_transfer"
        row["file_name"] = parsed.get("displayName") or parsed.get("fileName", "")
        row["file_size"] = parsed.get("totalSize") or parsed.get("size", "")
        row["file_sha3"] = parsed.get("sha3sum", "")
        row["summary"] = f"File: {row['file_name']} ({row['file_size']} bytes)"

    elif msg_type == "member":
        row["kind"] = "member_event"
        action = parsed.get("action", "")
        member = parsed.get("uri") or parsed.get("member", "")
        row["subtype"] = action
        row["content"] = member
        row["summary"] = f"Member {action}: {member}"

    elif msg_type == "initial":
        row["kind"] = "conversation_created"
        row["summary"] = "Conversation initial commit"

    # Fallback
    else:
        row["kind"] = "other"
        row["subtype"] = msg_type
        row["summary"] = f"Unrecognized type '{msg_type}' — see raw_json"

    return row


def parse_repo(repo_path: Path, self_id: str | None):
    raw = run_git_log(repo_path)
    if not raw:
        return []

    rows = []
    for record in raw.split(RS):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(FS, 5)
        if len(parts) < 6:
            # malformed record, keep it visible rather than crashing
            rows.append({
                "conversation": repo_path.name,
                "commit": "",
                "parents": "",
                "author_name": "",
                "author_email": "",
                "author_date": "",
                "committer_date": "",
                "is_merge": "",
                "direction": "",
                "kind": "malformed_record",
                "subtype": "",
                "summary": "",
                "peer_to": "",
                "duration_hms": "",
                "duration_sec": "",
                "content": "",
                "file_name": "",
                "file_size": "",
                "file_sha3": "",
                "raw_json": "",
                "raw_body": record,
            })
            continue

        commit_hash, parents, author_name, author_email, author_date, rest = parts
        # rest still contains committer_date + FS + body, split once more
        committer_date, body = rest.split(FS, 1)

        parsed_fields = classify(body)

        direction = ""
        if self_id:
            if author_email == self_id or author_name == self_id:
                direction = "outgoing"
            else:
                direction = "incoming"

        rows.append({
            "conversation": repo_path.name,
            "commit": commit_hash,
            "parents": parents,
            "author_name": author_name,
            "author_email": author_email,
            "author_date": author_date,
            "committer_date": committer_date,
            "is_merge": "yes" if len(parents.split()) > 1 else "",
            "direction": direction,
            **parsed_fields,
            "raw_body": body.strip(),
        })

    return rows


def find_repos(root: Path):
    if (root / ".git").exists():
        return [root]
 
    repos = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (child / ".git").exists():
            repos.append(child)
        else:
            print(f"[!] Skipping {child.name}: no .git found, not a conversation repo", file=sys.stderr)
    return repos


FIELDNAMES = [
    "conversation", "commit", "parents", "is_merge",
    "author_date", "committer_date",
    "author_name", "author_email", "direction",
    "kind", "subtype", "summary",
    "peer_to", "duration_hms", "duration_sec",
    "content", "file_name", "file_size", "file_sha3",
    "raw_json", "raw_body",
]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="Path to a single Jami conversation repo, or a folder containing several")
    ap.add_argument("-o", "--output", default="jami_log.csv", help="Output CSV path (default: jami_export.csv)")
    ap.add_argument("--self-id", default=None,
                    help="Your own account/device hash (as it appears as commit author), "
                         "to fill in an incoming/outgoing direction column")
    args = ap.parse_args()

    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        sys.exit(f"Path not found: {root}")

    repos = find_repos(root)
    if not repos:
        sys.exit(f"No git repositories found at or under: {root}")

    all_rows = []
    for repo in repos:
        try:
            rows = parse_repo(repo, args.self_id)
            all_rows.extend(rows)
            print(f"[+] {repo.name}: {len(rows)} commits parsed")
        except RuntimeError as e:
            print(f"[!] Skipping {repo}: {e}", file=sys.stderr)

    all_rows.sort(key=lambda r: r.get("author_date", ""))

    out_path = Path(args.output).expanduser().resolve()
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    counts = {}
    for r in all_rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1

    print(f"\nWrote {len(all_rows)} rows to {out_path}")
    print("Breakdown by kind:")
    for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:20s} {n}")


if __name__ == "__main__":
    main()
