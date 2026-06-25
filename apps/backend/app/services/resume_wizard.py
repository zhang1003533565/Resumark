"""Service helpers for the adaptive resume wizard."""

import copy
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from app.config_cache import get_content_language
from app.llm import _scrub_secrets, complete_json
from app.prompts.resume_wizard import RESUME_WIZARD_TURN_PROMPT
from app.prompts.templates import get_language_name
from app.services.improver import _sanitize_user_input
from app.schemas.models import (
    Education,
    Experience,
    Project,
    ResumeData,
    normalize_resume_data,
)
from app.schemas.resume_wizard import (
    ResumeWizardHistoryEntry,
    ResumeWizardProgress,
    ResumeWizardQuestion,
    ResumeWizardState,
)

logger = logging.getLogger(__name__)

RESUME_WIZARD_MAX_QUESTIONS = 15
_PROGRESS_BASELINE = 8

_VALID_SECTIONS = {
    "intro",
    "contact",
    "summary",
    "workExperience",
    "internships",
    "education",
    "personalProjects",
    "skills",
    "review",
}

_INTRO_QUESTION = (
    "你好，我会帮你一步步建立主简历。"
    "先告诉我你的姓名，以及你想投递或发展的职位方向。"
)

_SECTION_PROMPTS = {
    "intro": _INTRO_QUESTION,
    "contact": "你希望简历里放哪些联系方式？可以写邮箱、电话、城市、LinkedIn、GitHub 或个人网站。",
    "summary": "用一两句话描述你的职业定位、优势或目标方向。",
    "workExperience": (
        "请介绍一段工作经历：职位、公司、时间、你负责什么，以及有什么可量化成果。"
    ),
    "internships": (
        "请介绍一段实习经历：职位、公司、时间、你做了什么，以及带来了什么结果。"
    ),
    "education": (
        "请介绍你的教育背景：学校、专业/学位、时间，以及荣誉、课程或亮点。"
    ),
    "personalProjects": (
        "请介绍一个项目：你做了什么、为什么重要、用了哪些技术，以及最终结果。"
    ),
    "skills": "你希望简历里展示哪些工具、技术或能力？",
    "review": "我们先检查现有内容，再创建你的主简历。",
}

# The keyword ("my name", "name") may be lower- or upper-cased, but the captured
# name must start uppercase — so we case the keyword explicitly with [Mm]/[Nn]
# instead of re.IGNORECASE (which would let the [A-Z] capture match lowercase
# words and produce false positives like "domain name facebook is" -> "facebook is").
_INTRO_NAME_PATTERNS = (
    re.compile(r"\bI(?:'| a)m\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)"),
    re.compile(r"\b[Mm]y name is\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)"),
    re.compile(r"\b[Nn]ame(?:'s| is)?\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)"),
)
_LATIN_RE = re.compile(r"[A-Za-z]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{6,}\d)")
_URL_RE = re.compile(r"https?://[^\s，,；;]+")
_CHINESE_NAME_RE = re.compile(
    r"(?:我叫|我是|姓名是|名字是)\s*([\u4e00-\u9fff·]{2,8}|[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)"
)
_SKILL_SPLIT_RE = re.compile(r"[,，;；、/\n]+")
_FALLBACK_SEQUENCE = [
    "intro",
    "contact",
    "summary",
    "workExperience",
    "education",
    "personalProjects",
    "skills",
    "review",
]


def section_prompt(section: str) -> str:
    """Deterministic fallback question text for a section."""
    return _SECTION_PROMPTS.get(section, "你还想补充哪些内容？")


def _normalize_question_text(text: str, section: str) -> str:
    """Keep user-facing wizard questions Chinese even if an LLM returns English."""
    value = text.strip()
    if _LATIN_RE.search(value) and not _HAN_RE.search(value):
        return section_prompt(section)
    return value


def valid_section(section: str) -> str:
    """Clamp an LLM-provided section to a known value (defaults to review)."""
    return section if section in _VALID_SECTIONS else "review"


def build_initial_wizard_state() -> ResumeWizardState:
    """Build the first state shown to a user entering the wizard."""
    return ResumeWizardState(
        step="intro",
        resume_data=ResumeData(),
        current_question=ResumeWizardQuestion(text=_INTRO_QUESTION, section="intro"),
        progress=ResumeWizardProgress(current=0, total=_PROGRESS_BASELINE),
    )


