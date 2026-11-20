"""Gemini Vision timetable extraction pipeline using Gemini 3.8 Flash & Interactions API."""

from __future__ import annotations

import base64
import copy
import os
import re
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from models import (
    CANONICAL_DAYS,
    CANONICAL_TIME_SLOTS,
    ExtractedSlot,
    MultiTimetableExtraction,
    ScanStagingResponse,
    TimetableExtraction,
    normalize_day,
    normalize_group_code,
    normalize_session_type,
    normalize_time_slot,
)

SUPPORTED_MIME_TYPES: set[str] = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
}

_EXT_TO_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jfif": "image/jpeg",
    ".webp": "image/webp",
}

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_MODEL = DEFAULT_GEMINI_MODEL

# Ordered fallback chain if a model experiences temporary 503 high demand or retirement
FALLBACK_GEMINI_MODELS: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
)

_DEPRECATED_MODELS: set[str] = {
    "gemini-2.5-flash",
    "models/gemini-2.5-flash",
    "gemini-2.0-flash",
    "models/gemini-2.0-flash",
}

# Short-lived cache of the most recently responsive model during 503 demand spikes
_LAST_HEALTHY_MODEL: str | None = None
_LAST_HEALTHY_TS: float = 0.0
_HEALTHY_CACHE_TTL_SEC: float = 90.0

# Hard wall-clock budgets (seconds) per single model attempt so a congested model never hangs
_SCAN_ATTEMPT_TIMEOUT_SEC: float = 20.0
_PING_ATTEMPT_TIMEOUT_SEC: float = 8.0

SYSTEM_PROMPT = """You are an expert Algerian university academic timetable parser.
Your task is to analyze an uploaded timetable (PDF or image) and extract every scheduled class session into clean structured JSON matching the response schema.

STRICT EXTRACTION RULES:
1. Single vs. Multi-Section / Per-Group Documents (`timetables`):
   - If the uploaded document shows ONE timetable grid, output 1 entry in `timetables`.
   - If a multi-page PDF contains MULTIPLE distinct promos or sections across pages, extract EACH promo/section as its own `TimetableExtraction` object inside `timetables`.
   - IMPORTANT PER-GROUP HEADER RULE: Many Algerian universities (e.g., University of Skikda) export one timetable sheet per group with a header like `"Timetable — L2_S2-G6"` or `"L1 Info Sec 2 - G3"`:
     * Set `promo_name` to the promo/section WITHOUT the trailing group code (e.g., `"L2_S2"` or `"L1 Info Sec 2"`).
     * Set `group_target = "ALL"` for every `"COURS"` (lecture) slot on that sheet.
     * Set `group_target` to that header's group code (e.g., `"G6"` or `"G3"`) for every `"TD"` and `"TP"` slot on that sheet (unless a cell explicitly specifies another group).

2. Days (`day`):
   - Translate French, Arabic, or English day names into canonical English:
     Samedi / السبت / Saturday -> "Saturday"
     Dimanche / الأحد / Sunday -> "Sunday"
     Lundi / الإثنين / Monday -> "Monday"
     Mardi / الثلاثاء / Tuesday -> "Tuesday"
     Mercredi / الأربعاء / Wednesday -> "Wednesday"
     Jeudi / الخميس / Thursday -> "Thursday"
   - Ignore empty cells marked `---` or `-x-`.

3. Canonical 90-Minute Time Slots (`time_slot`):
   - Map university time blocks to the closest canonical 90-minute slot among ONLY these 6 values:
     - "08:00-09:30" (includes e.g. 08:00-09:30, 08:30-10:00)
     - "09:30-11:00" (includes e.g. 09:30-11:00, 09:40-11:10, 10:00-11:30)
     - "11:00-12:30" (includes e.g. 11:00-12:30, 11:15-12:45, 11:20-12:50)
     - "12:30-14:00" (includes e.g. 12:30-14:00, 12:45-14:15, 13:00-14:30)
     - "14:00-15:30" (includes e.g. 14:00-15:30, 14:15-15:45, 14:30-16:00, 14:40-16:10)
     - "15:30-17:00" (includes e.g. 15:30-17:00, 15:45-17:15, 16:00-17:30)
   - If a long session (such as a 3-hour TP) spans TWO consecutive 90-minute blocks (e.g., 08:00-11:00 or 14:00-17:00), emit TWO separate `ExtractedSlot` entries covering both canonical slots.

4. Session Types (`session_type`):
   - Must be strictly "COURS", "TD", or "TP".
   - Lectures / Amphitheater classes / Cours magistral -> "COURS".
   - Travaux Dirigés / Directed studies -> "TD".
   - Travaux Pratiques / Lab work -> "TP".
   - If a cell doesn't state TD/TP/Cours explicitly: full-row or Amphi sessions are "COURS"; Labo/SP/SPA/SPB sessions are "TP"; classroom S-room group sessions are "TD".

5. Group Targeting & Multi-Group Splitting (`group_target`):
   - If a lecture (`COURS`) applies to the whole section or promo, set `group_target = "ALL"`.
   - Normalize group labels as uppercase `"G1"`, `"G2"`, `"G3"`, ..., `"G15"` (strip leading zeros: `"G01"` -> `"G1"`).
   - CRITICAL: If a single cell applies to multiple groups (for example `"TD Algo G1 / G2"`, `"TP OS G3-G4"`, or `"G1, G2, G3"`), SPLIT them into separate `ExtractedSlot` objects — one object per individual group (`"G1"`, `"G2"`, etc.). NEVER output combined strings like `"G1/G2"` or `"G3-G4"`.

6. Module & Room (`module_name`, `room`):
   - Extract concise module names (e.g., `"ASD3"`, `"ProbaStat1"`, `"Archi-Ord"`, `"POO1"`, `"SysInfo"`, `"MethNum"`, `"GestProjets"`). Do NOT include the teacher's name in `module_name`.
   - Extract the room/location on the bottom line if visible (e.g., `"Amphi01"`, `"Amphi02"`, `"S24"`, `"SPB3"`, `"SPA3"`), or null if absent.
"""

