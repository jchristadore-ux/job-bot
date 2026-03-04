#!/usr/bin/env python3
"""
Automated Job Matching + Resume Tailoring Bot
Runs daily, fetches remote jobs, scores them, generates tailored docs, sends email.
"""

import os
import sys
import json
import re
import zipfile
import smtplib
import logging
import traceback
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from io import BytesIO

import requests
import yaml
from bs4 import BeautifulSoup
from docx import Document
from docx.shared import Pt
import feedparser

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
OUTPUT = ROOT / "output"
OUTPUT.mkdir(exist_ok=True)
TEMPLATES = ROOT / "templates"
BULLET_BANK_PATH = ROOT / "bullet_bank.json"
CONFIG_PATH = ROOT / "config.yaml"

MAX_JOB_TEXT = 20_000   # chars
TOP_N = 5
BULLET_COUNT = 10


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG + DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_bullet_bank() -> dict:
    with open(BULLET_BANK_PATH) as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════════════
# JOB FETCHING
# ══════════════════════════════════════════════════════════════════════════════

HEADERS = {"User-Agent": "JobBot/1.0 (automated job matching; contact via GitHub)"}


def fetch_remotive(cfg: dict) -> list[dict]:
    """Fetch from Remotive public API."""
    jobs = []
    try:
        url = "https://remotive.com/api/remote-jobs"
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        for j in data.get("jobs", []):
            jobs.append({
                "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "url": j.get("url", ""),
                "description": j.get("description", "")[:MAX_JOB_TEXT],
                "salary_raw": j.get("salary", ""),
                "source": "Remotive",
            })
        log.info(f"Remotive: fetched {len(jobs)} jobs")
    except Exception as e:
        log.warning(f"Remotive fetch failed: {e}")
    return jobs


def fetch_weworkremotely() -> list[dict]:
    """Fetch from We Work Remotely RSS feed."""
    jobs = []
    try:
        feed = feedparser.parse("https://weworkremotely.com/remote-jobs.rss")
        for entry in feed.entries:
            desc_html = entry.get("summary", "") or entry.get("content", [{}])[0].get("value", "")
            desc_text = BeautifulSoup(desc_html, "lxml").get_text(" ", strip=True)[:MAX_JOB_TEXT]
            jobs.append({
                "title": entry.get("title", ""),
                "company": entry.get("author", "") or _extract_company_from_title(entry.get("title", "")),
                "url": entry.get("link", ""),
                "description": desc_text,
                "salary_raw": "",
                "source": "WeWorkRemotely",
            })
        log.info(f"WeWorkRemotely: fetched {len(jobs)} jobs")
    except Exception as e:
        log.warning(f"WeWorkRemotely fetch failed: {e}")
    return jobs