def extract_intro_name(answer: str) -> str:
    """Extract a likely user name from the intro answer."""
    chinese_match = _CHINESE_NAME_RE.search(answer)
    if chinese_match:
        return chinese_match.group(1).strip().rstrip("。.")

    prefix = re.split(r"[,，。；;\n]", answer.strip(), maxsplit=1)[0].strip()
    if 2 <= len(prefix) <= 8 and re.fullmatch(r"[\u4e00-\u9fff·]+", prefix):
        return prefix

    for pattern in _INTRO_NAME_PATTERNS:
        match = pattern.search(answer)
        if match:
            return match.group(1).strip().rstrip(".")
    return ""


def merge_unique_skills(existing: list[str], inferred: list[str]) -> list[str]:
    """Merge skills while preserving first-seen casing and order."""
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*existing, *inferred]:
        skill = item.strip()
        key = skill.casefold()
        if skill and key not in seen:
            merged.append(skill)
            seen.add(key)
    return merged


def build_review_warnings(data: ResumeData) -> list[str]:
    """Deterministic, gentle notes about useful resume facts that are missing."""
    warnings: list[str] = []
    info = data.personalInfo
    # Name is the one HARD requirement for finalize (the request 422s without it),
    # so surface it at review rather than letting the user hit a generic failure.
    if not info.name.strip():
        warnings.append("请补充姓名，这是创建简历的必填项。")
    contact = [
        info.email,
        info.phone,
        info.linkedin or "",
        info.github or "",
        info.website or "",
    ]
    if not any(value.strip() for value in contact):
        warnings.append("请至少补充一种联系方式，例如邮箱、电话或链接。")
    if not data.workExperience and not data.personalProjects:
        warnings.append("请至少补充一段工作、实习或项目经历。")
    if not data.education:
        warnings.append("教育背景目前为空；如果你确实不想填写，可以忽略。")
    if not data.additional.technicalSkills:
        warnings.append("技能目前为空；建议补充你使用过的工具或技术。")
    return warnings


def compute_progress(asked_count: int, is_complete: bool) -> ResumeWizardProgress:
    """Server-side progress so the bar never trusts the model."""
    total = min(
        RESUME_WIZARD_MAX_QUESTIONS,
        max(_PROGRESS_BASELINE, asked_count + (0 if is_complete else 2)),
    )
    return ResumeWizardProgress(current=min(asked_count, total), total=total)


def normalize_wizard_resume_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize wizard resume data through the shared resume schema."""
    normalized = normalize_resume_data(copy.deepcopy(data))
    return ResumeData.model_validate(normalized).model_dump()


def _string_list(value: Any) -> list[str]:
    """Return string items from a list-like LLM field."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _next_gap_section(data: ResumeData) -> str:
    """Pick the next obviously-empty section, else review."""
    if not data.workExperience:
        return "workExperience"
    if not data.education:
        return "education"
    if not data.personalProjects:
        return "personalProjects"
    if not data.additional.technicalSkills:
        return "skills"
    return "review"


def _has_contact(info: Any) -> bool:
    return any(
        (value or "").strip()
        for value in (
            info.email,
            info.phone,
            info.linkedin or "",
            info.github or "",
            info.website or "",
        )
    )


def _section_has_content(data: ResumeData, section: str) -> bool:
    if section == "intro":
        return bool(data.personalInfo.name.strip())
    if section == "contact":
        return _has_contact(data.personalInfo)
    if section == "summary":
        return bool(data.summary.strip())
    if section in {"workExperience", "internships"}:
        return bool(data.workExperience)
    if section == "education":
        return bool(data.education)
    if section == "personalProjects":
        return bool(data.personalProjects)
    if section == "skills":
        return bool(data.additional.technicalSkills)
    return True


def _fallback_next_section(current_section: str, data: ResumeData) -> str:
    """Pick a deterministic next section when the LLM is unavailable."""
    try:
        start = _FALLBACK_SEQUENCE.index(current_section) + 1
    except ValueError:
        start = 0
    for section in _FALLBACK_SEQUENCE[start:]:
        if not _section_has_content(data, section):
            return section
    for section in _FALLBACK_SEQUENCE:
        if not _section_has_content(data, section):
            return section
    return "review"


