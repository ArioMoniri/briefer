"""The two analysis agents plus the hallucination-guard verifier.

Agent A (articles/posts): summary, catch-points, relevance to the company.
Agent B (events): everything Agent A does + deadlines, eligibility,
required materials and application steps.

Every run finishes with an independent verification pass that re-reads
the source and flags any claim — especially dates/deadlines/links — that
is not supported by the source material.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .config import Config
from .enrich import EnrichedContent
from .llm import LLM

log = logging.getLogger("briefer.analysis")


def _company_ctx(cfg: Config) -> str:
    return (
        f"COMPANY CONTEXT — the reader works at {cfg.company_name} "
        f"({cfg.company_url}). Focus: {cfg.company_focus}"
    )


def _images(content: EnrichedContent) -> list[dict[str, str]]:
    return [
        {"media_type": a.media_type, "data": a.data_b64}
        for a in content.attachments
        if a.kind == "image" and a.data_b64
    ]


# ---------------------------------------------------------------------------
# Classification: article/post vs event.
# ---------------------------------------------------------------------------

def classify(llm: LLM, content: EnrichedContent) -> str:
    system = (
        "Classify the SOURCE as either an EVENT or an ARTICLE.\n"
        "EVENT = a conference, hackathon, workshop, meetup, webinar, grant, "
        "call-for-applications, fellowship, competition, or anything with a "
        "date to attend or a deadline to apply (Luma/Eventbrite pages count).\n"
        "ARTICLE = a paper, blog post, news item, repo, product, thread, or "
        "general information with no application/attendance action.\n"
        'Respond as JSON: {"type": "event"|"article", "confidence": 0-1, '
        '"reason": "short"}'
    )
    user = "SOURCE:\n" + content.as_source_block()
    if content.luma_urls:
        user += "\n\n[hint] Contains a Luma link, which is usually an event."
    out = llm.json(system, user, images=_images(content), max_tokens=400)
    t = str(out.get("type", "article")).lower()
    return "event" if t == "event" else "article"


# ---------------------------------------------------------------------------
# Agent A — article / post analysis.
# ---------------------------------------------------------------------------

def analyze_article(llm: LLM, cfg: Config, content: EnrichedContent) -> dict[str, Any]:
    system = (
        _company_ctx(cfg) + "\n\n"
        "Analyse the SOURCE (which may include text, fetched links, a GitHub "
        "repo, files and images). Produce STRICT JSON with keys:\n"
        '{\n'
        '  "title": "concise title of the item",\n'
        '  "summary": "3-5 sentence factual summary, no fluff",\n'
        '  "catch_points": ["the 2-5 most important/novel takeaways"],\n'
        '  "vivax_relevance": "specifically how this could be used at the '
        'company given its focus — be concrete (a feature, a research angle, '
        'a partnership, a tool). If not relevant, say so honestly.",\n'
        '  "vivax_use_cases": ["concrete places we could apply this"],\n'
        '  "entities": ["people, orgs, products, models mentioned"],\n'
        '  "links": ["important URLs found in the source"],\n'
        '  "tags": ["3-6 topical tags"],\n'
        '  "confidence": 0-1\n'
        '}\n'
        "Only state facts present in the source. Do not invent numbers, "
        "dates, names or URLs."
    )
    user = "SOURCE:\n" + content.as_source_block()
    result = llm.json(system, user, images=_images(content), max_tokens=1800)
    result.setdefault("title", "Untitled")
    return result


# ---------------------------------------------------------------------------
# Agent B — event analysis (deadlines, criteria, application).
# ---------------------------------------------------------------------------

def analyze_event(llm: LLM, cfg: Config, content: EnrichedContent) -> dict[str, Any]:
    system = (
        _company_ctx(cfg) + "\n\n"
        "This SOURCE describes an EVENT or a call to apply. Extract everything "
        "needed to decide whether to attend/apply and to not miss a deadline. "
        "Produce STRICT JSON with keys:\n"
        '{\n'
        '  "title": "event/opportunity name",\n'
        '  "summary": "2-4 sentence factual summary",\n'
        '  "event_type": "conference|hackathon|grant|fellowship|webinar|cfp|other",\n'
        '  "organizer": "who runs it",\n'
        '  "location": "city/venue or Online",\n'
        '  "event_date": "ISO date or range if the event happens, else null",\n'
        '  "application_deadline": "ISO 8601 datetime of the deadline, or null",\n'
        '  "deadline_raw": "the exact deadline text as it appears in the source",\n'
        '  "eligibility": ["who can apply / criteria"],\n'
        '  "required_materials": ["what must be submitted (CV, abstract, demo…)"],\n'
        '  "application_steps": ["ordered steps to apply / register"],\n'
        '  "application_url": "direct link to apply/register or null",\n'
        '  "cost": "fee / free / prize info if stated",\n'
        '  "catch_points": ["why this matters"],\n'
        '  "vivax_relevance": "why the company should care and how to use it",\n'
        '  "should_apply": "yes|maybe|no + one-line reason",\n'
        '  "confidence": 0-1\n'
        '}\n'
        "Dates MUST come verbatim from the source. If a date is relative "
        '("next Friday") and you cannot resolve it with certainty, put the '
        "raw text in deadline_raw and null in application_deadline. Never "
        "guess a year, time or timezone that is not stated."
    )
    user = "SOURCE:\n" + content.as_source_block()
    if content.luma_urls:
        user += "\n\nLuma links present: " + ", ".join(content.luma_urls)
    result = llm.json(system, user, images=_images(content), max_tokens=2200)
    result.setdefault("title", "Untitled event")
    return result


# ---------------------------------------------------------------------------
# Verifier — independent second pass to catch hallucinations.
# ---------------------------------------------------------------------------

def verify(llm: LLM, content: EnrichedContent, analysis: dict[str, Any],
           kind: str) -> dict[str, Any]:
    system = (
        "You are a strict fact-checker. Compare the ANALYSIS against the "
        "SOURCE. For every concrete claim (dates, deadlines, numbers, names, "
        "eligibility, URLs, monetary amounts) decide if it is DIRECTLY "
        "supported by the SOURCE. Be adversarial: assume the analysis may "
        "have hallucinated.\n"
        "Return STRICT JSON:\n"
        '{\n'
        '  "verified": true|false,   // false if ANY critical claim is unsupported\n'
        '  "issues": [               // one entry per unsupported/If wrong claim\n'
        '     {"field": "which key", "claim": "what was said", '
        '"problem": "not in source / contradicts source / ambiguous"}\n'
        '  ],\n'
        '  "corrected": { ...only fields that should be changed, with '
        'source-supported values or null... },\n'
        '  "deadline_confidence": "high|medium|low",\n'
        '  "notes": "short reviewer note"\n'
        '}'
    )
    user = (
        "SOURCE:\n" + content.as_source_block()
        + "\n\nANALYSIS (" + kind + "):\n"
        + json.dumps(analysis, ensure_ascii=False, indent=2)
    )
    result = llm.json(system, user, images=_images(content),
                      verify=True, max_tokens=1600)
    result.setdefault("verified", False)
    result.setdefault("issues", [])
    result.setdefault("corrected", {})
    return result


def apply_corrections(analysis: dict[str, Any], verification: dict[str, Any]
                      ) -> dict[str, Any]:
    """Merge verifier corrections into the analysis and attach a verdict."""
    merged = dict(analysis)
    corrected = verification.get("corrected") or {}
    if isinstance(corrected, dict):
        for k, v in corrected.items():
            merged[k] = v
    merged["_verified"] = bool(verification.get("verified"))
    merged["_verification_issues"] = verification.get("issues", [])
    merged["_deadline_confidence"] = verification.get("deadline_confidence", "unknown")
    return merged