def fetch_remoteok() -> list[dict]:
    """Fetch from RemoteOK public API."""
    jobs = []
    try:
        r = requests.get("https://remoteok.io/api", headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        # First element is a legal notice object
        for j in data[1:] if isinstance(data, list) else []:
            if not isinstance(j, dict):
                continue
            desc_html = j.get("description", "")
            desc_text = BeautifulSoup(desc_html, "lxml").get_text(" ", strip=True)[:MAX_JOB_TEXT] if desc_html else ""
            jobs.append({
                "title": j.get("position", ""),
                "company": j.get("company", ""),
                "url": j.get("url", "") or f"https://remoteok.io/remote-jobs/{j.get('id', '')}",
                "description": desc_text,
                "salary_raw": f"{j.get('salary_min', '')} - {j.get('salary_max', '')}",
                "source": "RemoteOK",
            })
        log.info(f"RemoteOK: fetched {len(jobs)} jobs")
    except Exception as e:
        log.warning(f"RemoteOK fetch failed: {e}")
    return jobs


def _extract_company_from_title(title: str) -> str:
    """WWR titles often formatted as 'Company: Role'."""
    if ":" in title:
        return title.split(":")[0].strip()
    return ""


def dedupe_jobs(jobs: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for j in jobs:
        key = j["url"].strip().rstrip("/")
        if key and key not in seen:
            seen.add(key)
            out.append(j)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# FILTERING
# ══════════════════════════════════════════════════════════════════════════════

def extract_salary(text: str, salary_raw: str) -> int | None:
    """
    Try to extract max annual salary (USD) from salary_raw or description.
    Returns None if no salary info found.
    """
    combined = f"{salary_raw} {text[:3000]}"
    # Remove commas in numbers
    combined = combined.replace(",", "")
    # Patterns like $180000 or $180k or 180,000
    patterns = [
        r"\$\s*(\d{3,6})k",          # $180k
        r"\$\s*(\d{6})",              # $180000
        r"(\d{3})[\s,]*000\s*(?:USD|usd|/yr|/year|annually)?",  # 180 000
        r"USD\s*(\d{3,6})k",
    ]
    values = []
    for pat in patterns:
        for m in re.finditer(pat, combined, re.IGNORECASE):
            try:
                val = int(m.group(1))
                if pat.endswith("k"):
                    val *= 1000
                if 50_000 <= val <= 1_000_000:
                    values.append(val)
            except Exception:
                pass
    return max(values) if values else None


def has_bonus_or_equity(text: str) -> bool:
    signals = ["bonus", "equity", "stock", "rsu", "options", "401k", "401(k)", "profit sharing"]
    lower = text.lower()
    return any(s in lower for s in signals)


def is_fully_remote(text: str, title: str) -> bool:
    lower = (text + " " + title).lower()
    # Reject if clearly on-site or hybrid
    reject = ["on-site", "onsite", "in-office", "hybrid", "in office", "must be located in", "relocation required"]
    for r in reject:
        if r in lower:
            return False
    remote_signals = ["fully remote", "100% remote", "remote-first", "work from anywhere",
                      "work from home", "wfh", "remote only", "remote position", "remote role"]
    for s in remote_signals:
        if s in lower:
            return True
    # Fallback: source guarantees remote
    return True  # all three sources are remote-focused


def is_fulltime(text: str) -> bool:
    lower = text.lower()
    if any(x in lower for x in ["contract", "freelance", "part-time", "part time", "temporary"]):
        return False
    return True


def passes_filters(job: dict, cfg: dict) -> tuple[bool, str]:
    """Returns (passes, reason_if_rejected)."""
    title = job["title"]
    desc = job["description"]
    salary_raw = job["salary_raw"]
    combined_text = f"{title} {desc}"

    if not is_fulltime(combined_text):
        return False, "not full-time"

    if not is_fully_remote(desc, title):
        return False, "not fully remote"

    salary = extract_salary(desc, salary_raw)
    min_salary = cfg.get("filters", {}).get("min_salary", 165_000)
    if salary is not None and salary < min_salary:
        return False, f"salary {salary} < {min_salary}"

    if not has_bonus_or_equity(combined_text):
        return False, "no bonus/equity mention"

    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# SCORING
# ══════════════════════════════════════════════════════════════════════════════

def tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9\+\#]+", text.lower())
    return set(words)


def score_job(job: dict, bullet_bank: dict, cfg: dict) -> float:
    """Deterministic keyword overlap scoring."""
    title = job["title"].lower()
    desc = job["description"].lower()
    combined = f"{title} {desc}"
    tokens = tokenize(combined)

    score = 0.0

    # 1. Keywords priority overlap
    kw_priority = bullet_bank.get("keywords_priority", [])
    kw_hits = sum(1 for kw in kw_priority if kw.lower() in combined)
    score += kw_hits * 2.0

    # 2. Title signals
    title_signals = cfg.get("scoring", {}).get("title_signals", [
        "tpm", "program manager", "technical program", "infrastructure",
        "data center", "senior manager", "director", "vp", "principal",
        "engineering manager", "platform", "cloud", "devops", "sre"
    ])
    title_hits = sum(1 for t in title_signals if t.lower() in title)
    score += title_hits * 3.0

    # 3. Comp signals
    salary = extract_salary(desc, job["salary_raw"])
    min_salary = cfg.get("filters", {}).get("min_salary", 165_000)
    if salary and salary >= min_salary:
        score += 5.0
    if has_bonus_or_equity(combined):
        score += 2.0

    # 4. Bullet bank keyword overlap
    all_bullets = bullet_bank.get("bullets", [])
    bullet_overlap = 0
    for b in all_bullets:
        bullet_kws = b.get("keywords", [])
        hits = sum(1 for k in bullet_kws if k.lower() in combined)
        bullet_overlap += hits
    score += bullet_overlap * 0.5

    return round(score, 2)


def select_bullets(job: dict, bullet_bank: dict, n: int = BULLET_COUNT) -> list[str]:
    """Select top-N bullets by keyword overlap with job posting."""
    desc = job["description"].lower()
    all_bullets = bullet_bank.get("bullets", [])

    scored = []
    for b in all_bullets:
        kws = b.get("keywords", [])
        hits = sum(1 for k in kws if k.lower() in desc)
        scored.append((hits, b.get("text", "")))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [text for _, text in scored[:n]]


# ══════════════════════════════════════════════════════════════════════════════
# DOCX GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def load_master_resume_info() -> dict:
    """Extract basic info from master_resume.docx if present, else use defaults."""
    defaults = {
        "name": "YOUR NAME",
        "email": "your@email.com",
        "phone": "555-555-5555",
        "location": "Remote, USA",
        "linkedin": "linkedin.com/in/yourprofile",
    }
    master = ROOT / "master_resume.docx"
    if not master.exists():
        log.warning("master_resume.docx not found — using placeholder defaults")
        return defaults
    try:
        doc = Document(master)
        # Attempt to pull name from first non-empty paragraph
        for para in doc.paragraphs:
            if para.text.strip():
                defaults["name"] = para.text.strip()
                break
    except Exception as e:
        log.warning(f"Could not read master_resume.docx: {e}")
    return defaults


def fill_template(template_path: Path, replacements: dict) -> Document:
    """Fill {{PLACEHOLDER}} tokens in a DOCX template."""
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    doc = Document(template_path)

    def replace_in_runs(paragraph, replacements):
        for run in paragraph.runs:
            for key, value in replacements.items():
                token = "{{" + key + "}}"
                if token in run.text:
                    run.text = run.text.replace(token, str(value))
        # Also handle cases where token is split across the full paragraph text
        full = paragraph.text
        for key, value in replacements.items():
            token = "{{" + key + "}}"
            if token in full:
                # Rebuild: clear runs, set first run
                for run in paragraph.runs:
                    run.text = ""
                if paragraph.runs:
                    paragraph.runs[0].text = full.replace(token, str(value))
                break

    for para in doc.paragraphs:
        replace_in_runs(para, replacements)

    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    replace_in_runs(para, replacements)

    return doc


def build_cover_body(job: dict, matched_keywords: list[str], person: dict) -> str:
    today = date.today().strftime("%B %d, %Y")
    kw_str = ", ".join(matched_keywords[:6]) if matched_keywords else "your key requirements"
    body = (
        f"I am writing to express my strong interest in the {job['title']} position at {job['company']}. "
        f"With a proven track record in technical program management, infrastructure, and large-scale systems delivery, "
        f"I am confident in my ability to drive meaningful results for your organization.\n\n"
        f"My experience aligns directly with your needs in areas such as {kw_str}. "
        f"I have consistently led cross-functional teams, managed complex stakeholder environments, "
        f"and delivered high-impact programs on time and within budget at organizations ranging from "
        f"high-growth startups to Fortune 500 enterprises.\n\n"
        f"I thrive in fully remote, distributed environments and bring a strong bias for action, "
        f"structured thinking, and executive presence. I would welcome the opportunity to discuss how "
        f"my background can contribute to {job['company']}'s continued success.\n\n"
        f"Thank you for your consideration. I look forward to speaking with you."
    )
    return body


def generate_resume(job: dict, bullets: list[str], person: dict, idx: int) -> Path:
    template = TEMPLATES / "resume_template.docx"
    replacements = {
        "NAME": person["name"],
        "EMAIL": person.get("email", ""),
        "PHONE": person.get("phone", ""),
        "LOCATION": person.get("location", "Remote"),
        "LINKEDIN": person.get("linkedin", ""),
        "TITLE": job["title"],
        "COMPANY": job["company"],
        "DATE": date.today().strftime("%B %Y"),
    }
    for i, bullet in enumerate(bullets, 1):
        replacements[f"BULLET_{i}"] = bullet
    # Fill any unused bullet slots
    for i in range(len(bullets) + 1, BULLET_COUNT + 1):
        replacements[f"BULLET_{i}"] = ""

    doc = fill_template(template, replacements)
    safe_company = re.sub(r"[^\w]", "_", job["company"])[:30]
    filename = OUTPUT / f"resume_{idx:02d}_{safe_company}.docx"
    doc.save(filename)
    return filename


def generate_cover_letter(job: dict, bullets: list[str], person: dict, idx: int) -> Path:
    template = TEMPLATES / "cover_letter_template.docx"
    bullet_bank = load_bullet_bank()
    matched_kws = []
    for b in bullets:
        pass  # bullets are already text; extract nouns as matched keywords
    # Simple: pull keywords that appear in job description
    kw_priority = bullet_bank.get("keywords_priority", [])
    desc_lower = job["description"].lower()
    matched_kws = [k for k in kw_priority if k.lower() in desc_lower][:8]

    cover_body = build_cover_body(job, matched_kws, person)
    replacements = {
        "NAME": person["name"],
        "DATE": date.today().strftime("%B %d, %Y"),
        "COMPANY": job["company"],
        "ROLE": job["title"],
        "COVER_BODY": cover_body,
        "EMAIL": person.get("email", ""),
        "PHONE": person.get("phone", ""),
        "LINKEDIN": person.get("linkedin", ""),
    }
    doc = fill_template(template, replacements)
    safe_company = re.sub(r"[^\w]", "_", job["company"])[:30]
    filename = OUTPUT / f"cover_letter_{idx:02d}_{safe_company}.docx"
    doc.save(filename)
    return filename


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def write_summary(ranked: list[dict]) -> Path:
    lines = [
        f"Job Match Summary — {date.today().strftime('%A, %B %d, %Y')}",
        "=" * 60,
        "",
    ]
    for i, job in enumerate(ranked, 1):
        salary = extract_salary(job["description"], job["salary_raw"])
        sal_str = f"${salary:,}" if salary else "Not stated"
        lines += [
            f"#{i}  {job['title']} @ {job['company']}",
            f"    Score: {job['_score']}  |  Salary: {sal_str}  |  Source: {job['source']}",
            f"    URL: {job['url']}",
            f"    Why: {job.get('_why', 'Strong keyword overlap with target profile')}",
            "",
        ]
    path = OUTPUT / "summary.txt"
    path.write_text("\n".join(lines))
    return path


def build_why(job: dict, bullet_bank: dict, cfg: dict) -> str:
    """One-line reason for match."""
    title = job["title"]
    desc = job["description"].lower()
    kw_priority = bullet_bank.get("keywords_priority", [])
    hits = [k for k in kw_priority if k.lower() in desc][:5]
    salary = extract_salary(desc, job["salary_raw"])
    parts = []
    if hits:
        parts.append(f"keyword hits: {', '.join(hits)}")
    if salary:
        parts.append(f"salary ${salary:,}")
    if has_bonus_or_equity(desc):
        parts.append("mentions bonus/equity")
    return "; ".join(parts) if parts else "general remote match"


# ══════════════════════════════════════════════════════════════════════════════
# ZIP
# ══════════════════════════════════════════════════════════════════════════════

def create_zip(files: list[Path], summary: Path) -> Path:
    zip_path = OUTPUT / "tailored_packets.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(summary, summary.name)
        for f in files:
            if f.exists():
                zf.write(f, f.name)
    log.info(f"ZIP created: {zip_path}")
    return zip_path


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL
# ══════════════════════════════════════════════════════════════════════════════

def send_email(summary_path: Path, attachments: list[Path], zip_path: Path, ranked: list[dict]):
    smtp_user = os.environ.get("GMAIL_SMTP_USER", "")
    smtp_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    email_to = os.environ.get("EMAIL_TO", smtp_user)

    if not smtp_user or not smtp_pass:
        log.warning("Email credentials not set. Skipping email send.")
        return False

    # Build body
    body_lines = [
        f"Good morning! Here are your Top {TOP_N} job matches for {date.today().strftime('%B %d, %Y')}.",
        "",
    ]
    for i, job in enumerate(ranked, 1):
        salary = extract_salary(job["description"], job["salary_raw"])
        sal_str = f"${salary:,}" if salary else "Not stated"
        body_lines += [
            f"#{i}. {job['title']} @ {job['company']}",
            f"   Score: {job['_score']} | Salary: {sal_str} | Source: {job['source']}",
            f"   {job['url']}",
            f"   Why: {job.get('_why', '')}",
            "",
        ]
    body_lines += [
        "Attachments: tailored resumes + cover letters + ZIP of everything.",
        "",
        "Good luck! 🚀",
    ]
    body = "\n".join(body_lines)

    msg = EmailMessage()
    msg["Subject"] = f"🎯 Top {TOP_N} Job Matches — {date.today().strftime('%b %d, %Y')}"
    msg["From"] = smtp_user
    msg["To"] = email_to
    msg.set_content(body)

    # Attach all files
    all_attachments = list(attachments) + [zip_path, summary_path]
    for fpath in all_attachments:
        if fpath and fpath.exists():
            with open(fpath, "rb") as f:
                data = f.read()
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if fpath.suffix == ".zip":
                mime = "application/zip"
            elif fpath.suffix == ".txt":
                mime = "text/plain"
            msg.add_attachment(data, maintype=mime.split("/")[0],
                               subtype=mime.split("/")[1], filename=fpath.name)

    # Try TLS 587, fallback SSL 465
    sent = False
    for port, use_ssl in [(587, False), (465, True)]:
        try:
            if use_ssl:
                server = smtplib.SMTP_SSL("smtp.gmail.com", port, timeout=30)
            else:
                server = smtplib.SMTP("smtp.gmail.com", port, timeout=30)
                server.ehlo()
                server.starttls()
                server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
            server.quit()
            log.info(f"Email sent via port {port} to {email_to}")
            sent = True
            break
        except Exception as e:
            log.warning(f"Email attempt (port {port}) failed: {e}")

    if not sent:
        log.error("All email send attempts failed. Artifacts will still be uploaded.")
    return sent


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    log.info("=== Job Bot Starting ===")
    cfg = load_config()
    bullet_bank = load_bullet_bank()
    person = load_master_resume_info()

    # 1. Fetch jobs from all sources
    log.info("Fetching jobs...")
    jobs = []
    jobs += fetch_remotive(cfg)
    jobs += fetch_weworkremotely()
    jobs += fetch_remoteok()
    log.info(f"Total fetched: {len(jobs)} jobs")

    # 2. Deduplicate
    jobs = dedupe_jobs(jobs)
    log.info(f"After dedup: {len(jobs)} jobs")

    # 3. Filter
    passing = []
    for job in jobs:
        ok, reason = passes_filters(job, cfg)
        if ok:
            passing.append(job)
        # else: log.debug(f"REJECT [{job['title']}@{job['company']}]: {reason}")
    log.info(f"After filters: {len(passing)} jobs pass")

    if not passing:
        log.warning("No jobs passed filters. Consider loosening config.yaml filters.")
        # Write empty summary so workflow doesn't fail
        summary_path = OUTPUT / "summary.txt"
        summary_path.write_text(
            f"No jobs matched filters on {date.today()}.\n"
            "Consider loosening min_salary or filter criteria in config.yaml."
        )
        zip_path = create_zip([], summary_path)
        log.info("Uploading empty artifacts. Exiting cleanly.")
        return

    # 4. Score + rank
    for job in passing:
        job["_score"] = score_job(job, bullet_bank, cfg)
        job["_why"] = build_why(job, bullet_bank, cfg)

    ranked = sorted(passing, key=lambda j: j["_score"], reverse=True)[:TOP_N]
    log.info(f"Top {len(ranked)} matches selected")
    for i, j in enumerate(ranked, 1):
        log.info(f"  #{i} {j['title']} @ {j['company']} — score {j['_score']}")

    # 5. Generate docs
    resume_files = []
    cover_files = []
    for idx, job in enumerate(ranked, 1):
        bullets = select_bullets(job, bullet_bank)
        try:
            rf = generate_resume(job, bullets, person, idx)
            resume_files.append(rf)
            log.info(f"  Resume generated: {rf.name}")
        except Exception as e:
            log.error(f"Resume generation failed for job {idx}: {e}\n{traceback.format_exc()}")

        try:
            cf = generate_cover_letter(job, bullets, person, idx)
            cover_files.append(cf)
            log.info(f"  Cover letter generated: {cf.name}")
        except Exception as e:
            log.error(f"Cover letter generation failed for job {idx}: {e}\n{traceback.format_exc()}")

    # 6. Write summary
    summary_path = write_summary(ranked)
    log.info(f"Summary written: {summary_path}")

    # 7. Create ZIP
    all_docs = resume_files + cover_files
    zip_path = create_zip(all_docs, summary_path)

    # 8. Send email (failure is non-fatal)
    try:
        send_email(summary_path, all_docs, zip_path, ranked)
    except Exception as e:
        log.error(f"Email send raised unexpected exception: {e}")

    log.info("=== Job Bot Complete ===")


if __name__ == "__main__":
    main()
