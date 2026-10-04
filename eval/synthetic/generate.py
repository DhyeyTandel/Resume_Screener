"""Deterministic synthetic corpus for Module B (SYNTHETIC-ONLY, Spec 16.1).

Every candidate is a fully invented person: a resume text plus a synthetic world (GitHub repos with
languages, topics, manifests, trees, READMEs and commit authorship; an optional LinkedIn structured
export; an optional portfolio site). Ground truth is known by construction: the generator decides
what the person did (which repos use which skills, which roles are on LinkedIn, which claims were
injected or inflated) and derives each claim's label from those decisions, never from the system
under test.

Claim label rules (see label_claims):
  VERIFIED      SKILL used by an authored, non-fork repo; PROJECT with its own authored repo;
                METRIC whose exact figure is stated in a repo README (the artifact trace)
  CORROBORATED  ROLE that the LinkedIn export lists with the same employer, title and dates
  WEAK          SKILL only in a low-authorship repo, or only on the candidate's own portfolio
  UNSUPPORTED   a checkable source (a GitHub account with public repos, or a LinkedIn export) exists
                and holds no evidence for the claim
  CONTRADICTED  a source conflicts: a LinkedIn date/title conflict, or a METRIC whose README figure
                differs from the resume figure
  UNVERIFIABLE  no source that could adjudicate the claim was provided
  None          genuinely ambiguous by construction (fork/tutorial project claimed as own); such claims
                are excluded from claim-status scoring

Run:  .venv/bin/python eval/synthetic/generate.py            (writes eval/datasets/synthetic/)
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))

import perturb  # noqa: E402

SEED = 20261004
REF_YEAR = 2026
KIND_WEIGHTS = {"genuine": 0.40, "P1": 0.1333, "P2": 0.10, "P4": 0.1333, "P5": 0.10, "P6": 0.1333}
TRAIN_FRACTION = 0.30
OUT_DIR = ROOT / "eval" / "datasets" / "synthetic"

DISPLAY = {
    "python": "Python", "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
    "javascript": "JavaScript", "typescript": "TypeScript", "react": "React", "vue": "Vue",
    "angular": "Angular", "postgresql": "PostgreSQL", "mysql": "MySQL", "sqlite": "SQLite",
    "mongodb": "MongoDB", "kafka": "Kafka", "rabbitmq": "RabbitMQ", "docker": "Docker",
    "kubernetes": "Kubernetes", "aws": "AWS", "azure": "Azure", "gcp": "GCP", "ci/cd": "CI/CD",
    "pytest": "pytest", "java": "Java", "go": "Go",
}
ALIAS_TEXT = {
    "javascript": ["JS"], "typescript": ["TS"], "postgresql": ["Postgres"], "kubernetes": ["K8s"],
    "go": ["Golang"], "mongodb": ["Mongo"], "react": ["React.js"], "vue": ["Vue.js"],
    "aws": ["Amazon Web Services"], "gcp": ["Google Cloud"], "kafka": ["Apache Kafka"],
}
LANGS = {"python", "javascript", "typescript", "java", "go"}
FRONTEND = {"react", "vue", "angular"}
# "sql" and "rest apis" are not generated or injected: they are implied by other skills, so a label
# for them would be ambiguous by construction.
INJECTABLE = [s for s in DISPLAY if s not in ("pytest",)]
IMPLIED = {"javascript": {"typescript"}, "typescript": {"javascript"}}

FAMILIES = {
    "py_backend": dict(langs=["python"], fw=["fastapi", "django", "flask"],
                       db=["postgresql", "mysql", "mongodb", "sqlite"], mq=["kafka", "rabbitmq"],
                       infra=["docker", "kubernetes", "ci/cd"], cloud=["aws", "gcp", "azure"], test=["pytest"],
                       titles=["Backend Engineer", "Software Engineer", "Platform Engineer", "Data Engineer"], w=4),
    "js_web": dict(langs=["javascript", "typescript"], fw=["react", "vue", "angular"],
                   db=["postgresql", "mongodb", "mysql"], mq=[], infra=["docker", "ci/cd"],
                   cloud=["aws", "gcp", "azure"], test=[],
                   titles=["Frontend Engineer", "Full Stack Developer", "Web Developer", "Software Engineer"], w=3),
    "java_backend": dict(langs=["java"], fw=[], db=["mysql", "postgresql", "mongodb"], mq=["kafka", "rabbitmq"],
                         infra=["docker", "kubernetes", "ci/cd"], cloud=["aws", "azure", "gcp"], test=[],
                         titles=["Backend Developer", "Software Engineer", "Application Developer"], w=2),
    "go_backend": dict(langs=["go"], fw=[], db=["postgresql", "mongodb"], mq=["kafka"],
                       infra=["docker", "kubernetes", "ci/cd"], cloud=["aws", "gcp"], test=[],
                       titles=["Backend Engineer", "Platform Engineer", "Infrastructure Engineer"], w=1),
    "fullstack": dict(langs=["python", "javascript", "typescript"], fw=["django", "flask", "react", "vue"],
                      db=["postgresql", "mysql", "sqlite"], mq=[], infra=["docker", "ci/cd"],
                      cloud=["aws", "gcp"], test=["pytest"],
                      titles=["Full Stack Developer", "Software Engineer"], w=2),
}
FIRST = ["Priya", "Wei", "Aiko", "Mateo", "Fatima", "Kwame", "Olga", "Chen", "Sofia", "Ahmed", "Ingrid",
         "Rahul", "Ngozi", "Dmitri", "Yuki", "Carlos", "Amara", "Lars", "Mei", "Omar", "Anika", "Tomas",
         "Leila", "Hiroshi", "Zainab", "Pedro", "Noor", "Sven", "Ananya", "Kofi", "Elena", "Jun", "Marta",
         "Tariq", "Hana", "Lucas", "Imani", "Pavel", "Sana", "Diego", "Freya", "Arjun", "Nia", "Mikhail",
         "Camila", "Idris", "Katya", "Ravi", "Selin", "Joao"]
LAST = ["Raman", "Okafor", "Tanaka", "Silva", "Haddad", "Novak", "Kowalski", "Nguyen", "Petrov", "Mensah",
        "Larsen", "Gupta", "Alvarez", "Ito", "Rahman", "Mwangi", "Fischer", "Costa", "Khan", "Bianchi",
        "Moreau", "Sato", "Ivanova", "Adeyemi", "Cohen", "Lindqvist", "Ortiz", "Banerjee", "Yilmaz", "Park",
        "Duarte", "Hassan", "Volkov", "Mbeki", "Suzuki", "Kovacs", "Reyes", "Chaudhry", "Nilsson", "Abebe"]
CO_A = ["Northwind", "Corvid", "Bluefin", "Harbor", "Ironwood", "Lumen", "Meridian", "Quarry", "Redwood",
        "Summit", "Tidal", "Verdant", "Willow", "Zenith", "Atlas", "Beacon", "Cobalt", "Ember", "Fathom",
        "Granite", "Juniper", "Kestrel", "Lattice", "Monarch"]
CO_B = ["Payments", "Systems", "Labs", "Logistics", "Software", "Analytics", "Networks", "Health", "Commerce",
        "Robotics", "Media", "Energy", "Retail", "Mobility"]
INSTITUTIONS = ["Ravenna University", "Hollis Institute of Technology", "Marlow College", "Eastbrook University",
                "Kestrel Polytechnic", "Alder University", "Tamsin College"]
DEGREES = ["B.S. in Computer Science", "B.Tech in Information Technology", "B.E. in Computer Engineering",
           "B.Sc. in Software Engineering", "B.S. in Computer Science", "M.S. in Computer Science"]
DOMAINS = ["ledger", "billing", "inventory", "booking", "notification", "search", "analytics", "payment",
           "catalog", "scheduler", "auth", "reporting", "messaging", "shipping", "telemetry", "onboarding"]
KINDS = ["service", "api", "dashboard", "pipeline", "tool"]
SIDE_WORDS = ["playground", "experiments", "scripts", "notes", "learning", "sandbox"]
CERTS = ["AWS Certified Cloud Practitioner, 2022", "Certified Kubernetes Application Developer, 2023",
         "Professional Scrum Master I, 2021", "Azure Fundamentals, 2022"]
ACHIEVEMENTS = ["Won the internal hackathon, 2023", "Speaker at a regional developer meetup, 2022",
                "Employee of the quarter, 2024", "Published an open source guide on release automation, 2021"]
LI_TITLE_SYNONYMS = [("Engineer", "Developer"), ("Developer", "Engineer"), ("Senior ", "Sr. ")]

# (id, needs, text, kind, uses_percent)
BULLETS = [
    ("rps", {"main"}, "Built {main} services handling {n} requests per second, reducing p95 latency from {a}ms to {b}ms", "METRIC", False),
    ("schema", {"db"}, "Designed {db} schemas and rewrote the {dom} query, cutting run time by {p} percent", "METRIC", True),
    ("mq", {"mq"}, "Ran {mq} consumer groups for the {dom} event stream across {k} partitions", "ACHIEVEMENT", False),
    ("img", {"docker"}, "Owned {docker} images and the release pipeline for {k} services", "ACHIEVEMENT", False),
    ("suite", {"lang"}, "Wrote {lang} services and automated test suites for the {dom} platform", "ACHIEVEMENT", False),
    ("tune", {"db"}, "Tuned {db} queries, improving dashboard load time from {a} seconds to {b2} seconds", "METRIC", False),
    ("cloud", {"cloud"}, "Deployed the {dom} platform on {cloud}, serving {n2}k users", "METRIC", False),
    ("cicd", {"cicd"}, "Set up {cicd} workflows that cut release time by {p} percent", "METRIC", True),
    ("fwep", {"fw"}, "Developed {fw} endpoints for the {dom} workflow used by {k} internal teams", "ACHIEVEMENT", False),
    ("batch", {"lang"}, "Implemented {lang} batch jobs that process {n} records per hour", "ACHIEVEMENT", False),
    ("mig", {"infra"}, "Migrated {k} services to {infra}, cutting deploy time by {p} percent", "METRIC", True),
    ("fe", {"fe"}, "Built {fe} components for the {dom} dashboard, shipping {k} features per quarter", "ACHIEVEMENT", False),
    ("lead", set(), "Led onboarding for {k} engineers and documented the {dom} architecture", "ACHIEVEMENT", False),
]
VAGUE_BULLETS = [
    ("v1", {"lang"}, "Worked on {lang} services for the {dom} platform", "ACHIEVEMENT", False),
    ("v2", {"db"}, "Helped maintain {db} databases and support {k} releases", "ACHIEVEMENT", False),
    ("v3", {"infra"}, "Assisted with {infra} deployments across environments", "ACHIEVEMENT", False),
]
PROJECT_DESC = [
    "{Dom} {kind} built with {t1} and {t2}",
    "Designed and built a {dom} {kind} in {lang} using {t1}",
    "{Dom} {kind} in {lang} backed by {t1}",
]


def _iso(year: int, month: int, day: int) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}T00:00:00Z"


def _stable_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ profile
def gen_profile(rng: random.Random, kind: str) -> dict:
    fam_name = rng.choices(list(FAMILIES), weights=[FAMILIES[f]["w"] for f in FAMILIES])[0]
    fam = FAMILIES[fam_name]
    first, last = rng.choice(FIRST), rng.choice(LAST)
    grad = rng.randint(2012, 2024)
    style = {
        "writing": rng.choices(["plain", "terse", "verbose", "buzzy", "vague"], [4, 2, 2, 1.5, 1.5])[0],
        "role_fmt": rng.choices(["at", "comma", "to"], [0.70, 0.12, 0.18])[0],
        "marker": rng.choice(["- ", "• ", "* "]),
        "skills_heading": rng.choice(["Skills", "Technical Skills"]),
        "exp_heading": rng.choice(["Experience", "Work Experience"]),
        "skills_sep": rng.choice([", ", ", ", " | ", "; "]),
        "grouped_skills": rng.random() < 0.15,
        "project_fmt": rng.choices(["colon", "dash"], [0.7, 0.3])[0],
        "order": rng.choice([["summary", "skills", "experience", "projects", "education", "certs", "ach"],
                             ["summary", "experience", "skills", "projects", "education", "certs", "ach"],
                             ["summary", "experience", "projects", "skills", "education", "certs", "ach"],
                             ["summary", "skills", "experience", "education", "projects", "certs", "ach"]]),
        "role_order": rng.choice(["latest_first", "latest_first", "oldest_first"]),
    }
    # skills
    langs = rng.sample(fam["langs"], min(len(fam["langs"]), 2 if fam_name == "fullstack" or rng.random() < 0.3 else 1))
    skills = list(langs)
    for key, lo, hi in (("fw", 1, 2), ("db", 1, 2), ("mq", 0, 1), ("infra", 1, 2), ("cloud", 0, 1), ("test", 0, 1)):
        pool = fam[key]
        if pool:
            skills += rng.sample(pool, min(len(pool), rng.randint(lo, hi)))
    skills = list(dict.fromkeys(skills))
    rng.shuffle(skills)
    stext = {s: (rng.choice(ALIAS_TEXT[s]) if s in ALIAS_TEXT and rng.random() < 0.2 else DISPLAY[s]) for s in skills}
    # roles
    n = rng.choices([1, 2, 3], [0.25, 0.45, 0.30])[0]
    cursor = grad + rng.choice([0, 0, 1])
    if cursor >= 2025:
        n = 1
    t0 = rng.choice(fam["titles"])
    ladder = [t0, "Senior " + t0, rng.choice(["Lead ", "Staff "]) + t0]
    if rng.random() < 0.3:
        ladder[1] = rng.choice([t for t in fam["titles"] if t != t0] or [t0 + " II"])
    roles: list[dict] = []
    companies = rng.sample([f"{a} {b}" for a in CO_A for b in CO_B], 4)
    for i in range(n):
        start = cursor
        last_role = i == n - 1
        if not last_role:
            end = start + rng.randint(1, 3)
            if end >= REF_YEAR:
                last_role, end = True, "Present"
        else:
            end = "Present" if (rng.random() < 0.8 and start <= REF_YEAR - 1) else min(REF_YEAR, start + rng.randint(1, 4))
            if end == start:
                end = "Present"
        roles.append({"title": ladder[i], "company": companies[i], "start": start, "end": end, "intern": False})
        if last_role:
            break
        cursor = end
    if rng.random() < 0.15 and roles[0]["start"] >= grad:
        roles.insert(0, {"title": "Software Engineering Intern", "company": companies[3], "start": grad - 1,
                         "end": grad, "intern": True})
    # LinkedIn view of each role (truth unless a perturbation changes it)
    for r in roles:
        r["li_title"] = r["title"]
        r["li_company"] = r["company"]
        r["li_start"], r["li_end"] = r["start"], r["end"]
        r["in_li"], r["conflict"] = True, None
        if rng.random() < 0.12:
            for a, b in LI_TITLE_SYNONYMS:
                # Whole words only: a plain replace turned "Software Engineering Intern" into
                # the non-word "Software Developering Intern".
                rx = re.compile(rf"\b{re.escape(a.strip())}\b")
                if rx.search(r["title"]):
                    r["li_title"] = rx.sub(b.strip(), r["title"], count=1)
                    break
        if rng.random() < 0.15:
            r["li_company"] = r["company"] + " Inc."
    if len(roles) >= 2 and rng.random() < 0.10:
        roles[rng.randrange(len(roles) - 1)]["in_li"] = False
    first_real = next((r for r in roles if not r["intern"]), roles[0])
    total_years = REF_YEAR - first_real["start"]
    prof = {
        "name": f"{first} {last}", "first": first, "last": last,
        "email": f"{first.lower()}.{last.lower()}@example.com", "phone": f"+1 555 {rng.randint(100, 999):04d}"[:11],
        "family": fam_name, "grad": grad, "degree": f"{rng.choice(DEGREES)}, {rng.choice(INSTITUTIONS)}, {grad}",
        "skills": skills, "stext": stext, "injected": [], "roles": roles, "style": style,
        "certs": [rng.choice(CERTS)] if rng.random() < 0.25 else [],
        "ach": [rng.choice(ACHIEVEMENTS)] if rng.random() < 0.20 else [],
        "title": t0, "years": max(1, total_years), "has_summary": style["writing"] != "terse" and rng.random() < 0.8,
    }
    prof["domains"] = rng.sample(DOMAINS, 4)
    return prof


# ------------------------------------------------------------------ bullets / projects
def _ctx(rng: random.Random, prof: dict) -> dict:
    s = set(prof["skills"])
    fam = FAMILIES[prof["family"]]
    ctx: dict[str, list[str]] = {
        "lang": [x for x in prof["skills"] if x in LANGS],
        "fw": [x for x in prof["skills"] if x in fam["fw"] and x not in FRONTEND],
        "fe": [x for x in prof["skills"] if x in FRONTEND],
        "db": [x for x in prof["skills"] if x in ("postgresql", "mysql", "mongodb", "sqlite")],
        "mq": [x for x in prof["skills"] if x in ("kafka", "rabbitmq")],
        "docker": ["docker"] if "docker" in s else [],
        "infra": [x for x in prof["skills"] if x in ("docker", "kubernetes")],
        "cicd": ["ci/cd"] if "ci/cd" in s else [],
        "cloud": [x for x in prof["skills"] if x in ("aws", "gcp", "azure")],
    }
    ctx["main"] = ctx["fw"] or ctx["fe"] or ctx["lang"]
    return ctx


def gen_bullets(rng: random.Random, prof: dict, count: int, *, allow_pct: bool, max_pct: int | None = None) -> list[dict]:
    ctx = _ctx(rng, prof)
    pool = list(BULLETS)
    if prof["style"]["writing"] == "vague":
        pool = VAGUE_BULLETS * 2 + pool
    elif rng.random() < 0.15:
        pool = VAGUE_BULLETS + pool
    rng.shuffle(pool)
    out: list[dict] = []
    used: set[str] = set()
    pct_used = 0
    for bid, needs, text, kind, uses_pct in pool:
        if len(out) >= count:
            break
        if bid in used or any(not ctx.get(nd) for nd in needs):
            continue
        if uses_pct and (not allow_pct or (max_pct is not None and pct_used >= max_pct)):
            continue
        slots = {k: DISPLAY[rng.choice(ctx[k])] for k in needs if k not in ("main",)}
        slots["main"] = DISPLAY[rng.choice(ctx["main"])] if ctx.get("main") else ""
        a = rng.randint(220, 640)
        p = rng.randint(12, 35)
        vals = dict(slots, dom=rng.choice(prof["domains"]), n=rng.choice([800, 1200, 2500, 4000, 9000]),
                    k=rng.randint(3, 14), p=p, a=a, b=int(a * rng.uniform(0.3, 0.6)),
                    b2=rng.randint(2, 5), n2=rng.randint(5, 60))
        # "from {a} seconds" must be larger than "{b2} seconds"
        if bid == "tune":
            vals["a"] = rng.randint(7, 20)
        t = text.format(**vals)
        techs = sorted({s for s, d in DISPLAY.items() if re.search(rf"(?<![\w/]){re.escape(d)}(?![\w/])", t)})
        # {main} may be the literal name of a language/framework; techs come from the final text.
        item = {"id": bid, "text": t, "kind": kind, "techs": techs, "pct": p if uses_pct else None}
        used.add(bid)
        pct_used += 1 if uses_pct else 0
        out.append(item)
    w = prof["style"]["writing"]
    for item in out:
        if w == "buzzy":
            item["text"] = re.sub(r"^(Built|Designed|Owned|Developed|Implemented|Set up|Migrated)", "Spearheaded", item["text"])
            item["text"] = item["text"].replace(" services", " robust services", 1)
        elif w == "verbose":
            item["text"] += " as part of a cross-functional team"
    return out


def gen_projects(rng: random.Random, prof: dict, n_private_max: int = 1) -> list[dict]:
    s = prof["skills"]
    langs = [x for x in s if x in LANGS]
    projects = []
    doms = rng.sample(DOMAINS, 3)
    kinds = [rng.choice(KINDS) for _ in range(3)]
    n = rng.choices([1, 2, 3], [0.3, 0.45, 0.25])[0]
    seen_repo = set()
    for i in range(n):
        dom, kind = doms[i], kinds[i]
        repo = f"{dom}-{kind}"
        if repo in seen_repo:
            continue
        seen_repo.add(repo)
        lang = rng.choice(langs)
        others = [x for x in s if x != lang and x not in LANGS]
        uses = [lang] + rng.sample(others, min(len(others), rng.randint(2, 3)))
        nl = [x for x in uses if x != lang]
        t = nl[:2]
        tmpl = rng.choice(PROJECT_DESC) if len(t) >= 1 else "{Dom} {kind} written in {lang}"
        if len(t) < 2 and "{t2}" in tmpl:
            tmpl = PROJECT_DESC[1]
        desc = tmpl.format(Dom=dom.capitalize(), dom=dom, kind=kind, lang=DISPLAY[lang],
                           t1=DISPLAY[t[0]] if t else "", t2=DISPLAY[t[1]] if len(t) > 1 else "")
        projects.append({"name": f"{dom.capitalize()} {kind.capitalize()}", "repo": repo, "dom": dom, "kind": kind,
                         "uses": uses, "desc": desc, "private": False, "fork": False, "tutorial": False})
    private_budget = n_private_max if len(projects) > 1 else 0
    for p in projects[1:]:
        if private_budget and rng.random() < 0.25:
            p["private"], p["repo"] = True, None
            private_budget -= 1
    return projects


# ------------------------------------------------------------------ world
def _readme_text(rng: random.Random, name: str, desc: str, techs: list[str], tutorial_text: bool, extra: str = "") -> str:
    body = f"# {name}\n\n{desc}.\n\nFeatures: structured logging, retry handling and a test suite.\n"
    if techs:
        body += "\nBuilt with " + ", ".join(techs) + ".\n"
    if tutorial_text:
        body += "\nSee the tutorial section below for local setup.\n"
    return body + extra


# Real package names a repo declares when it genuinely uses the technology. Generated repos
# list them in manifest contents, as real repos do; a technology that appears only in a topic
# label or README prose is the owner describing their work (WEAK under Spec 11 Stage 3).
PY_DEPS = {"fastapi": "fastapi", "django": "django", "flask": "flask", "pytest": "pytest",
           "kafka": "confluent-kafka", "rabbitmq": "pika", "mongodb": "pymongo",
           "postgresql": "psycopg2-binary"}
JS_DEPS = {"react": "react", "vue": "vue", "angular": "@angular/core", "typescript": "typescript",
           "kafka": "kafkajs", "mongodb": "mongoose", "postgresql": "pg"}


def make_repo(rng: random.Random, name: str, uses: list[str], desc: str, *, mine: int, others: int,
              fork: bool = False, tutorial_readme: bool = False, spurious_tutorial_word: bool = False,
              year: int | None = None) -> dict:
    year = year or rng.randint(2019, 2025)
    created = _iso(year, rng.randint(1, 12), rng.randint(1, 28))
    pushed = _iso(min(2026, year + rng.randint(0, 1)), rng.randint(1, 9), rng.randint(1, 28))
    if pushed <= created:
        pushed = _iso(year + 1, 3, 1) if year < 2026 else created
    langs = [u for u in uses if u in LANGS]
    languages = {DISPLAY[u]: rng.randint(4000, 60000) for u in langs}
    manifests: set[str] = set()
    topics: list[str] = []
    readme_techs: list[str] = []
    extra: list[str] = ["src"]
    for u in uses:
        if u == "python":
            manifests.add(rng.choice(["requirements.txt", "pyproject.toml"]))
        elif u in ("javascript", "typescript") or u in FRONTEND:
            manifests.add("package.json")
        elif u == "java":
            manifests.add("pom.xml")
        elif u == "go":
            manifests.add("go.mod")
        elif u in ("fastapi", "django", "flask"):
            manifests.add("requirements.txt")
        if u == "docker":
            manifests.add("Dockerfile")
        if u == "ci/cd":
            readme_techs.append("CI/CD via GitHub Actions")
            extra.append(".github/workflows/ci.yml")
        elif u not in LANGS and u != "docker":
            if rng.random() < 0.6:
                topics.append(u)
            else:
                readme_techs.append(DISPLAY[u])
    if rng.random() < 0.6:
        extra.append("tests")
    # Declared dependencies (code evidence) and which uses are therefore shown in code.
    py_repo = any(u in ("python", "fastapi", "django", "flask") for u in uses)
    js_repo = any(u in ("javascript", "typescript") or u in FRONTEND for u in uses)
    py_deps = [PY_DEPS[u] for u in uses if py_repo and u in PY_DEPS]
    js_deps = [JS_DEPS[u] for u in uses if js_repo and u in JS_DEPS and not (py_repo and u in PY_DEPS)]
    contents: dict[str, str] = {}
    if py_deps:
        req = "requirements.txt" if "requirements.txt" in manifests or "pyproject.toml" not in manifests \
            else "pyproject.toml"
        manifests.add(req)
        contents[req] = ("\n".join(f"{d}>=1.0" for d in py_deps) + "\n" if req == "requirements.txt"
                         else '[project]\nname = "app"\ndependencies = ['
                         + ", ".join(f'"{d}>=1.0"' for d in py_deps) + "]\n")
    if js_deps:
        manifests.add("package.json")
        contents["package.json"] = json.dumps({"name": name, "dependencies": {d: "^1.0.0" for d in js_deps}})
    code_uses = set(langs) | {u for u in uses if (py_repo and u in PY_DEPS) or (js_repo and u in JS_DEPS)}
    if "docker" in uses:
        code_uses.add("docker")
    if "ci/cd" in uses:
        code_uses.add("ci/cd")
    readme = _readme_text(rng, name, desc, readme_techs, spurious_tutorial_word)
    if tutorial_readme:
        readme = f"# {name}\n\nFollowing along with a freecodecamp tutorial project.\n"
    return {
        "name": name, "fork": fork, "created_at": created, "pushed_at": pushed, "topics": topics,
        "default_branch": "main", "language": DISPLAY[langs[0]] if langs else "", "description": desc,
        "languages": languages, "readme": readme, "manifests": sorted(manifests), "extra_paths": extra,
        "commits": {"mine": mine, "others": others}, "uses": [] if fork else list(uses),
        "code_uses": [] if fork else sorted(code_uses), "manifest_contents": {} if fork else contents,
        "tutorial": bool(tutorial_readme),
    }


def gen_github(rng: random.Random, prof: dict, projects: list[dict]) -> dict:
    username = f"{prof['first'].lower()}{rng.choice(['', '-', '_'])}{prof['last'].lower()}{rng.randint(1, 99)}"
    username = username.replace("_", "-")
    repos = []
    backed: set[str] = set()
    for p in projects:
        if p["repo"] is None:
            continue
        mine = rng.randint(14, 70)
        others = rng.choice([0, 0, 2, 5, 10])
        if rng.random() < 0.15:
            mine, others = 15, 18  # shared team repo, still above the authorship threshold
        repos.append(make_repo(rng, p["repo"], p["uses"], p["desc"], mine=mine, others=others,
                               spurious_tutorial_word=rng.random() < 0.08))
        backed |= set(p["uses"])
    uncovered = [s for s in prof["skills"] if s not in backed]
    for _ in range(rng.choice([0, 1, 1, 2])):
        if not uncovered:
            break
        chosen = [u for u in uncovered if rng.random() < 0.55]
        if not chosen:
            continue
        lang = [s for s in prof["skills"] if s in LANGS]
        uses = ([rng.choice(lang)] if lang and not any(c in LANGS for c in chosen) else []) + chosen
        name = f"{rng.choice(SIDE_WORDS)}-{rng.choice(DOMAINS)}"
        if any(r["name"] == name for r in repos):
            continue
        repos.append(make_repo(rng, name, uses, f"Personal {name.split('-')[0]} repository", mine=rng.randint(6, 30), others=0))
        backed |= set(uses)
        uncovered = [s for s in uncovered if s not in uses]
    if uncovered and rng.random() < 0.10:  # shared team repo with a tiny contribution: WEAK by construction
        pick = rng.sample(uncovered, min(len(uncovered), 2))
        dom = rng.choice(DOMAINS)
        repos.append(make_repo(rng, f"team-{dom}-platform", pick, f"Team {dom} platform", mine=3, others=40))
        backed_low = set(pick)
    else:
        backed_low = set()
    if rng.random() < 0.25:  # untouched forks of popular projects; some named after a skill the candidate lists
        for _ in range(rng.choice([1, 1, 2])):
            cand = [DISPLAY[s].lower() for s in prof["skills"] if s not in LANGS and s not in ("ci/cd", "aws", "gcp", "azure")]
            fname = rng.choice(cand) if cand and rng.random() < 0.7 else rng.choice(["awesome-python", "interview-prep", "dotfiles-fork"])
            if any(r["name"] == fname for r in repos):
                continue
            fk = make_repo(rng, fname, [], f"Fork of {fname}", mine=0, others=40, fork=True, year=2022)
            fk["pushed_at"], fk["created_at"] = _iso(2022, 1, 5), _iso(2022, 6, 7)
            fk["languages"], fk["readme"], fk["manifests"], fk["extra_paths"] = {}, None, [], []
            repos.append(fk)
    repos.sort(key=lambda r: r["pushed_at"], reverse=True)
    return {"username": username, "status": "ok", "repos": repos, "_low": sorted(backed_low)}


def gen_portfolio(rng: random.Random, prof: dict, projects: list[dict], username: str | None) -> dict:
    slug = f"{prof['first'].lower()}{prof['last'].lower()}"
    mention = rng.sample(prof["skills"], max(2, min(len(prof["skills"]), rng.randint(2, 5))))
    mention_names = [DISPLAY[s] for s in mention]
    parts = [f"<html><head><title>{prof['name']}</title></head><body>", f"<h1>{prof['name']}</h1>",
             f"<p>Tools I use: {', '.join(mention_names)}.</p>"]
    demo_ok = rng.random() > 0.15
    for p in projects[:2]:
        parts.append(f"<h2>{p['name']}</h2><p>{p['desc']}.</p>")
    parts.append(f'<a href="https://{slug}-demo.vercel.app">Live demo</a>')
    if username and projects and projects[0]["repo"]:
        parts.append(f'<a href="https://github.com/{username}/{projects[0]["repo"]}">Source</a>')
    parts.append("</body></html>")
    html = "\n".join(parts)
    shown = [d for d in DISPLAY.values() if re.search(rf"(?<![\w/]){re.escape(d)}(?![\w/])", html)]
    return {"url": f"https://{slug}.dev", "robots": "disallow" if rng.random() < 0.05 else "allow",
            "html": html, "demo_ok": demo_ok, "mentions_display": shown}


def gen_linkedin(prof: dict) -> dict:
    roles = []
    for r in prof["roles"]:
        if not r["in_li"]:
            continue
        roles.append({"title": r["li_title"], "company": r["li_company"], "start": str(r["li_start"]),
                      "end": "Present" if r["li_end"] == "Present" else str(r["li_end"])})
    return {"type": "structured_json", "content": {"roles": roles, "education": [], "skills": [], "certifications": []}}


# ------------------------------------------------------------------ non-native English rendering
NN_VERBS = {"Built": "Build", "Designed": "Design", "Wrote": "Write", "Ran": "Run", "Tuned": "Tune", "Owned": "Own",
            "Deployed": "Deploy", "Developed": "Develop", "Implemented": "Implement", "Migrated": "Migrate",
            "Led": "Lead", "Spearheaded": "Lead", "Shipped": "Ship", "Worked": "Work", "Helped": "Help",
            "Assisted": "Assist", "Set up": "Set up"}
NN_VOCAB = [(" cutting ", " reducing "), (" improving ", " making better "), (" rewrote ", " wrote again "),
            (" handling ", " with "), (" automated test suites", " test suites"), (" robust ", " strong ")]
NN_PREP = [(" across ", " in "), (" for ", " on "), (" using ", " with ")]


def to_nonnative(text: str) -> str:
    """Rule-based, meaning-preserving rendering: article drops, simpler tense, different prepositions,
    plainer vocabulary. Numbers and technology names are untouched (checked by facts_preserved)."""
    out = text
    for k, v in NN_VERBS.items():
        if out.startswith(k + " "):
            out = v + out[len(k):]
            break
    out = re.sub(r"\b(the|a|an) (?=[a-z])", "", out)
    for a, b in NN_PREP + NN_VOCAB:
        out = out.replace(a, b)
    return out


def facts_preserved(a: str, b: str) -> bool:
    nums = lambda t: sorted(re.findall(r"\d+(?:\.\d+)?", t))  # noqa: E731
    techs = lambda t: sorted(  # noqa: E731
        d for d in DISPLAY.values() if re.search(rf"(?<![\w/]){re.escape(d)}(?![\w/])", t)
    )
    return nums(a) == nums(b) and techs(a) == techs(b) and a.count("percent") == b.count("percent")


# ------------------------------------------------------------------ rendering + claims
def render_resume(prof: dict, projects: list[dict], *, nonnative: bool = False) -> tuple[str, list[dict]]:
    st = prof["style"]
    f = to_nonnative if nonnative else (lambda s: s)
    claims: list[dict] = []

    def claim(ctype: str, text: str, **ref) -> None:
        claims.append({"type": ctype, "text": text, "ref": ref})

    items = [prof["stext"][s] for s in prof["skills"]] + [DISPLAY[s] for s in prof["injected"]]
    item_skill = {prof["stext"][s]: s for s in prof["skills"]} | {DISPLAY[s]: s for s in prof["injected"]}
    seen_lower: set[str] = set()
    for it in items:
        if it.lower() in seen_lower:
            continue
        seen_lower.add(it.lower())
        claim("SKILL", it, skill=item_skill[it], injected=item_skill[it] in prof["injected"])

    if st["grouped_skills"]:
        groups = [("Languages", [i for i in items if item_skill[i] in LANGS]),
                  ("Frameworks", [i for i in items if item_skill[i] in ("fastapi", "django", "flask", "react", "vue", "angular")]),
                  ("Databases", [i for i in items if item_skill[i] in ("postgresql", "mysql", "mongodb", "sqlite")])]
        used = {i for _, g in groups for i in g}
        groups.append(("Tools", [i for i in items if i not in used]))
        skills_lines = [f"{lab}: {st['skills_sep'].join(g)}" for lab, g in groups if g]
    else:
        skills_lines = [st["skills_sep"].join(items)]

    sections: dict[str, list[str]] = {}
    mk = st["marker"]
    # summary
    if prof["has_summary"]:
        w = st["writing"]
        s1 = f"{prof['title']} with {prof['years']} years of professional experience building {prof['domains'][0]} systems."
        s2 = ("Results-driven and passionate about robust, seamless software." if w == "buzzy"
              else f"Focused on reliable services in {DISPLAY[[s for s in prof['skills'] if s in LANGS][0]]}.")
        sections["summary"] = ["Summary", f(s1) + " " + f(s2)]
    sections["skills"] = [st["skills_heading"], *skills_lines]

    # experience
    exp = [st["exp_heading"]]
    order = prof["roles"] if st["role_order"] == "oldest_first" else list(reversed(prof["roles"]))
    body_techs: list[tuple[str, str]] = []
    for r in order:
        end = r["end"]
        dates = f"{r['start']} to {end}" if st["role_fmt"] == "to" else f"{r['start']} - {end}"
        if st["role_fmt"] == "comma":
            exp.append(f"{r['title']}, {r['company']}, {dates}")
        else:
            exp.append(f"{r['title']} at {r['company']}, {dates}")
        claim("ROLE", r["title"], role=r["company"], in_li=r["in_li"], conflict=r["conflict"])
        for b in r.get("bullets", []):
            text = f(b["text"])
            exp.append(f"{mk}{text}")
            ref = {}
            if b["kind"] == "METRIC" and b.get("trace_repo"):
                ref = {"trace_repo": b["trace_repo"], "true_pct": b["true_pct"], "claimed_pct": b["pct"]}
            claim(b["kind"], text, **ref)
            for t in b["techs"]:
                body_techs.append((DISPLAY[t], t))
    sections["experience"] = exp

    # projects
    pr = ["Projects"]
    for p in projects:
        desc = f(p["desc"])
        if st["project_fmt"] == "colon":
            pr.append(f"{mk}{p['name']}: {desc}")
            ctext = desc
        else:
            pr.append(f"{mk}{p['name']} - {desc}")
            ctext = f"{p['name']} - {desc}"
        claim("PROJECT", ctext, repo=p["repo"], project=p["name"], fork=p.get("fork", False),
              tutorial=p.get("tutorial", False))
        for t in p["uses"]:
            if re.search(rf"(?<![\w/]){re.escape(DISPLAY[t])}(?![\w/])", desc):
                body_techs.append((DISPLAY[t], t))
    sections["projects"] = pr if projects else []

    sections["education"] = ["Education", prof["degree"]]
    claim("EDUCATION", prof["degree"])
    if prof["certs"]:
        sections["certs"] = ["Certifications", *prof["certs"]]
        for c in prof["certs"]:
            claim("CERTIFICATION", c)
    if prof["ach"]:
        sections["ach"] = ["Achievements", *[f"{mk}{a}" for a in prof["ach"]]]
        for a in prof["ach"]:
            claim("ACHIEVEMENT", a, section="achievements")

    for disp, canon in body_techs:  # a technology named in a bullet or project is a claim of its own
        if disp.lower() not in seen_lower:
            seen_lower.add(disp.lower())
            claim("SKILL", disp, skill=canon, injected=False)

    lines = [prof["name"], f"{prof['email']} | {prof['phone']}", ""]
    for key in st["order"]:
        if sections.get(key):
            lines += sections[key] + [""]
    return "\n".join(lines).strip() + "\n", claims


# ------------------------------------------------------------------ ground truth
def _authorship(repo: dict) -> float:
    tot = repo["commits"]["mine"] + repo["commits"]["others"]
    return repo["commits"]["mine"] / tot if tot else 0.0


def label_claims(claims: list[dict], world: dict) -> list[str | None]:
    """Ground-truth status per claim, derived only from the world and the construction flags."""
    gh, li, pf = world["github"], world["linkedin"], world["portfolio"]
    repos = gh["repos"] if gh and gh["status"] == "ok" else []
    gh_ok = bool(repos)  # a GitHub account with no public repos cannot adjudicate anything
    pf_ok = bool(pf and pf["robots"] == "allow")
    out: list[str | None] = []
    for c in claims:
        t, ref = c["type"], c["ref"]
        if t == "SKILL":
            s = ref["skill"]
            using = [r for r in repos if not r["fork"] and s in r["uses"]]
            authored = [r for r in using if _authorship(r) >= 0.30]
            in_code = [r for r in authored if s in r.get("code_uses", [])]
            status: str | None
            if in_code:
                status = None if all(r.get("tutorial") for r in in_code) else "VERIFIED"
            elif authored:
                # Shown only in a topic label or README prose: corroboration, not verification.
                status = None if all(r.get("tutorial") for r in authored) else "WEAK"
            elif using or pf_ok and DISPLAY[s] in pf["mentions_display"]:
                status = "WEAK"
            elif gh_ok:
                status = "UNSUPPORTED"
            else:
                status = "UNVERIFIABLE"
        elif t == "PROJECT":
            if not gh_ok:
                status = "UNVERIFIABLE"
            else:
                match = next((r for r in repos if r["name"] == ref["repo"]), None) if ref["repo"] else None
                if match is None:
                    status = "UNSUPPORTED"
                elif match["fork"] or match.get("tutorial"):
                    status = None
                else:
                    status = "VERIFIED" if _authorship(match) >= 0.30 else "WEAK"
        elif t == "ROLE":
            if li is None or not li["content"]["roles"]:
                status = "UNVERIFIABLE"
            elif ref["conflict"]:
                status = "CONTRADICTED"
            elif ref["in_li"]:
                status = "CORROBORATED"
            else:
                status = "UNSUPPORTED"
        elif t == "METRIC" and ref.get("trace_repo"):
            traced = gh_ok and any(r["name"] == ref["trace_repo"] and not r["fork"] for r in repos)
            if not traced:
                status = "UNVERIFIABLE"
            else:
                status = "VERIFIED" if ref["true_pct"] == ref["claimed_pct"] else "CONTRADICTED"
        else:
            status = "UNVERIFIABLE"
        out.append(status)
    return out


# ------------------------------------------------------------------ jd
def make_jd(rng: random.Random, prof: dict) -> tuple[str, list[str]]:
    s = [x for x in prof["skills"] if x not in ("ci/cd", "pytest")]
    must = rng.sample(s, min(len(s), rng.randint(3, 4)))
    pref = rng.sample([x for x in s if x not in must], min(1, len([x for x in s if x not in must])))
    lines = [f"{prof['title']}", "", "About the role", "We are hiring an engineer to build and operate production services.",
             "", "Must have", f"- Strong {DISPLAY[must[0]]} and production experience with {' and '.join(DISPLAY[m] for m in must[1:])}"]
    lines += ["- 3+ years of professional software engineering experience", "", "Preferred"]
    lines += [f"- {DISPLAY[p]} experience" for p in pref]
    return "\n".join(lines) + "\n", must + pref


# ------------------------------------------------------------------ assemble
def _attach_bullets(rng: random.Random, prof: dict, *, pct_policy: str) -> None:
    """pct_policy: 'one' exactly one percent bullet (needed by P2), 'free' up to two."""
    first_pct_done = False
    for r in prof["roles"]:
        count = 1 if r["intern"] else (2 if prof["style"]["writing"] == "terse" else rng.randint(2, 4))
        if pct_policy == "one":
            r["bullets"] = gen_bullets(rng, prof, count, allow_pct=not first_pct_done, max_pct=1)
            first_pct_done = first_pct_done or any(b["pct"] for b in r["bullets"])
        else:
            r["bullets"] = gen_bullets(rng, prof, count, allow_pct=True, max_pct=1)
    if pct_policy == "one" and not first_pct_done:  # force a percent bullet onto the latest role
        r = prof["roles"][-1]
        ctx = _ctx(rng, prof)
        if ctx["db"]:
            p = rng.randint(12, 35)
            db = DISPLAY[rng.choice(ctx["db"])]
            r["bullets"].append({"id": "schema", "text": f"Designed {db} schemas and rewrote the {prof['domains'][0]} query, cutting run time by {p} percent",
                                 "kind": "METRIC", "techs": [k for k, v in DISPLAY.items() if v == db], "pct": p})
            first_pct_done = True
    return None


def _trace_one_metric(rng: random.Random, prof: dict, world: dict) -> None:
    """Record a README figure for one percent bullet (the artifact trace)."""
    repos = [r for r in world["github"]["repos"] if not r["fork"] and not r.get("tutorial")]
    if not repos:
        return
    for r in prof["roles"]:
        for b in r.get("bullets", []):
            if b["kind"] == "METRIC" and b["pct"]:
                repo = rng.choice(repos)
                repo["readme"] = (repo["readme"] or "") + f"\nBenchmarks: run time reduced by {b['pct']} percent.\n"
                b["trace_repo"], b["true_pct"] = repo["name"], b["pct"]
                return


def build_candidate(idx: int, kind: str, rng: random.Random) -> dict:
    for attempt in range(20):
        cand = _try_build(idx, kind, random.Random(rng.random() + attempt))
        if cand is not None:
            return cand
    raise RuntimeError(f"could not build candidate {idx} of kind {kind}")


def _try_build(idx: int, kind: str, rng: random.Random) -> dict | None:
    prof = gen_profile(rng, kind)
    if kind == "P4":
        prof["style"]["role_fmt"] = rng.choices(["at", "to", "comma"], [0.60, 0.25, 0.15])[0]
    if kind == "P2":
        prof["style"]["role_fmt"] = "at"
    projects = gen_projects(rng, prof)
    expected: dict = {}
    _attach_bullets(rng, prof, pct_policy="one" if kind == "P2" else "free")
    # world
    world: dict = {"github": None, "linkedin": None, "portfolio": None}
    has_gh = kind != "P6"
    if has_gh:
        world["github"] = gen_github(rng, prof, projects)
    want_li = (rng.random() < 0.6) if kind in ("genuine", "P1", "P5", "P2") else (True if kind == "P4" else rng.random() < 0.4)
    want_pf = rng.random() < (0.35 if kind in ("genuine", "P1", "P5", "P2") else 0.25)
    if kind == "P6":
        r6 = rng.random()
        if r6 < 0.20:
            world["github"] = {"username": f"{prof['first'].lower()}-{prof['last'].lower()}", "status": "ok", "repos": [], "_low": []}
            expected["p6_variant"] = "empty_github"
        elif r6 < 0.30:
            world["github"] = {"username": f"{prof['first'].lower()}{prof['last'].lower()}", "status": "not_found", "repos": [], "_low": []}
            expected["p6_variant"] = "github_404"
        else:
            expected["p6_variant"] = "no_github"
    if want_pf:
        world["portfolio"] = gen_portfolio(rng, prof, projects, world["github"]["username"] if has_gh and world["github"] else None)
    # tracing a README figure (P2 requires it; genuine candidates sometimes have it)
    if world["github"] and world["github"]["repos"] and (kind == "P2" or (kind == "genuine" and rng.random() < 0.30)):
        _trace_one_metric(rng, prof, world)

    # perturbations that edit facts
    if kind == "P1":
        in_world = set(prof["skills"])
        pool = [s for s in INJECTABLE if s not in in_world and not (IMPLIED.get(s, set()) & in_world)]
        n_inj = rng.randint(3, 5)
        prof["injected"] = rng.sample(pool, min(n_inj, len(pool)))
        expected["injected_skills"] = list(prof["injected"])
    if kind == "P5":
        proj = projects[0]
        variant = rng.choices(["fork", "tutorial"], [0.6, 0.4])[0]
        repo = next(r for r in world["github"]["repos"] if r["name"] == proj["repo"])
        if variant == "fork":
            repo.update(fork=True, uses=[], commits={"mine": 0, "others": 40}, readme=None, manifests=[], extra_paths=[],
                        languages={}, created_at=_iso(2023, 6, 7), pushed_at=_iso(2023, 1, 5))
            proj["fork"] = True
        else:
            repo.update(readme=f"# {repo['name']}\n\nFollowing along with a freecodecamp tutorial project.\n", tutorial=True)
            proj["tutorial"] = True
        expected.update(p5_variant=variant, p5_repo=proj["repo"], p5_flags=["fork_claimed_as_own", "tutorial_clone"] if variant == "fork" else ["tutorial_clone"])
        # a P5 candidate has no other repo for the project's skills only if the fork was their only evidence
    if kind == "P4":
        sub = rng.choices(["date_end", "date_start", "title_inflate", "title_different", "overlap"], [0.30, 0.20, 0.20, 0.15, 0.15])[0]
        roles = prof["roles"]
        for r in roles:
            r["in_li"] = True
        target = None
        if sub == "date_end":
            target = rng.randrange(len(roles))
            r = roles[target]
            res_end = REF_YEAR if r["end"] == "Present" else r["end"]
            li_end = res_end - rng.randint(1, 2)
            if li_end <= r["start"]:
                return None
            r["li_end"], r["conflict"] = li_end, "date"
            expected["p4_types"] = ["date_conflict", "overlapping_roles"]
        elif sub == "date_start":
            target = 0
            r = roles[0]
            r["start"] = r["start"] - rng.randint(2, 3)
            r["conflict"] = "date"
            expected["p4_types"] = ["date_conflict", "overlapping_roles", "graduation_inconsistency"]
        elif sub == "title_inflate":
            cands = [i for i, r in enumerate(roles) if not r["intern"] and not re.match(r"(Senior|Lead|Staff|Principal)", r["title"])]
            if not cands:
                return None
            target = rng.choice(cands)
            r = roles[target]
            r["title"] = rng.choice(["Lead ", "Principal ", "Staff "]) + r["title"]
            r["li_title"] = roles[target]["li_title"]
            r["conflict"] = "title"
            expected["p4_types"] = ["title_mismatch"]
        elif sub == "title_different":
            target = rng.randrange(len(roles))
            r = roles[target]
            r["li_title"] = rng.choice(["Data Analyst", "QA Analyst", "Technical Support Specialist", "Project Coordinator"])
            r["conflict"] = "title"
            expected["p4_types"] = ["title_mismatch"]
        else:  # overlap, reusing eval/perturb.p4_date_contradiction on the rendered text
            expected["p4_types"] = ["overlapping_roles", "date_conflict"]
        expected["p4_subtype"] = sub
        expected["p4_target_role"] = roles[target]["company"] if target is not None else None
        prof["_p4_overlap"] = sub == "overlap"
    if want_li or kind == "P4":
        world["linkedin"] = gen_linkedin(prof)
    # render
    text, claims = render_resume(prof, projects)
    if kind == "P4" and prof.get("_p4_overlap"):
        new_text, info = perturb.p4_date_contradiction(text)
        old_l, new_l = text.splitlines(), new_text.splitlines()
        diff = [(o, n) for o, n in zip(old_l, new_l, strict=True) if o != n]
        if len(diff) != 1 or prof["style"]["role_fmt"] == "to":
            return None
        o, n = diff[0]
        hit = next((r for r in prof["roles"] if o.startswith(r["title"])), None)
        if hit is None:
            return None
        hit["conflict"] = "date"
        expected["p4_target_role"] = hit["company"]
        text = new_text
        for c in claims:
            if c["type"] == "ROLE" and c["ref"].get("role") == hit["company"]:
                c["ref"]["conflict"] = "date"
        # LinkedIn keeps the truthful dates (already generated before the edit)
    if kind == "P1" and not prof["style"]["grouped_skills"]:
        # Reuse eval/perturb: the same injection a padded resume would show. Claims are identical to the
        # ones render_resume produced for the injected skills.
        base_text, _ = render_resume(dict(prof, injected=[]), projects)
        text, _ = perturb.p1_inject_skills(base_text, [DISPLAY[s] for s in prof["injected"]])
    if kind == "P2":
        base_text = text
        new_text, info = perturb.p2_metric_inflation(base_text)
        if new_text == base_text:
            return None
        old_l, new_l = base_text.splitlines(), new_text.splitlines()
        diff = [(o, n) for o, n in zip(old_l, new_l, strict=True) if o != n]
        if len(diff) != 1:
            return None
        o, n = diff[0]
        hit = False
        for c in claims:
            if c["type"] == "METRIC" and c["ref"].get("trace_repo") and c["text"] in o:
                c["text"] = n.lstrip("-*• ").strip()
                c["ref"]["claimed_pct"] = info["inflated"]
                expected["p2_claim_text"] = c["text"]
                expected["p2_original"], expected["p2_inflated"] = info["original"], info["inflated"]
                hit = True
        if not hit or info["inflated"] == info["original"]:
            return None
        text = new_text
    # P4 role conflicts set on prof must be mirrored onto claim refs
    for c in claims:
        if c["type"] == "ROLE":
            r = next(r for r in prof["roles"] if r["company"] == c["ref"]["role"])
            c["ref"]["conflict"], c["ref"]["in_li"] = r["conflict"], r["in_li"]

    labels = label_claims(claims, world)
    for c, lab in zip(claims, labels, strict=True):
        c["gt"] = lab
    jd, required = make_jd(rng, prof)

    cand = {
        "id": f"syn-{idx:04d}", "kind": kind, "genuine": kind in ("genuine", "P6"), "name": prof["name"],
        "family": prof["family"], "style": prof["style"], "resume_text": text, "jd_text": jd,
        "required_skills": [DISPLAY[s] for s in required],
        "world": {k: v for k, v in world.items()}, "claims": claims, "expected": expected,
        "sources": {"github": bool(world["github"] and world["github"]["status"] == "ok" and world["github"]["repos"]),
                    "linkedin": world["linkedin"] is not None, "portfolio": world["portfolio"] is not None},
    }
    if kind in ("genuine", "P6"):
        nn_text, nn_claims = render_resume(prof, projects, nonnative=True)
        cand["resume_text_nonnative"] = nn_text
        cand["nonnative_facts_preserved"] = facts_preserved(text, nn_text)
        assert len(nn_claims) == len(claims)
        # Same facts, so the same labels; only the wording of bullets and project lines differs.
        cand["claims_nonnative"] = [dict(nc, gt=c["gt"]) for nc, c in zip(nn_claims, claims, strict=True)]
    if kind == "genuine" and world["github"] and world["github"]["repos"]:
        cand["variants"] = {"private_heavy": private_heavy_variant(rng, claims, world)}
    for r in world["github"]["repos"] if world["github"] else []:
        r.pop("_unused", None)
    if world["github"]:
        world["github"].pop("_low", None)
    return cand


def private_heavy_variant(rng: random.Random, claims: list[dict], world: dict) -> dict:
    """Same person, same resume, but the work lives in private repositories: the public account shows
    at most one small repository. Labels are re-derived from the thinner world."""
    gh = json.loads(json.dumps(world["github"]))
    keep = [r for r in gh["repos"] if not r["fork"] and r["name"].split("-")[0] in SIDE_WORDS][:1]
    forks = [r for r in gh["repos"] if r["fork"]][:1]
    gh["repos"] = sorted(keep + forks, key=lambda r: r["pushed_at"], reverse=True)
    if not gh["repos"]:
        small = make_repo(rng, "dotfiles-config", [], "Personal editor configuration", mine=9, others=0)
        gh["repos"] = [small]
    w2 = {"github": gh, "linkedin": world["linkedin"], "portfolio": world["portfolio"]}
    return {"world": {"github": gh}, "gt": label_claims(claims, w2)}


# ------------------------------------------------------------------ corpus
def _kind_counts(n: int) -> dict[str, int]:
    counts = {k: max(1, round(n * w)) for k, w in KIND_WEIGHTS.items()}
    while sum(counts.values()) > n:
        counts[max(counts, key=counts.get)] -= 1
    while sum(counts.values()) < n:
        counts["genuine"] += 1
    return counts


def generate(n: int = 300, seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)
    kinds: list[str] = []
    for k, c in _kind_counts(n).items():
        kinds += [k] * c
    rng.shuffle(kinds)
    cands = [build_candidate(i, k, random.Random(f"{seed}-{i}-{k}")) for i, k in enumerate(kinds)]
    # fixed stratified split: ~30% train, ~70% test per kind. Test candidates are never used for tuning.
    by_kind: dict[str, list[dict]] = {}
    for c in cands:
        by_kind.setdefault(c["kind"], []).append(c)
    srng = random.Random(f"{seed}-split")
    for group in by_kind.values():
        srng.shuffle(group)
        n_train = int(len(group) * TRAIN_FRACTION)
        for i, c in enumerate(group):
            c["split"] = "train" if i < n_train else "test"
    return cands


def write_corpus(cands: list[dict], out_dir: Path = OUT_DIR, seed: int = SEED) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"synthetic_only": True, "seed": seed, "n": len(cands), "train_fraction": TRAIN_FRACTION,
                "counts": {}, "note": "Invented people and sources. Ground truth is known by construction; "
                "results do not establish real-world performance. Never tune on the test split."}
    for split in ("train", "test"):
        part = [c for c in cands if c["split"] == split]
        (out_dir / f"{split}.json").write_text(json.dumps(part, separators=(",", ":"), sort_keys=True))
        manifest["counts"][split] = {"n": len(part), **{k: sum(1 for c in part if c["kind"] == k) for k in KIND_WEIGHTS}}
        manifest[f"{split}_sha"] = _stable_hash(part)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


if __name__ == "__main__":
    corpus = generate()
    print(json.dumps(write_corpus(corpus), indent=2))
