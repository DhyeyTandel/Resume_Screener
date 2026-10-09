"""structure_resume: grouped skills, slash compounds, short names, achievements, role formats."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.modules.core_screening.core import structure_resume

ROOT = Path(__file__).resolve().parents[2]


def _skills(text: str) -> set[str]:
    return {s.lower() for s in structure_resume(text)["skills"]}


def test_grouped_skills_lines_drop_the_category_label() -> None:
    r = structure_resume(
        "Skills\nLanguages: Python, Go\nDatabases: Postgres; Mongo\nCloud: AWS | GCP\nOther: Docker\n"
    )
    assert {s.lower() for s in r["skills"]} == {"python", "go", "postgres", "mongo", "aws", "gcp", "docker"}
    assert not any(":" in s for s in r["skills"])


def test_slash_compounds_stay_intact_and_other_slashes_split() -> None:
    r = structure_resume("Skills\nCI/CD, Docker/Kubernetes, PL/SQL\n")
    low = {s.lower() for s in r["skills"]}
    assert "ci/cd" in low and "ci" not in low and "cd" not in low
    assert {"docker", "kubernetes"} <= low
    assert "pl/sql" in low


def test_short_skill_names_kept_in_skills_section_but_not_from_prose() -> None:
    assert {"go", "js", "ts"} <= _skills("Skills\nGo, JS, TS\n")
    prose = "Experience\nEngineer at Acme, 2020 - 2022\n- Ready to go live and go further, no go zones\n"
    assert "go" not in _skills(prose)
    named = "Experience\nEngineer at Acme, 2020 - 2022\n- Wrote Go services for billing\n"
    assert "go" in _skills(named)


@pytest.mark.parametrize("heading", ["Achievements", "Key Achievements", "Awards", "Honors", "Awards & Honors"])
def test_achievements_section_is_captured(heading: str) -> None:
    r = structure_resume(
        f"Skills\nPython\n\n{heading}\n- Won the internal hackathon, 2023\n- Employee of the quarter, 2024\n"
    )
    assert r["achievements"] == ["Won the internal hackathon, 2023", "Employee of the quarter, 2024"]
    assert r["skills"] == ["Python"]


@pytest.mark.parametrize(
    "line",
    [
        "Data Engineer at Acme Corp, 2019 - 2022",
        "Data Engineer, Acme Corp, 2019 - 2022",
        "Data Engineer | Acme Corp | 2019 - 2022",
        "Data Engineer - Acme Corp (2019 - 2022)",
        "Data Engineer, Acme Corp, Jan 2019 - Mar 2022",
    ],
)
def test_role_line_formats_populate_title_and_company(line: str) -> None:
    r = structure_resume(f"Experience\n{line}\n- Built pipelines\n")
    (role,) = r["experience"]
    assert (role["title"], role["company"]) == ("Data Engineer", "Acme Corp")
    assert (role["start"], role["end"]) == ("2019", role["end"]) and role["end"] in {"2022"}
    assert role["relevant_points"] == ["Built pipelines"]
    # Month-level math (Spec 9.3): "Jan 2019 - Mar 2022" is 39 months, year-only is 3 full years.
    assert r["total_years"] == (3.25 if "Jan 2019" in line else 3.0)


SAMPLE_EXPECTED = {
    "01_strong_match": (
        {"python", "fastapi", "postgresql", "kafka", "docker", "aws", "pytest", "ci/cd", "sql"},
        [("Senior Backend Engineer", "Northwind Payments", "2021", "Present"),
         ("Backend Engineer", "Corvid Systems", "2019", "2021")],
    ),
    "05_private_work": (
        {"python", "fastapi", "postgresql", "kafka", "docker", "sql"},
        [("Backend Engineer", "Vantage Health Systems", "2020", "Present"),
         ("Backend Developer", "Ridgeline Analytics", "2019", "2020")],
    ),
    "06_inflated_contradicted": (
        {"python", "fastapi", "postgresql", "kafka", "docker"},
        [("Lead Engineer", "Pinegate Software", "2018", "Present")],
    ),
    "07_brief": ({"python"}, []),
}


@pytest.mark.parametrize("stem", sorted(SAMPLE_EXPECTED))
def test_sample_resumes_still_structure_sensibly(stem: str) -> None:
    skills, roles = SAMPLE_EXPECTED[stem]
    r = structure_resume((ROOT / "sample_data" / "resumes" / f"{stem}.txt").read_text())
    got = {s.lower() for s in r["skills"]}
    assert skills <= got
    assert got - skills <= {"postgresql-backed"}  # body token already present before this change
    assert [(e["title"], e["company"], e["start"], e["end"]) for e in r["experience"]] == roles


def test_transferable_sample_keeps_skills_and_years() -> None:
    r = structure_resume((ROOT / "sample_data" / "resumes" / "02_transferable.txt").read_text())
    assert {"python", "django", "flask", "mysql", "pytest", "sql"} <= {s.lower() for s in r["skills"]}
    assert [e["company"] for e in r["experience"]] == ["Bluefin Retail", "Halcyon Labs"]
    # 2019 - 2021 then 2021 - Present: the present end is the current month, read from the clock.
    today = date.today()
    assert r["total_years"] == round(((today.year - 2019) * 12 + today.month) / 12, 2)