_DAY_ORDER = {d: i for i, d in enumerate(CANONICAL_DAYS)}
_SLOT_ORDER = {s: i for i, s in enumerate(CANONICAL_TIME_SLOTS)}
_TYPE_ORDER = {"COURS": 0, "TD": 1, "TP": 2}

# Matches trailing single-group suffix in headers/filenames like "L2_S2-G6", "L1 Info Sec 2 - G03", "L3_GL_G2"
_TRAILING_GROUP_SUFFIX_RE = re.compile(
    r"^(?P<promo>.+?)(?:\s*[-–—_]+\s*|\s+)(?:GROUPE?\s*|GRP\s*|G)0*(?P<gnum>\d{1,2})$",
    re.IGNORECASE,
)


def get_active_gemini_model() -> str:
    """Return the configured Gemini model, upgrading deprecated 2.x model names automatically."""
    configured = (os.getenv("GEMINI_MODEL") or "").strip()
    if configured.startswith("models/"):
        configured = configured.removeprefix("models/")
    if not configured or configured in _DEPRECATED_MODELS:
        return DEFAULT_GEMINI_MODEL
    return configured


def _record_healthy_model(model_name: str) -> None:
    global _LAST_HEALTHY_MODEL, _LAST_HEALTHY_TS
    _LAST_HEALTHY_MODEL = model_name
    _LAST_HEALTHY_TS = time.monotonic()


def _candidate_models() -> list[str]:
    primary = get_active_gemini_model()
    ordered: list[str] = []

    # If a fallback model succeeded within the last 90s because primary was 503-congested, try it first
    if (
        _LAST_HEALTHY_MODEL
        and (time.monotonic() - _LAST_HEALTHY_TS) < _HEALTHY_CACHE_TTL_SEC
        and (not os.getenv("GEMINI_MODEL") or _LAST_HEALTHY_MODEL == primary or primary == DEFAULT_GEMINI_MODEL)
    ):
        ordered.append(_LAST_HEALTHY_MODEL)

    if primary not in ordered:
        ordered.append(primary)
    for m in FALLBACK_GEMINI_MODELS:
        if m not in ordered:
            ordered.append(m)
    return ordered


