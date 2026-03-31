#!/usr/bin/env python3
"""
build_graph.py — rebuild docs/data/graph.json from live monitor and Compossible data.

Sources (all public URLs, no auth required):
  • https://asym-intel.info/monitors/{slug}/data/report-latest.json  (×7)
  • https://asym-intel.info/monitors/{slug}/data/archive.json         (×7)
  • https://compossible.asym-intel.info/posts-index.json

Outputs:
  • docs/data/graph.json

Edge design:
  - Monitor→monitor edges sourced from cross_monitor_flags in each report-latest.json.
    Edges carry first_flagged date, report_slug, and a direct report_url so every
    connection is traceable to the exact weekly report that identified it.
  - Compossible→monitor edges inferred from monitors_referenced frontmatter (primary)
    or graph_tags (fallback), capped at 3 per post. Poetry/creative posts excluded.
  - Series-chain edges (extends) between sequential posts in the same series.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ── Configuration ────────────────────────────────────────────────────────────

MONITORS = [
    {
        "id":    "ai-governance",
        "label": "AI Governance",
        "url":   "https://asym-intel.info/monitors/ai-governance/dashboard.html",
        "color": "#4f98a3",
    },
    {
        "id":    "conflict-escalation",
        "label": "Strategic Conflict & Escalation",
        "url":   "https://asym-intel.info/monitors/conflict-escalation/dashboard.html",
        "color": "#c0392b",
    },
    {
        "id":    "democratic-integrity",
        "label": "Democratic Integrity",
        "url":   "https://asym-intel.info/monitors/democratic-integrity/dashboard.html",
        "color": "#8e44ad",
    },
    {
        "id":    "environmental-risks",
        "label": "Environmental Risks",
        "url":   "https://asym-intel.info/monitors/environmental-risks/dashboard.html",
        "color": "#27ae60",
    },
    {
        "id":    "european-strategic-autonomy",
        "label": "European Strategic Autonomy",
        "url":   "https://asym-intel.info/monitors/european-strategic-autonomy/dashboard.html",
        "color": "#2980b9",
    },
    {
        "id":    "fimi-cognitive-warfare",
        "label": "FIMI & Cognitive Warfare",
        "url":   "https://asym-intel.info/monitors/fimi-cognitive-warfare/dashboard.html",
        "color": "#e67e22",
    },
    {
        "id":    "macro-monitor",
        "label": "Macro Monitor",
        "url":   "https://asym-intel.info/monitors/macro-monitor/dashboard.html",
        "color": "#f39c12",
    },
]

BASE_URL    = "https://asym-intel.info"
COMP_INDEX  = "https://compossible.asym-intel.info/posts-index.json"
REPORT_URL  = "{base}/monitors/{slug}/data/report-latest.json"
ARCHIVE_URL = "{base}/monitors/{slug}/data/archive.json"

# Canonical monitor slug lookup — handles all name variants seen across monitors
MONITOR_NAME_TO_SLUG = {
    # Full canonical names
    "ai governance":                          "ai-governance",
    "global fimi & cognitive warfare":        "fimi-cognitive-warfare",
    "global fimi & cognitive warfare monitor":"fimi-cognitive-warfare",
    "fimi & cognitive warfare":               "fimi-cognitive-warfare",
    "fimi & cognitive warfare monitor":       "fimi-cognitive-warfare",
    "fimi":                                   "fimi-cognitive-warfare",
    "democratic integrity":                   "democratic-integrity",
    "democratic integrity monitor":           "democratic-integrity",
    "world democracy monitor":                "democratic-integrity",
    "strategic conflict & escalation":        "conflict-escalation",
    "strategic conflict and escalation":      "conflict-escalation",
    "conflict escalation":                    "conflict-escalation",
    "conflict & escalation":                  "conflict-escalation",
    "environmental risks":                    "environmental-risks",
    "global environmental risks":             "environmental-risks",
    "environmental risks monitor":            "environmental-risks",
    "european strategic autonomy":            "european-strategic-autonomy",
    "european strategic autonomy monitor":    "european-strategic-autonomy",
    "european geopolitical & hybrid threat monitor": "european-strategic-autonomy",
    "european geopolitical and hybrid threat monitor": "european-strategic-autonomy",
    "eghtm":                                  "european-strategic-autonomy",
    "macro monitor":                          "macro-monitor",
    "macro-monitor":                          "macro-monitor",
    "ai governance monitor":                  "ai-governance",
    # Additional variants found in the wild
    "strategic conflict & escalation monitor":         "conflict-escalation",
    "strategic conflict and escalation monitor":       "conflict-escalation",
    "global environmental risks & planetary boundaries monitor": "environmental-risks",
    "environmental risks & planetary boundaries monitor": "environmental-risks",
    "environmental risks and planetary boundaries monitor": "environmental-risks",
    "global environmental risks and planetary boundaries monitor": "environmental-risks",
    "global environmental risks":             "environmental-risks",
    "european geopolitical & hybrid threat monitor (eghtm)": "european-strategic-autonomy",
    "monitoring":                             None,  # classification value, not a monitor name
}

# graph_tag → monitor slug mapping for Compossible fallback edges
GRAPH_TAG_TO_MONITOR = {
    "ai-governance":       "ai-governance",
    "democratic-integrity":"democratic-integrity",
    "ecological-crisis":   "environmental-risks",
    "environmental-risks": "environmental-risks",
    "hybrid-warfare":      "fimi-cognitive-warfare",
    "fimi":                "fimi-cognitive-warfare",
    "european-autonomy":   "european-strategic-autonomy",
    "geopolitical-competition": "european-strategic-autonomy",
    "ukraine-conflict":    "conflict-escalation",
    "conflict-escalation": "conflict-escalation",
    "far-right-networks":  "democratic-integrity",
    "wealth-inequality":   "macro-monitor",
    "tech-power-concentration": "ai-governance",
}

# Post types to exclude from graph (no analytical connections to monitors)
EXCLUDED_POST_TYPES = {"poetry", "music"}
EXCLUDED_GRAPH_TAGS = {"creative-writing"}

# Relation type for cross-monitor flags
FLAG_RELATION_MAP = {
    "structural": "contextualises",
    "transient":  "correlates",
    "emerging":   "correlates",
    "causal":     "analyses",
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def fetch_json(url: str) -> dict | list | None:
    """Fetch JSON from URL, return None on failure."""
    try:
        req = Request(url, headers={"User-Agent": "asym-intel-graph-builder/1.0"})
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, json.JSONDecodeError) as e:
        print(f"  WARN: could not fetch {url}: {e}", file=sys.stderr)
        return None


def normalise_slug(name: str) -> str | None:
    """Convert a monitor name (in any variant) to its canonical slug."""
    return MONITOR_NAME_TO_SLUG.get(name.strip().lower())


def extract_target_slug(flag: dict) -> str | None:
    """
    Extract the target monitor slug from a cross_monitor_flag entry.
    Handles all field name variants used across the 7 monitors.
    """
    # Direct slug fields (most reliable)
    for key in ("target_slug", "monitor_slug"):
        v = flag.get(key)
        if v:
            return v.strip()

    # Name fields that need lookup
    for key in ("monitor", "target_monitor"):
        v = flag.get(key)
        if v:
            slug = normalise_slug(v)
            if slug:
                return slug

    # monitors_involved is a list (democratic-integrity style)
    involved = flag.get("monitors_involved", [])
    for name in involved:
        slug = normalise_slug(name)
        if slug:
            return slug

    # Last resort: extract slug from monitor_url
    url = flag.get("monitor_url") or flag.get("source_url") or ""
    if url:
        # e.g. https://asym-intel.info/monitors/fimi-cognitive-warfare/dashboard.html
        m = __import__('re').search(r'/monitors/([^/]+)', url)
        if m:
            return m.group(1)

    return None


def flag_relation(flag: dict) -> str:
    """Map flag classification/type to a graph relation type."""
    for key in ("classification", "structural_or_transient", "type", "significance"):
        v = str(flag.get(key, "")).lower()
        for keyword, relation in FLAG_RELATION_MAP.items():
            if keyword in v:
                return relation
    return "contextualises"  # default


def flag_first_date(flag: dict) -> str | None:
    """Extract the first-flagged date from a flag (handles field name variants)."""
    for key in ("first_flagged", "first_raised", "last_updated"):
        v = flag.get(key)
        if v:
            return v[:10]  # normalise to YYYY-MM-DD
    return None


def flag_description(flag: dict) -> str:
    """Extract the descriptive text from a flag (handles field name variants)."""
    for key in ("linkage", "body", "detail", "description"):
        v = flag.get(key)
        if v:
            return v[:400] + ("…" if len(v) > 400 else "")
    return ""


def flag_title(flag: dict) -> str:
    """Extract the flag title."""
    for key in ("title", "headline", "signal"):
        v = flag.get(key)
        if v:
            return v
    return ""


def flag_id(flag: dict) -> str | None:
    """Extract the flag id."""
    for key in ("id", "flag_id"):
        v = flag.get(key)
        if v:
            return str(v)
    return None


def build_report_url(base_url: str, monitor_slug: str, report_slug: str,
                     source_url: str | None) -> str:
    """
    Build the direct URL to a specific archived report.
    Prefers source_url if present (some monitors supply it directly).
    Falls back to constructing from base + monitor slug + report slug.
    """
    if source_url and source_url.startswith("http"):
        return source_url
    # Construct: e.g. https://asym-intel.info/monitors/ai-governance/2026-03-30-weekly-digest/
    # or just link to the dashboard if we can't build a better URL
    if report_slug and report_slug not in ("", monitor_slug):
        return f"{base_url}/monitors/{monitor_slug}/{report_slug}/"
    return f"{base_url}/monitors/{monitor_slug}/dashboard.html"


def truncate(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s


# ── Main build ───────────────────────────────────────────────────────────────

def build_graph(output_path: Path) -> None:
    nodes = []
    edges = []
    edge_ids_seen: set[str] = set()
    edge_counter = [0]

    def next_edge_id(prefix="e") -> str:
        edge_counter[0] += 1
        return f"{prefix}-{edge_counter[0]:03d}"

    def add_edge(source, target, relation, title, rationale,
                 flag_id_val=None, report_url=None, first_flagged=None,
                 edge_type="monitor-monitor", status="Active"):
        # Deduplicate: one edge per source/target pair per relation
        key = f"{source}|{target}|{relation}"
        rev = f"{target}|{source}|{relation}"
        if key in edge_ids_seen or rev in edge_ids_seen:
            return
        edge_ids_seen.add(key)
        e = {
            "id":           next_edge_id(),
            "source":       source,
            "target":       target,
            "relation":     relation,
            "title":        title,
            "rationale":    rationale,
            "edge_type":    edge_type,
            "status":       status,
        }
        if flag_id_val:
            e["flag_id"] = flag_id_val
        if first_flagged:
            e["first_flagged"] = first_flagged
        if report_url:
            e["report_url"] = report_url
        edges.append(e)

    # ── 1. Fetch monitor data ────────────────────────────────────────────────
    print("Fetching monitor reports…")
    monitor_data = {}  # slug → {report, archive}

    for m in MONITORS:
        slug = m["id"]
        print(f"  {slug}")

        report = fetch_json(REPORT_URL.format(base=BASE_URL, slug=slug))
        archive = fetch_json(ARCHIVE_URL.format(base=BASE_URL, slug=slug))

        monitor_data[slug] = {"report": report, "archive": archive, "meta": m}

    # ── 2. Build monitor nodes ───────────────────────────────────────────────
    print("Building monitor nodes…")
    for slug, data in monitor_data.items():
        m_meta = data["meta"]
        report  = data["report"] or {}
        archive = data["archive"] or []
        meta    = report.get("meta", {})

        # Published date / week label from report
        published = meta.get("published", "")[:10] or ""
        week_label = meta.get("week_label", "")
        report_slug = meta.get("slug", "")
        issue = meta.get("issue")

        # Build URL to this specific report issue (for "Open report" button)
        # If archive has entries, use the latest entry's slug to construct URL
        report_url = m_meta["url"]  # default: dashboard
        if archive and isinstance(archive, list):
            latest_entry = max(archive, key=lambda x: x.get("published", ""), default=None)
            if latest_entry:
                entry_slug = latest_entry.get("slug", "")
                src = None
                report_url = build_report_url(BASE_URL, slug, entry_slug, src)

        # Description: use signal from latest archive entry if available
        description = ""
        if archive and isinstance(archive, list):
            latest = max(archive, key=lambda x: x.get("published", ""), default=None)
            if latest:
                description = truncate(latest.get("signal", ""), 300)

        nodes.append({
            "id":          slug,
            "type":        "monitor",
            "monitor":     slug,
            "label":       m_meta["label"],
            "color":       m_meta["color"],
            "url":         report_url,
            "dashboard_url": m_meta["url"],
            "week_label":  week_label,
            "date":        published,
            "report_slug": report_slug,
            "issue":       issue,
            "description": description,
        })

    # ── 3. Monitor→monitor edges from cross_monitor_flags ───────────────────
    print("Building monitor→monitor edges from cross_monitor_flags…")
    for slug, data in monitor_data.items():
        report = data["report"] or {}
        meta   = report.get("meta", {})
        report_slug = meta.get("slug", "")

        flags = report.get("cross_monitor_flags", {}).get("flags", [])
        for flag in flags:
            target_slug = extract_target_slug(flag)
            if not target_slug:
                print(f"  WARN: could not resolve target for flag in {slug}: {flag.get('title','?')[:60]}")
                continue
            if target_slug == slug:
                continue  # skip self-loops

            relation     = flag_relation(flag)
            title        = flag_title(flag) or f"{slug} ↔ {target_slug}"
            rationale    = flag_description(flag)
            first_date   = flag_first_date(flag)
            fid          = flag_id(flag)
            status       = flag.get("status", "Active")

            # Source URL: prefer explicit source_url in flag, then build from report slug
            source_url_in_flag = flag.get("source_url") or flag.get("monitor_url")
            report_url = build_report_url(BASE_URL, slug, report_slug, source_url_in_flag)

            add_edge(
                source=slug,
                target=target_slug,
                relation=relation,
                title=title,
                rationale=rationale,
                flag_id_val=fid,
                report_url=report_url,
                first_flagged=first_date,
                edge_type="monitor-monitor",
                status=status,
            )

    print(f"  → {sum(1 for e in edges if e['edge_type'] == 'monitor-monitor')} monitor→monitor edges")

    # ── 4. Compossible posts ─────────────────────────────────────────────────
    print("Fetching Compossible posts-index…")
    posts_index = fetch_json(COMP_INDEX) or {}
    posts = posts_index.get("posts", []) if isinstance(posts_index, dict) else []
    print(f"  {len(posts)} posts found")

    monitor_ids = {m["id"] for m in MONITORS}
    compossible_nodes_added = 0

    for post in posts:
        post_type = post.get("post_type", "essay")
        graph_tags = post.get("graph_tags", [])
        title = post.get("title", "")
        slug = post.get("slug", "")

        if not slug or not title:
            continue

        # Exclude pure creative/poetry posts
        if post_type in EXCLUDED_POST_TYPES:
            continue
        if all(t in EXCLUDED_GRAPH_TAGS for t in graph_tags) and graph_tags:
            continue

        node_id = f"compossible-{slug}"

        # ── Determine monitor connections ────────────────────────────────────
        # Priority 1: explicit monitors_referenced frontmatter
        monitors_ref = [
            r.strip() for r in post.get("monitors_referenced", [])
            if r.strip() in monitor_ids
        ]

        # Priority 2: graph_tags mapped to monitors (fallback if monitors_referenced empty)
        if not monitors_ref:
            tag_monitors = []
            for tag in graph_tags:
                if tag in EXCLUDED_GRAPH_TAGS:
                    continue
                m = GRAPH_TAG_TO_MONITOR.get(tag)
                if m and m not in tag_monitors:
                    tag_monitors.append(m)
            monitors_ref = tag_monitors[:3]  # cap at 3

        if not monitors_ref:
            continue  # no connections → skip (orphan)

        # Determine post_type relation
        if post_type in ("series-part", "fiction"):
            relation = "extends"
        elif post_type == "essay":
            relation = "analyses"
        else:
            relation = "contextualises"

        # Add node
        nodes.append({
            "id":          node_id,
            "type":        "compossible",
            "monitor":     monitors_ref[0] if monitors_ref else None,
            "label":       title,
            "url":         post.get("url", ""),
            "date":        post.get("date", ""),
            "description": post.get("description", ""),
            "post_type":   post_type,
            "series":      post.get("series", ""),
            "series_part": post.get("series_part"),
            "graph_tags":  graph_tags,
            "monitors_referenced": monitors_ref,
        })
        compossible_nodes_added += 1

        # Add edges to each connected monitor
        for m_slug in monitors_ref[:3]:
            add_edge(
                source=node_id,
                target=m_slug,
                relation=relation,
                title=truncate(title, 80),
                rationale=f"Post references {m_slug} monitor via {'monitors_referenced frontmatter' if post.get('monitors_referenced') else 'graph_tags'}.",
                edge_type="compossible-monitor",
            )

    print(f"  → {compossible_nodes_added} Compossible nodes added")

    # ── 5. Series chain edges ────────────────────────────────────────────────
    print("Building series chain edges…")
    series_posts: dict[str, list] = {}
    for node in nodes:
        if node.get("type") == "compossible" and node.get("series"):
            series_posts.setdefault(node["series"], []).append(node)

    chain_count = 0
    for series_name, parts in series_posts.items():
        parts_sorted = sorted(
            [p for p in parts if p.get("series_part") is not None],
            key=lambda p: p["series_part"]
        )
        for i in range(len(parts_sorted) - 1):
            add_edge(
                source=parts_sorted[i]["id"],
                target=parts_sorted[i+1]["id"],
                relation="extends",
                title=f"{series_name} series chain",
                rationale=f"Sequential parts of the '{series_name}' series.",
                edge_type="series-chain",
            )
            chain_count += 1

    print(f"  → {chain_count} series chain edges")

    # ── 6. Assemble and write ────────────────────────────────────────────────
    graph = {
        "generated":   datetime.now(timezone.utc).isoformat(),
        "node_count":  len(nodes),
        "edge_count":  len(edges),
        "nodes":       nodes,
        "edges":       edges,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False))

    print(f"\n✓ graph.json written → {output_path}")
    print(f"  Nodes: {len(nodes)}  ({sum(1 for n in nodes if n['type']=='monitor')} monitors, "
          f"{sum(1 for n in nodes if n['type']=='compossible')} Compossible)")
    print(f"  Edges: {len(edges)}  ({sum(1 for e in edges if e['edge_type']=='monitor-monitor')} monitor↔monitor, "
          f"{sum(1 for e in edges if e['edge_type']=='compossible-monitor')} post→monitor, "
          f"{sum(1 for e in edges if e['edge_type']=='series-chain')} series chains)")


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    repo_root = Path(__file__).parent.parent
    output = repo_root / "docs" / "data" / "graph.json"
    build_graph(output)