def _split_skills(answer_text: str) -> list[str]:
    parts = [part.strip(" .。") for part in _SKILL_SPLIT_RE.split(answer_text)]
    return [part for part in parts if part]


def _apply_local_answer(data: ResumeData, section: str, answer_text: str) -> ResumeData:
    """Best-effort local draft update used only when the LLM call fails."""
    answer = answer_text.strip()
    if not answer:
        return data

    updated = data.model_copy(deep=True)
    if section == "intro":
        name = extract_intro_name(answer)
        if name:
            updated.personalInfo.name = name
        elif not updated.summary.strip():
            updated.summary = answer
        return updated

    if section == "contact":
        if match := _EMAIL_RE.search(answer):
            updated.personalInfo.email = match.group(0)
        if match := _PHONE_RE.search(answer):
            updated.personalInfo.phone = match.group(0).strip()
        for url in _URL_RE.findall(answer):
            lowered = url.lower()
            if "linkedin.com" in lowered:
                updated.personalInfo.linkedin = url
            elif "github.com" in lowered:
                updated.personalInfo.github = url
            elif not updated.personalInfo.website:
                updated.personalInfo.website = url
        return updated

    if section == "summary":
        updated.summary = answer
        return updated

    if section in {"workExperience", "internships"}:
        updated.workExperience.append(Experience(description=[answer]))
        return updated

    if section == "education":
        updated.education.append(Education(description=answer))
        return updated

    if section == "personalProjects":
        updated.personalProjects.append(Project(name="项目经历", description=[answer]))
        return updated

    if section == "skills":
        updated.additional.technicalSkills = merge_unique_skills(
            updated.additional.technicalSkills, _split_skills(answer)
        )
        return updated

    return updated


def _is_solid_draft(data: ResumeData) -> bool:
    return bool(
        data.personalInfo.name.strip()
        and (data.workExperience or data.personalProjects)
        and data.additional.technicalSkills
    )


def _fallback_turn(state: ResumeWizardState, answer_text: str, *, skip: bool) -> ResumeWizardState:
    """Continue the wizard without failing when the LLM provider is unavailable."""
    section = state.current_question.section
    data = state.resume_data.model_copy(deep=True)
    if not skip:
        data = _apply_local_answer(data, section, answer_text)
    _assign_entry_ids(data)

    asked_count = state.asked_count + 1
    next_section = _fallback_next_section(section, data)
    is_complete = _is_solid_draft(data) or asked_count >= RESUME_WIZARD_MAX_QUESTIONS
    history = list(state.history)
    history.append(
        ResumeWizardHistoryEntry(
            question=state.current_question.text,
            answer="" if skip else answer_text,
            section=section,
            resume_data_before=state.resume_data,
        )
    )

    return ResumeWizardState(
        step="question",
        resume_data=data,
        current_question=ResumeWizardQuestion(
            text=section_prompt(next_section),
            section=next_section,
        ),
        history=history,
        asked_count=asked_count,
        inferred_skills=[],
        is_complete=is_complete,
        progress=compute_progress(asked_count, is_complete),
        warnings=[],
    )


def _merge_entries[T](
    existing: list[T],
    updated: list[T],
    key: Callable[[T], tuple[str, ...]],
) -> list[T]:
    """Union list entries by identity signature.

    A partial model reply (e.g. it echoes only the role the user just described
    instead of the full list) must NOT erase earlier entries. So: existing
    entries the model omits are kept, entries it echoes (same signature) are
    replaced in place, and genuinely new entries are appended. Signatures are
    content-based rather than ``id``-based because wizard entry ids default to 0.
    """
    result = list(existing)
    index: dict[tuple[str, ...], int] = {}
    for position, item in enumerate(result):
        index.setdefault(key(item), position)
    for item in updated:
        signature = key(item)
        if signature in index:
            result[index[signature]] = item
        else:
            index[signature] = len(result)
            result.append(item)
    return result