def _build_fast_failover_client(api_key: str, timeout_ms: int = 20000) -> genai.Client:
    """Build a GenAI client that fails over immediately on 503/timeout instead of stalling."""
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=timeout_ms,
            retry_options=types.HttpRetryOptions(attempts=0, http_status_codes=[408]),
        ),
    )


def _is_retryable_model_error(exc: Exception) -> bool:
    """Return True if we should seamlessly try the next Gemini 3.x Flash model in the fallback chain."""
    msg = f"{type(exc).__name__}: {exc}".upper()
    if any(auth_err in msg for auth_err in ("API_KEY_INVALID", "401", "403", "PERMISSION_DENIED")):
        return False
    return any(
        token in msg
        for token in (
            "404",
            "NOT_FOUND",
            "NO LONGER AVAILABLE",
            "503",
            "UNAVAILABLE",
            "SERVICE_UNAVAILABLE",
            "HIGH DEMAND",
            "429",
            "RESOURCE_EXHAUSTED",
            "TIMEOUT",
            "TIMED OUT",
            "THINKING_LEVEL",
            "500",
            "502",
            "504",
        )
    )


def _inline_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Dereference Pydantic $defs/$ref inline for clean Interactions API JSON Schema compliance."""
    raw = copy.deepcopy(schema)
    defs = raw.pop("$defs", {})

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_path = str(node["$ref"])
                ref_name = ref_path.rsplit("/", 1)[-1]
                target = copy.deepcopy(defs.get(ref_name, {}))
                for k, v in node.items():
                    if k != "$ref":
                        target[k] = v
                return _resolve(target)
            cleaned: dict[str, Any] = {}
            for k, v in node.items():
                if k in {"title", "default"}:
                    continue
                cleaned[k] = _resolve(v)
            return cleaned
        if isinstance(node, list):
            return [_resolve(item) for item in node]
        return node

    return _resolve(raw)


_EXTRACTION_JSON_SCHEMA: dict[str, Any] = _inline_json_schema(
    MultiTimetableExtraction.model_json_schema()
)


def _extract_interaction_text(interaction: Any) -> str:
    """Extract concatenated text output from a Gemini Interactions API response."""
    direct = getattr(interaction, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    chunks: list[str] = []
    for attr in ("steps", "outputs"):
        items = getattr(interaction, attr, None)
        if not isinstance(items, list):
            continue
        for step in items:
            step_type = getattr(step, "type", None) or (
                step.get("type") if isinstance(step, dict) else None
            )
            if step_type in {"thought", "user_input"}:
                continue
            content = getattr(step, "content", None) or (
                step.get("content") if isinstance(step, dict) else None
            )
            if isinstance(content, list):
                for part in content:
                    ptype = getattr(part, "type", None) or (
                        part.get("type") if isinstance(part, dict) else None
                    )
                    ptext = getattr(part, "text", None) or (
                        part.get("text") if isinstance(part, dict) else None
                    )
                    if ptype == "text" and isinstance(ptext, str):
                        chunks.append(ptext)
            else:
                ptext = getattr(step, "text", None) or (
                    step.get("text") if isinstance(step, dict) else None
                )
                if isinstance(ptext, str):
                    chunks.append(ptext)
    return "".join(chunks).strip()


def resolve_mime_type(content_type: str | None, filename: str | None) -> str:
    """Resolve and validate MIME type from upload headers and filename extension."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype == "image/jpg":
        ctype = "image/jpeg"
    if ctype in SUPPORTED_MIME_TYPES:
        return ctype

    if filename:
        ext = Path(filename).suffix.lower()
        if ext in _EXT_TO_MIME:
            return _EXT_TO_MIME[ext]

    raise ValueError(
        f"Unsupported file type '{content_type or filename}'. "
        "Please upload a PDF, PNG, JPEG, or WEBP file."
    )


