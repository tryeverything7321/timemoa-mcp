"""PlayMCP submission server for conversation-first preference coordination."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from collections import deque
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Iterator, Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field, model_validator


mcp = FastMCP("preference_coordination_mcp", stateless_http=True, json_response=True)
DB_PATH = Path(os.getenv("COORDINATION_DB_PATH", "/data/coordination.db"))
ROOM_TTL_DAYS = int(os.getenv("COORDINATION_ROOM_TTL_DAYS", "7"))
MAX_ACTIVE_ROOMS = int(os.getenv("COORDINATION_MAX_ACTIVE_ROOMS", "5000"))
MAX_CREATES_PER_MINUTE = int(os.getenv("COORDINATION_MAX_CREATES_PER_MINUTE", "60"))
SEOUL = ZoneInfo("Asia/Seoul")
_CREATE_TIMES: deque[float] = deque()


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):  # noqa: ANN001
    from starlette.responses import JSONResponse

    try:
        with _connect() as connection:
            connection.execute("SELECT 1")
        return JSONResponse({"status": "ok", "server": "preference_coordination_mcp"})
    except sqlite3.Error:
        return JSONResponse(
            {"status": "error", "server": "preference_coordination_mcp"},
            status_code=503,
        )


class TimeIntervalInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    start: str = Field(
        ...,
        description="Asia/Seoul 기준 구간 시작 YYYY-MM-DDTHH:MM",
        min_length=16,
        max_length=16,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$",
    )
    end: str = Field(
        ...,
        description="Asia/Seoul 기준 구간 종료 YYYY-MM-DDTHH:MM",
        min_length=16,
        max_length=16,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$",
    )

    @model_validator(mode="after")
    def validate_interval(self) -> "TimeIntervalInput":
        if _parse_datetime(self.end) <= _parse_datetime(self.start):
            raise ValueError("구간 end는 start보다 뒤여야 합니다.")
        return self


class ParticipantInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., description="참여자 이름", min_length=1, max_length=50)
    hard_blocks: list[TimeIntervalInput] = Field(
        default_factory=list,
        description="현재 대화에서 확인된 절대 참석 불가 구간",
        max_length=30,
    )
    avoid: list[TimeIntervalInput] = Field(
        default_factory=list,
        description="현재 대화에서 확인된 가능하지만 피하고 싶은 구간",
        max_length=30,
    )
    prefer: list[TimeIntervalInput] = Field(
        default_factory=list,
        description="현재 대화에서 확인된 선호 구간",
        max_length=30,
    )
    covers_time_window: bool = Field(
        default=False,
        description=(
            "true는 이 참여자의 입력이 전체 후보 범위를 다루며 hard block 외 시간은 "
            "가능하다고 확인된 경우에만 사용"
        ),
    )


class CoordinateScheduleInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="회의 또는 모임 이름", min_length=1, max_length=100)
    date_start: str = Field(
        ...,
        description="후보 시작 날짜 YYYY-MM-DD",
        min_length=10,
        max_length=10,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    date_end: str = Field(
        ...,
        description="후보 종료 날짜 YYYY-MM-DD",
        min_length=10,
        max_length=10,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    daily_start: str = Field(
        default="09:00",
        description="매일 후보 시작 시각 HH:MM",
        min_length=5,
        max_length=5,
        pattern=r"^\d{2}:\d{2}$",
    )
    daily_end: str = Field(
        default="18:00",
        description="매일 후보 종료 시각 HH:MM",
        min_length=5,
        max_length=5,
        pattern=r"^\d{2}:\d{2}$",
    )
    duration_minutes: int = Field(default=60, description="회의 길이(분)", ge=15, le=480)
    participants: list[ParticipantInput] = Field(
        ...,
        description="참여자와 현재 대화에서 확인된 각 참여자의 일정 조건",
        min_length=2,
        max_length=12,
    )
    limit: int = Field(default=3, description="즉시 반환할 후보 수", ge=1, le=3)

    @model_validator(mode="after")
    def validate_window(self) -> "CoordinateScheduleInput":
        start_date = _parse_date(self.date_start)
        end_date = _parse_date(self.date_end)
        start_clock = _parse_clock(self.daily_start)
        end_clock = _parse_clock(self.daily_end)
        if end_date < start_date:
            raise ValueError("date_end는 date_start와 같거나 뒤여야 합니다.")
        if (end_date - start_date).days >= 14:
            raise ValueError("후보 날짜 범위는 최대 14일입니다.")
        if end_clock <= start_clock:
            raise ValueError("daily_end는 daily_start보다 뒤여야 합니다.")
        window_minutes = (
            datetime.combine(date.min, end_clock) - datetime.combine(date.min, start_clock)
        ).seconds // 60
        if self.duration_minutes > window_minutes:
            raise ValueError("회의 길이는 일일 후보 시간 범위보다 길 수 없습니다.")
        names = [participant.name.casefold() for participant in self.participants]
        if len(names) != len(set(names)):
            raise ValueError("참여자 이름은 중복될 수 없습니다.")
        return self


class SubmitPreferenceInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    coordination_id: str = Field(
        ...,
        description="이전 coordinate_schedule 결과의 내부 coordination_id. Agent가 같은 대화에서 재사용",
        min_length=20,
        max_length=64,
    )
    participant: str = Field(..., description="조건을 제출하는 참여자 이름", min_length=1, max_length=50)
    hard_blocks: list[TimeIntervalInput] = Field(
        default_factory=list,
        description="절대 참석할 수 없는 시간 구간",
        max_length=30,
    )
    avoid: list[TimeIntervalInput] = Field(
        default_factory=list,
        description="가능하지만 피하고 싶은 시간 구간",
        max_length=30,
    )
    prefer: list[TimeIntervalInput] = Field(
        default_factory=list,
        description="선호하는 시간 구간",
        max_length=30,
    )
    covers_time_window: bool = Field(
        ...,
        description="true면 위 hard block 외의 조율방 후보 시간에는 참석 가능하다고 확인한 것",
    )


class GetCandidatesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    coordination_id: str = Field(
        ...,
        description="이전 coordinate_schedule 결과의 내부 coordination_id. Agent가 같은 대화에서 재사용",
        min_length=20,
        max_length=64,
    )
    limit: int = Field(default=3, description="반환할 후보 수", ge=1, le=3)


class ConfirmCoordinationInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    coordination_id: str = Field(..., min_length=20, max_length=64)
    chosen_start: str = Field(
        ...,
        description="get_coordination_candidates가 반환한 후보 start 값",
        min_length=16,
        max_length=32,
    )


class RoomParticipant(BaseModel):
    name: str


class SubmitPreferenceOutput(BaseModel):
    status: Literal["recorded", "not_found", "participant_not_found", "invalid_request", "internal_error"]
    participant: str | None = None
    submitted_count: int = 0
    total_count: int = 0
    remaining_participants: list[str] = Field(default_factory=list)
    message: str


class MeetingCandidate(BaseModel):
    start: str
    end: str
    fully_confirmed: bool
    available_count: int
    participant_count: int
    avoid_count: int
    prefer_count: int
    unknown_participants: list[str]
    reason: str


class ConfirmedEvent(BaseModel):
    title: str
    start: str
    end: str
    timezone: str = "Asia/Seoul"


class ConfirmCoordinationOutput(BaseModel):
    status: Literal["confirmed", "not_found", "invalid_request", "needs_confirmation", "internal_error"]
    event: ConfirmedEvent | None = None
    google_calendar_url: str | None = None
    outlook_calendar_url: str | None = None
    message: str


class CoordinateScheduleOutput(BaseModel):
    status: Literal[
        "ready",
        "needs_input",
        "blocked",
        "invalid_request",
        "rate_limited",
        "capacity_reached",
        "internal_error",
    ]
    coordination_id: str | None = Field(
        default=None,
        description="후속 Tool 호출용 내부 상태 ID. Agent가 같은 AI 대화에서 보관하고 재사용",
    )
    title: str | None = None
    expires_at: str | None = None
    participants: list[RoomParticipant] = Field(default_factory=list)
    candidates: list[MeetingCandidate] = Field(default_factory=list)
    submitted_participants: list[str] = Field(default_factory=list)
    missing_participants: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    share_message: str
    message: str


class CandidateOutput(BaseModel):
    status: Literal["ready", "needs_input", "blocked", "not_found", "invalid_request", "internal_error"]
    title: str | None = None
    candidates: list[MeetingCandidate] = Field(default_factory=list)
    submitted_participants: list[str] = Field(default_factory=list)
    missing_participants: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confirmed_event: ConfirmedEvent | None = None
    message: str


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


def _parse_clock(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("시각은 HH:MM 형식이어야 합니다.") from exc
    if parsed.second or parsed.microsecond:
        raise ValueError("시각은 분 단위까지만 입력하세요.")
    return parsed


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("시간 구간은 ISO 8601 형식이어야 합니다.") from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coordination_id() -> str:
    return secrets.token_urlsafe(18)


def _allow_room_creation() -> bool:
    now = monotonic()
    cutoff = now - 60
    while _CREATE_TIMES and _CREATE_TIMES[0] <= cutoff:
        _CREATE_TIMES.popleft()
    if len(_CREATE_TIMES) >= MAX_CREATES_PER_MINUTE:
        return False
    _CREATE_TIMES.append(now)
    return True


def _initialize_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS coordination_rooms (
                coordination_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_coordination_rooms_expires_at "
            "ON coordination_rooms(expires_at)"
        )


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH, timeout=1)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _delete_expired(connection: sqlite3.Connection) -> None:
    connection.execute(
        "DELETE FROM coordination_rooms WHERE expires_at <= ?",
        (_utc_now().isoformat(),),
    )


def _load_room(connection: sqlite3.Connection, coordination_id: str) -> dict | None:
    row = connection.execute(
        "SELECT state_json FROM coordination_rooms WHERE coordination_id = ? AND expires_at > ?",
        (coordination_id, _utc_now().isoformat()),
    ).fetchone()
    return json.loads(row["state_json"]) if row else None


def _normalize_interval(interval: TimeIntervalInput, room: dict) -> dict:
    room_start = datetime.combine(
        _parse_date(room["date_start"]),
        _parse_clock(room["daily_start"]),
        SEOUL,
    )
    room_end = datetime.combine(
        _parse_date(room["date_end"]),
        _parse_clock(room["daily_end"]),
        SEOUL,
    )

    start = _parse_datetime(interval.start)
    end = _parse_datetime(interval.end)
    start = start.replace(tzinfo=SEOUL)
    end = end.replace(tzinfo=SEOUL)
    if start < room_start or end > room_end or end <= start:
        raise ValueError("선호 구간은 조율방 후보 범위 안에 있어야 합니다.")
    if start.date() != end.date():
        raise ValueError("한 시간 구간은 같은 날짜 안에서 시작하고 끝나야 합니다.")
    daily_start = _parse_clock(room["daily_start"])
    daily_end = _parse_clock(room["daily_end"])
    if start.time() < daily_start or end.time() > daily_end:
        raise ValueError("시간 구간은 조율방의 일일 후보 시간 안에 있어야 합니다.")
    return {"start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes")}


def _normalize_submission(
    room: dict,
    *,
    covers_time_window: bool,
    hard_blocks: list[TimeIntervalInput],
    avoid: list[TimeIntervalInput],
    prefer: list[TimeIntervalInput],
) -> dict:
    has_constraints = bool(hard_blocks or avoid or prefer)
    return {
        "covers_time_window": covers_time_window or has_constraints,
        "hard_blocks": [_normalize_interval(item, room) for item in hard_blocks],
        "avoid": [_normalize_interval(item, room) for item in avoid],
        "prefer": [_normalize_interval(item, room) for item in prefer],
        "submitted_at": _utc_now().isoformat(),
    }


def _overlaps(start: datetime, end: datetime, interval: dict) -> bool:
    interval_start = _parse_datetime(interval["start"])
    interval_end = _parse_datetime(interval["end"])
    return start < interval_end and interval_start < end


def _contains(interval: dict, start: datetime, end: datetime) -> bool:
    interval_start = _parse_datetime(interval["start"])
    interval_end = _parse_datetime(interval["end"])
    return interval_start <= start and end <= interval_end


def _candidate_slots(room: dict) -> list[tuple[datetime, datetime]]:
    start_date = _parse_date(room["date_start"])
    end_date = _parse_date(room["date_end"])
    daily_start = _parse_clock(room["daily_start"])
    daily_end = _parse_clock(room["daily_end"])
    duration = timedelta(minutes=room["duration_minutes"])
    step = timedelta(minutes=30)
    slots: list[tuple[datetime, datetime]] = []

    current_date = start_date
    while current_date <= end_date:
        cursor = datetime.combine(current_date, daily_start, SEOUL)
        day_end = datetime.combine(current_date, daily_end, SEOUL)
        while cursor + duration <= day_end:
            slots.append((cursor, cursor + duration))
            cursor += step
        current_date += timedelta(days=1)
    return slots


def _rank_candidates(room: dict, limit: int) -> list[MeetingCandidate]:
    participants = room["participants"]
    submissions = room["submissions"]
    ranked: list[tuple[tuple, MeetingCandidate]] = []

    for start, end in _candidate_slots(room):
        hard_conflict = False
        available_count = 0
        avoid_count = 0
        prefer_count = 0
        unknown: list[str] = []

        for participant in participants:
            submission = submissions.get(participant["id"])
            if submission is None:
                unknown.append(participant["name"])
                continue
            if any(_overlaps(start, end, interval) for interval in submission["hard_blocks"]):
                hard_conflict = True
                break
            is_avoid = any(_overlaps(start, end, interval) for interval in submission["avoid"])
            is_prefer = any(_contains(interval, start, end) for interval in submission["prefer"])
            if submission["covers_time_window"] or is_avoid or is_prefer:
                available_count += 1
            else:
                unknown.append(participant["name"])
            if is_avoid:
                avoid_count += 1
            if is_prefer:
                prefer_count += 1

        if hard_conflict:
            continue

        fully_confirmed = available_count == len(participants) and not unknown
        reason_parts = [f"가능 확인 {available_count}/{len(participants)}"]
        if avoid_count:
            reason_parts.append(f"비선호 {avoid_count}명")
        if prefer_count:
            reason_parts.append(f"선호 {prefer_count}명")
        if unknown:
            reason_parts.append(f"미확인 {len(unknown)}명")
        candidate = MeetingCandidate(
            start=start.isoformat(timespec="minutes"),
            end=end.isoformat(timespec="minutes"),
            fully_confirmed=fully_confirmed,
            available_count=available_count,
            participant_count=len(participants),
            avoid_count=avoid_count,
            prefer_count=prefer_count,
            unknown_participants=unknown,
            reason=" · ".join(reason_parts),
        )
        rank_key = (-available_count, avoid_count, -prefer_count, start.isoformat())
        ranked.append((rank_key, candidate))

    ranked.sort(key=lambda item: item[0])
    return [candidate for _, candidate in ranked[:limit]]


def _build_candidate_output(room: dict, limit: int) -> CandidateOutput:
    submitted_ids = set(room["submissions"])
    submitted = [item["name"] for item in room["participants"] if item["id"] in submitted_ids]
    missing = [item["name"] for item in room["participants"] if item["id"] not in submitted_ids]
    candidates = _rank_candidates(room, limit)
    if not candidates:
        return CandidateOutput(
            status="blocked",
            title=room["title"],
            submitted_participants=submitted,
            missing_participants=missing,
            risks=["현재 제출된 hard block을 모두 지키는 후보가 없습니다."],
            message="후보 범위를 넓히거나 참여자가 자신의 절대 불가 조건을 다시 확인해야 합니다.",
        )

    status: Literal["ready", "needs_input"] = "needs_input" if missing or any(
        candidate.unknown_participants for candidate in candidates
    ) else "ready"
    risks = []
    if missing:
        risks.append(f"아직 일정 정보가 없는 참여자: {', '.join(missing)}")
    if any(candidate.unknown_participants for candidate in candidates):
        risks.append("일부 참여자는 전체 후보 범위의 가능 여부가 확인되지 않았습니다.")
    return CandidateOutput(
        status=status,
        title=room["title"],
        candidates=candidates,
        submitted_participants=submitted,
        missing_participants=missing,
        assumptions=[
            "Agent가 Tool 입력으로 전달한 시간만 사용했으며 임의의 채팅방이나 외부 캘린더를 조회하지 않았습니다.",
            "조건을 하나 이상 제시한 참여자는 명시하지 않은 후보 시간에 참석 가능한 것으로 계산했습니다.",
            "후보는 30분 간격으로 계산했습니다.",
        ],
        risks=risks,
        confirmed_event=(
            ConfirmedEvent(**room["confirmed_event"])
            if room.get("confirmed_event")
            else None
        ),
        message="확정 전 모든 필수 참여자에게 최종 후보를 다시 확인하세요.",
    )


@mcp.tool(
    name="coordinate_schedule",
    annotations={
        "title": "대화 조건으로 일정 조율",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def coordinate_schedule(
    title: str,
    date_start: str,
    date_end: str,
    participants: list[ParticipantInput],
    daily_start: str = "09:00",
    daily_end: str = "18:00",
    duration_minutes: int = 60,
    limit: int = 3,
) -> CoordinateScheduleOutput:
    """시간모아는 현재 AI 대화에서 확인된 여러 참여자의 조건을 저장하고 후보를 계산한다.

    임의의 카카오톡 방을 읽지 않는다. Agent가 현재 대화에 제공된 참여자별 조건을
    구조화해 전달하며, 조건이 없는 참여자는 이름만 넣어 후속 확인 대상으로 남긴다.
    """
    try:
        params = CoordinateScheduleInput(
            title=title,
            date_start=date_start,
            date_end=date_end,
            participants=participants,
            daily_start=daily_start,
            daily_end=daily_end,
            duration_minutes=duration_minutes,
            limit=limit,
        )
    except ValueError as exc:
        return CoordinateScheduleOutput(
            status="invalid_request",
            share_message="조율을 시작하지 못했습니다.",
            message=str(exc),
        )

    if not _allow_room_creation():
        return CoordinateScheduleOutput(
            status="rate_limited",
            share_message="잠시 후 다시 시도해 주세요.",
            message="분당 조율 생성 한도를 초과했습니다.",
        )

    now = _utc_now()
    expires_at = now + timedelta(days=ROOM_TTL_DAYS)
    coordination_id = _coordination_id()
    participants = [
        {"id": secrets.token_hex(16), "name": participant.name}
        for participant in params.participants
    ]
    room = {
        "schema_version": 2,
        "coordination_id": coordination_id,
        "title": params.title,
        "date_start": params.date_start,
        "date_end": params.date_end,
        "daily_start": params.daily_start,
        "daily_end": params.daily_end,
        "timezone": "Asia/Seoul",
        "duration_minutes": params.duration_minutes,
        "participants": participants,
        "submissions": {},
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }

    try:
        for participant_input, participant in zip(params.participants, participants, strict=True):
            has_initial_response = bool(
                participant_input.covers_time_window
                or participant_input.hard_blocks
                or participant_input.avoid
                or participant_input.prefer
            )
            if has_initial_response:
                room["submissions"][participant["id"]] = _normalize_submission(
                    room,
                    covers_time_window=participant_input.covers_time_window,
                    hard_blocks=participant_input.hard_blocks,
                    avoid=participant_input.avoid,
                    prefer=participant_input.prefer,
                )
    except ValueError as exc:
        return CoordinateScheduleOutput(
            status="invalid_request",
            share_message="조율을 시작하지 못했습니다.",
            message=str(exc),
        )

    try:
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _delete_expired(connection)
            active_count = connection.execute(
                "SELECT COUNT(*) FROM coordination_rooms WHERE expires_at > ?",
                (now.isoformat(),),
            ).fetchone()[0]
            if active_count >= MAX_ACTIVE_ROOMS:
                return CoordinateScheduleOutput(
                    status="capacity_reached",
                    share_message="새 조율을 저장할 수 없습니다.",
                    message="활성 조율 저장 한도에 도달했습니다. 만료 후 다시 시도해 주세요.",
                )
            connection.execute(
                "INSERT INTO coordination_rooms(coordination_id, state_json, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (coordination_id, json.dumps(room, ensure_ascii=False), now.isoformat(), expires_at.isoformat()),
            )
    except sqlite3.Error:
        return CoordinateScheduleOutput(
            status="internal_error",
            share_message="조율방을 저장하지 못했습니다.",
            message="잠시 후 다시 시도해 주세요.",
        )

    snapshot = _build_candidate_output(room, params.limit)
    missing_text = ", ".join(snapshot.missing_participants)
    share_message = (
        f"{missing_text}님의 일정 정보가 더 필요합니다. "
        "같은 AI 대화에서 추가 조건을 알려주면 후보가 갱신됩니다."
        if snapshot.missing_participants
        else "조율 상태를 저장했습니다. 같은 AI 대화에서 변경 사항을 말하면 후보가 갱신됩니다."
    )
    return CoordinateScheduleOutput(
        status=snapshot.status,
        coordination_id=coordination_id,
        title=params.title,
        expires_at=expires_at.isoformat(),
        participants=[RoomParticipant(**participant) for participant in participants],
        candidates=snapshot.candidates,
        submitted_participants=snapshot.submitted_participants,
        missing_participants=snapshot.missing_participants,
        assumptions=snapshot.assumptions,
        risks=snapshot.risks,
        share_message=share_message,
        message=snapshot.message,
    )


@mcp.tool(
    name="submit_participant_preference",
    annotations={
        "title": "내 일정 선호 제출",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def submit_participant_preference(
    coordination_id: str,
    participant: str,
    covers_time_window: bool,
    hard_blocks: list[TimeIntervalInput] | None = None,
    avoid: list[TimeIntervalInput] | None = None,
    prefer: list[TimeIntervalInput] | None = None,
) -> SubmitPreferenceOutput:
    """시간모아에서 이전 조율의 한 참여자 조건을 추가하거나 교체한다. Agent가 내부 ID를 재사용한다."""
    try:
        params = SubmitPreferenceInput(
            coordination_id=coordination_id,
            participant=participant,
            covers_time_window=covers_time_window,
            hard_blocks=hard_blocks or [],
            avoid=avoid or [],
            prefer=prefer or [],
        )
    except ValueError as exc:
        return SubmitPreferenceOutput(status="invalid_request", message=str(exc))

    try:
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _delete_expired(connection)
            room = _load_room(connection, params.coordination_id)
            if room is None:
                return SubmitPreferenceOutput(status="not_found", message="조율방을 찾을 수 없거나 만료되었습니다.")

            participant = next(
                (item for item in room["participants"] if item["name"].casefold() == params.participant.casefold()),
                None,
            )
            if participant is None:
                return SubmitPreferenceOutput(
                    status="participant_not_found",
                    message="이 조율방에 등록된 참여자 이름이 아닙니다.",
                )

            try:
                submission = _normalize_submission(
                    room,
                    covers_time_window=params.covers_time_window,
                    hard_blocks=params.hard_blocks,
                    avoid=params.avoid,
                    prefer=params.prefer,
                )
            except ValueError as exc:
                return SubmitPreferenceOutput(status="invalid_request", message=str(exc))

            room["submissions"][participant["id"]] = submission
            connection.execute(
                "UPDATE coordination_rooms SET state_json = ? WHERE coordination_id = ?",
                (json.dumps(room, ensure_ascii=False), params.coordination_id),
            )
            submitted_ids = set(room["submissions"])
            remaining = [item["name"] for item in room["participants"] if item["id"] not in submitted_ids]
            return SubmitPreferenceOutput(
                status="recorded",
                participant=participant["name"],
                submitted_count=len(submitted_ids),
                total_count=len(room["participants"]),
                remaining_participants=remaining,
                message=(
                    "조건을 저장했습니다. 같은 이름으로 다시 제출하면 이전 조건을 통째로 교체합니다."
                ),
            )
    except sqlite3.Error:
        return SubmitPreferenceOutput(status="internal_error", message="조건을 저장하지 못했습니다.")


@mcp.tool(
    name="get_coordination_candidates",
    annotations={
        "title": "조율 후보 보기",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_coordination_candidates(
    coordination_id: str,
    limit: int = 3,
) -> CandidateOutput:
    """시간모아에서 이전 조율의 최신 후보를 반환한다. Agent가 내부 ID를 재사용한다."""
    try:
        params = GetCandidatesInput(coordination_id=coordination_id, limit=limit)
    except ValueError as exc:
        return CandidateOutput(status="invalid_request", message=str(exc))

    try:
        with _connect() as connection:
            room = _load_room(connection, params.coordination_id)
            if room is None:
                return CandidateOutput(status="not_found", message="조율방을 찾을 수 없거나 만료되었습니다.")
    except sqlite3.Error:
        return CandidateOutput(status="internal_error", message="조율 결과를 불러오지 못했습니다.")

    return _build_candidate_output(room, params.limit)


@mcp.tool(
    name="confirm_coordination",
    annotations={
        "title": "시간모아 후보 확정 및 캘린더 연결",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def confirm_coordination(
    coordination_id: str,
    chosen_start: str,
) -> ConfirmCoordinationOutput:
    """시간모아에서 전원 가능이 확인된 후보를 확정하고 캘린더 추가 링크를 만든다.

    coordinate_schedule 또는 get_coordination_candidates가 반환한 후보의 start 값을
    chosen_start로 사용한다. 미확인 참여자가 있거나 후보가 아닌 시간은 확정하지 않는다.
    """
    try:
        params = ConfirmCoordinationInput(
            coordination_id=coordination_id,
            chosen_start=chosen_start,
        )
        chosen = _parse_datetime(params.chosen_start)
        chosen = chosen.replace(tzinfo=SEOUL) if chosen.tzinfo is None else chosen.astimezone(SEOUL)
    except ValueError as exc:
        return ConfirmCoordinationOutput(status="invalid_request", message=str(exc))

    try:
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _delete_expired(connection)
            room = _load_room(connection, params.coordination_id)
            if room is None:
                return ConfirmCoordinationOutput(status="not_found", message="조율을 찾을 수 없거나 만료되었습니다.")

            candidates = _rank_candidates(room, len(_candidate_slots(room)))
            candidate = next(
                (
                    item for item in candidates
                    if _parse_datetime(item.start).astimezone(SEOUL) == chosen
                ),
                None,
            )
            if candidate is None:
                return ConfirmCoordinationOutput(
                    status="invalid_request",
                    message="시간모아가 반환한 후보 중 하나를 선택해 주세요.",
                )
            if not candidate.fully_confirmed:
                return ConfirmCoordinationOutput(
                    status="needs_confirmation",
                    message="미확인 참여자가 있어 아직 확정할 수 없습니다.",
                )

            event = ConfirmedEvent(
                title=room["title"],
                start=candidate.start,
                end=candidate.end,
            )
            room["confirmed_event"] = event.model_dump()
            connection.execute(
                "UPDATE coordination_rooms SET state_json = ? WHERE coordination_id = ?",
                (json.dumps(room, ensure_ascii=False), params.coordination_id),
            )
    except sqlite3.Error:
        return ConfirmCoordinationOutput(status="internal_error", message="일정을 확정하지 못했습니다.")

    start = _parse_datetime(event.start)
    end = _parse_datetime(event.end)
    google_dates = (
        f"{start.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}/"
        f"{end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    google_url = "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE",
        "text": event.title,
        "dates": google_dates,
        "details": "시간모아에서 참여자 조건을 조율해 확정한 일정입니다.",
    })
    outlook_url = "https://outlook.live.com/calendar/0/deeplink/compose?" + urlencode({
        "subject": event.title,
        "startdt": event.start,
        "enddt": event.end,
        "body": "시간모아에서 참여자 조건을 조율해 확정한 일정입니다.",
    })
    return ConfirmCoordinationOutput(
        status="confirmed",
        event=event,
        google_calendar_url=google_url,
        outlook_calendar_url=outlook_url,
        message="시간모아가 후보를 확정했습니다. 원하는 캘린더 추가 화면을 여세요.",
    )


if __name__ == "__main__":
    _initialize_db()
    allowed_hosts = os.getenv("MCP_ALLOWED_HOSTS")
    allowed_origins = os.getenv("MCP_ALLOWED_ORIGINS")
    if allowed_hosts:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host.strip() for host in allowed_hosts.split(",") if host.strip()],
            allowed_origins=[origin.strip() for origin in (allowed_origins or "").split(",") if origin.strip()],
        )
    else:
        # The Kakao Cloud hostname is assigned after deployment. The service is public-only
        # behind its HTTPS reverse proxy, so a localhost DNS-rebinding boundary is not useful.
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="streamable-http")