def _experience_key(item: Experience) -> tuple[str, ...]:
    return (
        item.title.strip().casefold(),
        item.company.strip().casefold(),
        item.years.strip().casefold(),
    )


def _education_key(item: Education) -> tuple[str, ...]:
    return (
        item.institution.strip().casefold(),
        item.degree.strip().casefold(),
        item.years.strip().casefold(),
    )


def _project_key(item: Project) -> tuple[str, ...]:
    return (item.name.strip().casefold(), item.years.strip().casefold())


def _merge_section(
    *,
    existing: ResumeData,
    updated: ResumeData,
    raw_updated: dict[str, Any],
    section: str,
    inferred_skills: list[str],
) -> ResumeData:
    """Merge LLM output ONLY into the active section, never clobbering the rest."""
    merged = existing.model_copy(deep=True)

    if section in {"intro", "contact"}:
        if isinstance(raw_updated.get("personalInfo"), dict):
            for field in ("name", "title", "email", "phone", "location"):
                new_val = getattr(updated.personalInfo, field)
                if isinstance(new_val, str) and new_val.strip():
                    setattr(merged.personalInfo, field, new_val)
            for field in ("website", "linkedin", "github"):
                new_val = getattr(updated.personalInfo, field)
                if new_val:
                    setattr(merged.personalInfo, field, new_val)
        return merged

    if section == "summary":
        if "summary" in raw_updated and updated.summary.strip():
            merged.summary = updated.summary
        return merged

    if section in {"workExperience", "internships"}:
        if "workExperience" in raw_updated:
            merged.workExperience = _merge_entries(
                merged.workExperience, updated.workExperience, _experience_key
            )
        return merged

    if section == "education":
        if "education" in raw_updated:
            merged.education = _merge_entries(
                merged.education, updated.education, _education_key
            )
        return merged

    if section == "personalProjects":
        if "personalProjects" in raw_updated:
            merged.personalProjects = _merge_entries(
                merged.personalProjects, updated.personalProjects, _project_key
            )
        return merged

    if section == "skills":
        raw_additional = raw_updated.get("additional")
        if isinstance(raw_additional, dict):
            if "technicalSkills" in raw_additional:
                merged.additional.technicalSkills = merge_unique_skills(
                    merged.additional.technicalSkills,
                    updated.additional.technicalSkills,
                )
            if "languages" in raw_additional:
                merged.additional.languages = merge_unique_skills(
                    merged.additional.languages, updated.additional.languages
                )
            if "certificationsTraining" in raw_additional:
                merged.additional.certificationsTraining = merge_unique_skills(
                    merged.additional.certificationsTraining,
                    updated.additional.certificationsTraining,
                )
            if "awards" in raw_additional:
                merged.additional.awards = merge_unique_skills(
                    merged.additional.awards, updated.additional.awards
                )
        merged.additional.technicalSkills = merge_unique_skills(
            merged.additional.technicalSkills, inferred_skills
        )
        return merged

    # Unknown / review section: never mutate resume_data.
    return merged


def _assign_entry_ids(data: ResumeData) -> None:
    """Give every list entry a unique 1-based id (in place).

    The LLM omits ``id`` (the wizard prompt's schema doesn't request it), so
    entries default to ``id=0``. Downstream consumers — the live preview's React
    keys and the builder's ``Math.max(...ids)+1`` add logic — assume unique ids,
    so renumber them deterministically by position (order is append-stable).
    """
    for index, item in enumerate(data.workExperience, start=1):
        item.id = index
    for index, item in enumerate(data.education, start=1):
        item.id = index
    for index, item in enumerate(data.personalProjects, start=1):
        item.id = index


def _next_question(result: dict[str, Any], data: ResumeData) -> ResumeWizardQuestion:
    """Use the model's next_question, or fall back to the next empty section."""
    candidate = result.get("next_question")
    if isinstance(candidate, dict):
        text = candidate.get("text")
        section = candidate.get("section")
        if isinstance(text, str) and text.strip() and isinstance(section, str):
            normalized_section = valid_section(section)
            return ResumeWizardQuestion(
                text=_normalize_question_text(text, normalized_section),
                section=normalized_section,
            )
    gap = _next_gap_section(data)
    return ResumeWizardQuestion(text=section_prompt(gap), section=gap)