def _split_promo_and_inferred_group(
    promo_name: str, fallback_filename: str | None = None
) -> tuple[str, str | None]:
    """Extract clean promo name and optional single-group suffix (e.g. 'L2_S2-G6' -> ('L2_S2', 'G6'))."""
    inferred_group: str | None = None
    cleaned_promo = re.sub(r"\s+", " ", (promo_name or "").strip())

    # Remove generic prefixes like "Timetable — " or "Emploi du temps : "
    cleaned_promo = re.sub(
        r"^(?:timetable|emploi\s+du\s+temps)\s*[-–—:]+\s*",
        "",
        cleaned_promo,
        flags=re.IGNORECASE,
    ).strip()

    m = _TRAILING_GROUP_SUFFIX_RE.match(cleaned_promo)
    if m and m.group("promo").strip():
        cleaned_promo = m.group("promo").strip()
        inferred_group = f"G{int(m.group('gnum'))}"
    elif fallback_filename:
        stem = Path(fallback_filename).stem.strip()
        mf = _TRAILING_GROUP_SUFFIX_RE.match(stem)
        if mf:
            inferred_group = f"G{int(mf.group('gnum'))}"
            if not cleaned_promo:
                cleaned_promo = mf.group("promo").strip()

    if not cleaned_promo:
        cleaned_promo = (
            Path(fallback_filename).stem.replace("_", " ").strip()
            if fallback_filename
            else "Imported Promo"
        )

    return cleaned_promo, inferred_group


def expand_group_targets(
    raw_group: str,
    session_type: str,
    default_td_tp_group: str = "G1",
) -> list[str]:
    """Split multi-group strings like 'G1/G2', 'G1, G2', or ranges 'G3-G4' into individual normalized codes."""
    cleaned = (raw_group or "").strip().upper()
    if not cleaned:
        return ["ALL"] if session_type == "COURS" else [default_td_tp_group]

    if cleaned in {"ALL", "TOUS", "TOUTE LA SECTION", "SECTION", "PROMO", "*"}:
        return ["ALL"] if session_type == "COURS" else [default_td_tp_group]

    cleaned = re.sub(r"^(?:GROUPES?|GRP)[\s:.\-]*", "", cleaned).strip()

    range_match = re.fullmatch(r"G?0*(\d+)\s*[-–—]\s*G?0*(\d+)", cleaned)
    if range_match:
        start_g, end_g = int(range_match.group(1)), int(range_match.group(2))
        if 1 <= start_g <= end_g and (end_g - start_g) <= 20:
            return [f"G{n}" for n in range(start_g, end_g + 1)]

    parts = [p.strip() for p in re.split(r"[/,;&+]+", cleaned) if p.strip()]
    if len(parts) > 1:
        expanded: list[str] = []
        for part in parts:
            expanded.extend(expand_group_targets(part, session_type, default_td_tp_group))
        return expanded

    return [normalize_group_code(cleaned, allow_all=True)]


