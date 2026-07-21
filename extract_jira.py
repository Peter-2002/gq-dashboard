#!/usr/bin/env python3
"""
extract_jira.py
---------------
Pulls tasks from a Jira project and writes data.json in the exact shape
the executive dashboard (boss's HTML) expects: projects[] each containing tasks[].

MAPPING (decided): each Jira card = a task; cards are grouped into "projects" by Brand.
  assignees[] = [Assignee] + [Co-owners] + [Reviewer]
  turnaround_days = completed_date - start_date (only for completed cards)

Custom fields are auto-discovered by NAME so you don't need to look up
customfield_XXXXX ids manually.

USAGE:
  1. pip install requests
  2. Set three environment variables (do NOT hardcode the token):
       export JIRA_BASE="https://gqgroup-team.atlassian.net"
       export JIRA_EMAIL="kittiphoom.t@gqgroup.com"
       export JIRA_TOKEN="your_api_token_from_id.atlassian.net"
  3. python extract_jira.py --project DM
  4. It writes data.json next to the dashboard.
"""

import os, sys, json, argparse, datetime, base64
import requests

# ---- field NAMES as you created them in Jira (edit here if you renamed) ----
FIELD_NAMES = {
    "brand":         "Brand",
    "work_type":     "Work Type",
    "tier":          "Tier",
    "priority":      "Priority",          # built-in, but we read it uniformly
    "co_owner":      "Co-owner",
    "reviewer":      "Reviewer",
    "collection":    "Collection Type",
    "update_type":   "Product Update Type",
    "delay_reason":  "Delay Reason",
    "start_date":    "Start date",        # built-in
    "due_date":      "Due date",          # built-in
}

# Which Jira statuses count as "done" (adjust to your workflow)
DONE_STATUSES = {"Delivered", "Closed", "Done"}
INPROGRESS_HINT = {"In Progress", "Internal Review", "Revision", "Pending Feedback",
                   "Final QC", "In Planning", "Brief Review"}


def auth_header():
    email = os.environ["JIRA_EMAIL"]
    token = os.environ["JIRA_TOKEN"]
    raw = f"{email}:{token}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode(),
            "Accept": "application/json"}


def discover_fields(base):
    """Map human field names -> Jira internal ids (customfield_xxxxx)."""
    r = requests.get(f"{base}/rest/api/3/field", headers=auth_header(), timeout=30)
    r.raise_for_status()
    by_name = {}
    for f in r.json():
        by_name[f["name"].strip().lower()] = f["id"]
    resolved = {}
    for key, name in FIELD_NAMES.items():
        fid = by_name.get(name.strip().lower())
        resolved[key] = fid  # may be None if not found; handled later
    return resolved


def get_all_issues(base, project_key, fields_ids):
    """Fetch every issue in the project via /rest/api/3/search/jql."""
    want = ["summary", "status", "assignee", "priority", "duedate", "created", "resolutiondate"]
    want += [fid for fid in fields_ids.values() if fid and str(fid).startswith("customfield")]
    for k in ("start_date",):
        if fields_ids.get(k) and str(fields_ids[k]).startswith("customfield"):
            want.append(fields_ids[k])
    # de-dupe and drop any None/empty
    want = [w for w in dict.fromkeys(want) if w]

    issues = []
    next_token = None
    while True:
        payload = {"jql": f'project = {project_key} ORDER BY created ASC',
                   "maxResults": 100, "fields": want}
        if next_token:
            payload["nextPageToken"] = next_token
        r = requests.post(f"{base}/rest/api/3/search/jql",
                          headers={**auth_header(), "Content-Type": "application/json"},
                          json=payload, timeout=60)
        if r.status_code >= 400:
            # surface Jira's actual complaint instead of a generic error
            print("---- Jira returned an error ----", file=sys.stderr)
            print("status:", r.status_code, file=sys.stderr)
            print("body:", r.text[:800], file=sys.stderr)
            print("payload sent:", json.dumps(payload)[:500], file=sys.stderr)
            r.raise_for_status()
        data = r.json()
        issues.extend(data.get("issues", []))
        next_token = data.get("nextPageToken")
        if data.get("isLast", True) or not next_token:
            break
    return issues


def name_of(user):
    if not user:
        return None
    return user.get("displayName")