async def run_ai_turn(
    state: ResumeWizardState,
    answer_text: str,
    *,
    skip: bool,
) -> ResumeWizardState:
    """Run one adaptive AI turn (answer or skip) and validate the result."""
    section = state.current_question.section
    resume_json = json.dumps(state.resume_data.model_dump(mode="json"), ensure_ascii=False)
    prompt_answer = (
        "(The user skipped this question. Do NOT modify resume_data. "
        "Ask the next most useful question for a different section.)"
        if skip
        # Strip prompt-injection patterns AND redact credential-like tokens
        # (sk-…/AIza…/Bearer …) before the answer reaches the LLM.
        else _scrub_secrets(_sanitize_user_input(answer_text))
    )
    prompt = RESUME_WIZARD_TURN_PROMPT.format(
        output_language=get_language_name(get_content_language()),
        current_section=section,
        resume_json=resume_json,
        answer_text=prompt_answer,
    )
    try:
        result = await complete_json(prompt, max_tokens=8192, schema_type="resume")
        if not isinstance(result, dict):
            raise ValueError("Resume wizard LLM response must be a JSON object.")
    except Exception as e:
        logger.warning(
            "Resume wizard AI turn failed; using deterministic fallback: %s",
            e,
        )
        return _fallback_turn(state, answer_text, skip=skip)

    raw_resume = result.get("resume_data")
    inferred = _string_list(result.get("inferred_skills"))

    if skip or not isinstance(raw_resume, dict):
        data = state.resume_data.model_copy(deep=True)
    else:
        updated = ResumeData.model_validate(normalize_wizard_resume_data(raw_resume))
        data = _merge_section(
            existing=state.resume_data,
            updated=updated,
            raw_updated=raw_resume,
            section=section,
            inferred_skills=inferred,
        )

    if section == "intro" and not data.personalInfo.name.strip():
        fallback = extract_intro_name(answer_text)
        if fallback:
            data.personalInfo.name = fallback

    # Entries from the LLM default to id=0; give them unique ids so the preview
    # keys and the builder's id-based logic work on a finalized wizard resume.
    _assign_entry_ids(data)

    asked_count = state.asked_count + 1
    # `is_complete` is a SUGGESTION to surface "Review & finish" — the step stays
    # "question" and never auto-finalizes. The client decides when to call /review.
    is_complete = bool(result.get("is_complete")) or asked_count >= RESUME_WIZARD_MAX_QUESTIONS

    history = list(state.history)
    history.append(
        ResumeWizardHistoryEntry(
            question=state.current_question.text,
            answer="" if skip else answer_text,
            section=section,
            resume_data_before=state.resume_data,
        )
    )

    return ResumeWizardState(
        step="question",
        resume_data=data,
        current_question=_next_question(result, data),
        history=history,
        asked_count=asked_count,
        inferred_skills=inferred,
        is_complete=is_complete,
        progress=compute_progress(asked_count, is_complete),
        warnings=[],
    )


def apply_back(state: ResumeWizardState) -> ResumeWizardState:
    """Deterministically restore the previous question + draft snapshot."""
    if not state.history:
        return state.model_copy(deep=True)
    history = list(state.history)
    last = history.pop()
    asked_count = max(0, state.asked_count - 1)
    # Derive step from the restored question itself, not just the count, so a
    # restored non-intro question never renders under the intro step (which hides
    # the question-step actions).
    return ResumeWizardState(
        step="intro" if last.section == "intro" else "question",
        resume_data=last.resume_data_before,
        current_question=ResumeWizardQuestion(
            text=_normalize_question_text(last.question, last.section),
            section=last.section,
        ),
        history=history,
        asked_count=asked_count,
        inferred_skills=[],
        is_complete=False,
        progress=compute_progress(asked_count, False),
        warnings=[],
    )


def apply_review(state: ResumeWizardState) -> ResumeWizardState:
    """Move to the review step (no LLM call) and compute gentle warnings."""
    next_state = state.model_copy(deep=True)
    next_state.step = "review"
    next_state.current_question = ResumeWizardQuestion(
        text=section_prompt("review"), section="review"
    )
    next_state.warnings = build_review_warnings(next_state.resume_data)
    return next_state