def normalize_extraction(
    extraction: TimetableExtraction, fallback_name: str | None = None
) -> TimetableExtraction:
    """Post-process a single TimetableExtraction to guarantee normalized, split, deduplicated slots."""
    promo_name, inferred_group = _split_promo_and_inferred_group(
        extraction.promo_name, fallback_filename=fallback_name
    )
    default_group = inferred_group or "G1"

    normalized_slots: list[ExtractedSlot] = []
    seen: set[tuple[str, str, str, str, str | None, str | None]] = set()

    for item in extraction.slots:
        day = normalize_day(item.day)
        time_slot = normalize_time_slot(item.time_slot, snap_nearest=True)
        session_type = normalize_session_type(item.session_type)
        groups = expand_group_targets(
            item.group_target,
            session_type,
            default_td_tp_group=default_group,
        )

        mod_name = (
            item.module_name.strip()
            if item.module_name and item.module_name.strip()
            else None
        )
        room_name = (
            item.room.strip() if item.room and item.room.strip() else None
        )

        for grp in groups:
            if session_type == "COURS":
                grp = "ALL"
            elif inferred_group and grp in {"ALL", ""}:
                grp = inferred_group

            key = (day, time_slot, grp, session_type, mod_name, room_name)
            if key not in seen:
                seen.add(key)
                normalized_slots.append(
                    ExtractedSlot(
                        day=day,  # type: ignore[arg-type]
                        time_slot=time_slot,  # type: ignore[arg-type]
                        group_target=grp,
                        session_type=session_type,
                        module_name=mod_name,
                        room=room_name,
                    )
                )

    def _group_sort(g: str) -> tuple[int, int, str]:
        if g == "ALL":
            return (0, 0, "")
        m = re.match(r"^G(\d+)(.*)$", g)
        return (1, int(m.group(1)), m.group(2)) if m else (2, 0, g)

    normalized_slots.sort(
        key=lambda s: (
            _DAY_ORDER.get(s.day, 99),
            _SLOT_ORDER.get(s.time_slot, 99),
            _TYPE_ORDER.get(s.session_type, 99),
            _group_sort(s.group_target),
        )
    )

    return TimetableExtraction(promo_name=promo_name, slots=normalized_slots)


def _extract_via_interactions(
    client: Any,
    model_name: str,
    file_bytes: bytes,
    resolved_mime: str,
    user_prompt: str,
) -> list[TimetableExtraction]:
    """Run multimodal structured extraction using the Gemini Interactions API with a hard timeout."""
    b64_payload = base64.b64encode(file_bytes).decode("ascii")
    if resolved_mime == "application/pdf":
        media_block: dict[str, Any] = {
            "type": "document",
            "data": b64_payload,
            "mime_type": "application/pdf",
        }
    else:
        media_block = {
            "type": "image",
            "data": b64_payload,
            "mime_type": resolved_mime,
        }

    interaction = client.interactions.create(
        model=model_name,
        system_instruction=SYSTEM_PROMPT,
        input=[
            media_block,
            {"type": "text", "text": user_prompt},
        ],
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": _EXTRACTION_JSON_SCHEMA,
        },
        store=False,
        automatic_continuation=False,
        timeout=_SCAN_ATTEMPT_TIMEOUT_SEC,
    )

    raw_text = _extract_interaction_text(interaction)
    if not raw_text:
        raise RuntimeError(
            f"Gemini ({model_name}) returned an empty response when parsing the timetable."
        )
    validated = MultiTimetableExtraction.model_validate_json(raw_text)
    return validated.timetables


def _extract_via_generate_content(
    client: Any,
    model_name: str,
    file_bytes: bytes,
    resolved_mime: str,
    user_prompt: str,
) -> list[TimetableExtraction]:
    """Fallback extraction via client.models.generate_content (also supports mock test clients)."""
    file_part = types.Part.from_bytes(data=file_bytes, mime_type=resolved_mime)
    response = client.models.generate_content(
        model=model_name,
        contents=[file_part, user_prompt],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=MultiTimetableExtraction,
            temperature=0.1,
        ),
    )

    parsed_obj = getattr(response, "parsed", None)
    if isinstance(parsed_obj, MultiTimetableExtraction):
        return parsed_obj.timetables
    if isinstance(parsed_obj, TimetableExtraction):
        return [parsed_obj]
    if parsed_obj is not None:
        return MultiTimetableExtraction.model_validate(parsed_obj).timetables

    raw_text = getattr(response, "text", None)
    if not raw_text:
        raise RuntimeError("Gemini returned an empty response when parsing the timetable.")
    return MultiTimetableExtraction.model_validate_json(raw_text).timetables