def field_val(fields, fid):
    if not fid:
        return None
    v = fields.get(fid)
    if isinstance(v, dict):
        return v.get("value") or v.get("name") or v.get("displayName")
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                out.append(x.get("value") or x.get("name") or x.get("displayName"))
            else:
                out.append(x)
        return out
    return v


def days_between(a, b):
    if not a or not b:
        return None
    try:
        da = datetime.date.fromisoformat(a[:10])
        db = datetime.date.fromisoformat(b[:10])
        return (db - da).days
    except Exception:
        return None


def build(base, project_key):
    ids = discover_fields(base)
    missing = [k for k, v in ids.items() if v is None]
    if missing:
        print(f"[warn] these fields were not found by name (will be blank): {missing}", file=sys.stderr)

    issues = get_all_issues(base, project_key, ids)
    today = datetime.date.today().isoformat()

    tasks = []
    for it in issues:
        f = it["fields"]
        status = (f.get("status") or {}).get("name", "")
        is_done = status in DONE_STATUSES
        due = f.get("duedate")
        start = field_val(f, ids.get("start_date")) or f.get("created")
        completed = f.get("resolutiondate")

        # assignees = Assignee + Co-owner(s) + Reviewer
        people = []
        a = name_of(f.get("assignee"))
        if a:
            people.append(a)
        co = field_val(f, ids.get("co_owner"))
        if isinstance(co, list):
            people += [c for c in co if c]
        elif co:
            people.append(co)
        rev = field_val(f, ids.get("reviewer"))
        if isinstance(rev, list):
            people += [r for r in rev if r]
        elif rev:
            people.append(rev)
        people = list(dict.fromkeys(people))  # dedupe, keep order

        overdue = bool(due and not is_done and due[:10] < today)
        turn = days_between(start, completed) if is_done else None

        brand = field_val(f, ids.get("brand")) or "Unassigned Brand"
        pr = f.get("priority") or {}
        tasks.append({
            "id": it["key"],
            "name": f.get("summary", ""),
            "bucket": status,                    # boss's 'bucket' = our stage/status
            "status": "Completed" if is_done else ("In Progress" if status in INPROGRESS_HINT else "Not Started"),
            "priority": pr.get("name", "Medium"),
            "assignees": people,
            "created": (f.get("created") or "")[:10],
            "due": (due or "")[:10],
            "start": (start or "")[:10],
            "completed": (completed or "")[:10],
            "overdue": overdue,
            "labels": "",
            "project": brand,
            "project_id": brand,
            "turnaround_days": turn,
            "work_type": field_val(f, ids.get("work_type")),
            "tier": field_val(f, ids.get("tier")),
            "delay_reason": field_val(f, ids.get("delay_reason")),
        })

    # group tasks into "projects" by Brand
    projects = {}
    for t in tasks:
        p = projects.setdefault(t["project"], {
            "id": t["project"], "name": t["project"], "total": 0,
            "completed": 0, "in_progress": 0, "not_started": 0,
            "overdue": 0, "urgent": 0, "buckets": set(), "tasks": [],
            "_starts": [], "_ends": []})
        p["total"] += 1
        if t["status"] == "Completed":
            p["completed"] += 1
        elif t["status"] == "In Progress":
            p["in_progress"] += 1
        else:
            p["not_started"] += 1
        if t["overdue"]:
            p["overdue"] += 1
        if t["priority"] in ("Urgent", "Highest", "High"):
            p["urgent"] += 1
        if t["bucket"]:
            p["buckets"].add(t["bucket"])
        if t["start"]:
            p["_starts"].append(t["start"])
        if t["completed"]:
            p["_ends"].append(t["completed"])
        p["tasks"].append(t)

    proj_list = []
    for p in projects.values():
        p["completion_pct"] = round(100 * p["completed"] / p["total"]) if p["total"] else 0
        p["start_date"] = min(p["_starts"]) if p["_starts"] else ""
        p["end_date"] = max(p["_ends"]) if p["_ends"] else ""
        p["buckets"] = sorted(p["buckets"])
        del p["_starts"], p["_ends"]
        proj_list.append(p)

    # ---- overall KPIs ----
    total = len(tasks)
    completed = sum(1 for t in tasks if t["status"] == "Completed")
    in_prog = sum(1 for t in tasks if t["status"] == "In Progress")
    not_started = sum(1 for t in tasks if t["status"] == "Not Started")
    overdue_ct = sum(1 for t in tasks if t["overdue"])
    urgent_ct = sum(1 for t in tasks if t["priority"] in ("Urgent", "Highest", "High"))
    unassigned = sum(1 for t in tasks if not t["assignees"])
    overall = {"total": total, "completed": completed, "in_progress": in_prog,
               "not_started": not_started, "overdue": overdue_ct, "urgent": urgent_ct,
               "unassigned": unassigned,
               "completion_pct": round(100 * completed / total) if total else 0}

    # ---- workload: per person (assignee + co-owner + reviewer) ----
    wl = {}
    for t in tasks:
        for person in t["assignees"]:
            w = wl.setdefault(person, {"name": person, "total": 0, "completed": 0,
                                       "in_progress": 0, "not_started": 0})
            w["total"] += 1
            if t["status"] == "Completed":
                w["completed"] += 1
            elif t["status"] == "In Progress":
                w["in_progress"] += 1
            else:
                w["not_started"] += 1
    workload = sorted(wl.values(), key=lambda x: -x["total"])

    # ---- turnaround by work type and by project (avg days, completed only) ----
    def avg_turn(group_key):
        agg = {}
        for t in tasks:
            if t["status"] != "Completed" or t["turnaround_days"] is None:
                continue
            k = t.get(group_key) or "Unspecified"
            agg.setdefault(k, []).append(t["turnaround_days"])
        return [{"type": k, "name": k, "avg": round(sum(v) / len(v), 1), "n": len(v)}
                for k, v in sorted(agg.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))]
    turnaround = {"by_work_type": avg_turn("work_type"), "by_project": avg_turn("project")}

    # ---- deliverable types (by work_type: total vs completed) ----
    dt = {}
    for t in tasks:
        k = t.get("work_type") or "Other"
        d = dt.setdefault(k, {"type": k, "count": 0, "completed": 0, "n": 0})
        d["count"] += 1
        d["n"] += 1
        if t["status"] == "Completed":
            d["completed"] += 1
    deliverable_types = sorted(dt.values(), key=lambda x: -x["count"])

    # ---- monthly completed (velocity) ----
    mc = {}
    for t in tasks:
        if t["status"] == "Completed" and t["completed"]:
            m = t["completed"][:7]  # YYYY-MM
            mc[m] = mc.get(m, 0) + 1
    monthly_completed = [{"month": m, "count": c} for m, c in sorted(mc.items())]

    # ---- overdue list ----
    overdue_list = sorted(
        [{"id": t["id"], "name": t["name"], "project": t["project"],
          "due": t["due"], "assignees": t["assignees"], "priority": t["priority"]}
         for t in tasks if t["overdue"]],
        key=lambda x: x["due"])

    # ---- upcoming deadlines (open, has due date, soonest first) ----
    upcoming = sorted(
        [{"id": t["id"], "name": t["name"], "project": t["project"],
          "due": t["due"], "assignees": t["assignees"], "priority": t["priority"]}
         for t in tasks if t["due"] and t["status"] != "Completed" and not t["overdue"]],
        key=lambda x: x["due"])[:15]

    # ---- exec summary (auto text) ----
    exec_summary = {
        "headline": f"{completion_line(overall)}",
        "overdue": overdue_ct, "urgent": urgent_ct, "in_progress": in_prog,
        "avg_turnaround_days": (round(sum(t["turnaround_days"] for t in tasks
                                          if t["turnaround_days"] is not None) /
                                      max(1, sum(1 for t in tasks if t["turnaround_days"] is not None)), 1))
    }

    overall["avg_turnaround_days"] = exec_summary["avg_turnaround_days"]

    return {
        "generated": today,
        "projects": proj_list,
        "overall": overall,
        "workload": workload,
        "turnaround": turnaround,
        "deliverable_types": deliverable_types,
        "monthly_completed": monthly_completed,
        "overdue": overdue_list,
        "upcoming": upcoming,
        "exec_summary": exec_summary,
    }


def completion_line(overall):
    return (f"{overall['completed']} of {overall['total']} tasks complete "
            f"({overall['completion_pct']}%).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="Jira project key, e.g. DM")
    ap.add_argument("--out", default="data.json")
    args = ap.parse_args()
    base = os.environ["JIRA_BASE"].rstrip("/")
    data = build(base, args.project)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    n = sum(len(p["tasks"]) for p in data["projects"])
    print(f"Wrote {args.out}: {len(data['projects'])} projects (brands), {n} tasks")