def parse_timetable_bytes(
    file_bytes: bytes,
    mime_type: str,
    api_key: str | None = None,
    filename: str | None = None,
    client_override: object | None = None,
) -> ScanStagingResponse:
    """Extract one or multiple structured university timetables from PDF/image bytes via Gemini 3.8 Flash."""
    if not file_bytes:
        raise ValueError("Uploaded file is empty.")

    resolved_mime = resolve_mime_type(mime_type, filename)
    effective_key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()
    if not effective_key and client_override is None:
        raise PermissionError(
            "GEMINI_API_KEY is missing. Set it in .env or open the Settings modal in the top bar."
        )

    client = client_override or _build_fast_failover_client(
        effective_key, timeout_ms=int(_SCAN_ATTEMPT_TIMEOUT_SEC * 1000)
    )
    user_prompt = (
        "Extract every academic promo/section timetable from this document. "
        "Include each promo/section name and every COURS, TD, and TP slot mapped to the 6 canonical time slots. "
        "Split multi-group cells into separate slot entries."
    )
    if filename:
        user_prompt += f" (Source filename hint: {filename})"

    raw_timetables: list[TimetableExtraction] | None = None
    last_exc: Exception | None = None

    has_interactions = hasattr(client, "interactions") and hasattr(
        getattr(client, "interactions", None), "create"
    )

    candidates = _candidate_models()
    for pass_idx in range(2):
        for model_name in candidates:
            try:
                if has_interactions:
                    raw_timetables = _extract_via_interactions(
                        client=client,
                        model_name=model_name,
                        file_bytes=file_bytes,
                        resolved_mime=resolved_mime,
                        user_prompt=user_prompt,
                    )
                else:
                    raw_timetables = _extract_via_generate_content(
                        client=client,
                        model_name=model_name,
                        file_bytes=file_bytes,
                        resolved_mime=resolved_mime,
                        user_prompt=user_prompt,
                    )
                if client_override is None:
                    _record_healthy_model(model_name)
                break
            except Exception as exc:
                last_exc = exc
                if client_override is not None or not _is_retryable_model_error(exc):
                    raise
        if raw_timetables is not None or client_override is not None:
            break
        if pass_idx == 0:
            time.sleep(1.0)

    if raw_timetables is None:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Timetable extraction failed with no available Gemini models.")

    normalized_list = [
        normalize_extraction(tt, fallback_name=filename)
        for tt in raw_timetables
        if tt.slots or tt.promo_name
    ]

    if not normalized_list:
        fallback = normalize_extraction(
            TimetableExtraction(promo_name="", slots=[]), fallback_name=filename
        )
        normalized_list = [fallback]

    primary = normalized_list[0]
    return ScanStagingResponse(
        promo_name=primary.promo_name,
        slots=primary.slots,
        timetables=normalized_list,
        source_filename=filename,
    )


def test_gemini_api_key(api_key: str | None = None) -> dict[str, object]:
    """Verify connectivity and authentication with Gemini 3.8 Flash via the Interactions API."""
    effective_key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()
    if not effective_key:
        raise PermissionError("No GEMINI_API_KEY provided.")

    client = _build_fast_failover_client(
        effective_key, timeout_ms=int(_PING_ATTEMPT_TIMEOUT_SEC * 1000)
    )
    last_exc: Exception | None = None

    has_interactions = hasattr(client, "interactions") and hasattr(
        getattr(client, "interactions", None), "create"
    )

    for model_name in _candidate_models():
        try:
            if has_interactions:
                interaction = client.interactions.create(
                    model=model_name,
                    input="Reply with OK",
                    store=False,
                    automatic_continuation=False,
                    timeout=_PING_ATTEMPT_TIMEOUT_SEC,
                )
                text = (_extract_interaction_text(interaction) or "OK").strip()
                api_label = "Interactions API"
            else:
                response = client.models.generate_content(
                    model=model_name,
                    contents="Reply with OK",
                )
                text = (getattr(response, "text", None) or "OK").strip()
                api_label = "Models API"
            _record_healthy_model(model_name)
            return {
                "valid": True,
                "model": model_name,
                "reply": text[:32],
                "message": f"Successfully authenticated with {model_name} ({api_label})!",
            }
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_model_error(exc):
                raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Gemini API key verification failed with no available models.")
