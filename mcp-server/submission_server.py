"""PlayMCP submission server for participant-driven preference coordination."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


mcp = FastMCP("preference_coordination_mcp", stateless_http=True, json_response=True)
DB_PATH = Path(os.getenv("COORDINATION_DB_PATH", "/data/coordination.db"))
ROOM_TTL_DAYS = int(os.getenv("COORDINATION_ROOM_TTL_DAYS", "7"))


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


class ParticipantInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., description="참여자 이름", min_length=1, max_length=50)
    required: bool = Field(default=True, description="필수 참석 여부")


class CreateRoomInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="회의 또는 모임 이름", min_length=1, max_length=100)
    date_start: str = Field(..., description="후보 시작 날짜 YYYY-MM-DD")
    date_end: str = Field(..., description="후보 종료 날짜 YYYY-MM-DD")
    daily_start: str = Field(default="09:00", description="매일 후보 시작 시각 HH:MM")
    daily_end: str = Field(default="18:00", description="매일 후보 종료 시각 HH:MM")
    timezone: str = Field(default="Asia/Seoul", description="IANA timezone")
    duration_minutes: int = Field(default=60, description="회의 길이(분)", ge=15, le=480)
    participants: list[ParticipantInput] = Field(
        ...,
        description="조율에 참여할 사람",
        min_length=2,
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_window(self) -> "CreateRoomInput":
        start_date = _parse_date(self.date_start)
        end_date = _parse_date(self.date_end)
        start_clock = _parse_clock(self.daily_start)
        end_clock = _parse_clock(self.daily_end)
        if end_date < start_date:
            raise ValueError("date_end는 date_start와 같거나 뒤여야 합니다.")
        if (end_date - start_date).days > 31:
            raise ValueError("후보 날짜 범위는 최대 31일입니다.")
        if end_clock <= start_clock:
            raise ValueError("daily_end는 daily_start보다 뒤여야 합니다.")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("유효한 IANA timezone을 입력하세요.") from exc
        names = [participant.name.casefold() for participant in self.participants]
        if len(names) != len(set(names)):
            raise ValueError("참여자 이름은 중복될 수 없습니다.")
        return self


class TimeIntervalInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    start: str = Field(..., description="구간 시작 ISO 8601. 예: 2026-07-15T09:00")
    end: str = Field(..., description="구간 종료 ISO 8601. 예: 2026-07-15T12:00")

    @model_validator(mode="after")
    def validate_interval(self) -> "TimeIntervalInput":
        if _parse_datetime(self.end) <= _parse_datetime(self.start):
            raise ValueError("구간 end는 start보다 뒤여야 합니다.")
        return self


class SubmitPreferenceInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    room_code: str = Field(..., description="조율방 생성 시 받은 공유 코드", min_length=20, max_length=64)
    participant: str = Field(..., description="조건을 제출하는 참여자 이름", min_length=1, max_length=50)
    hard_blocks: list[TimeIntervalInput] = Field(
        default_factory=list,
        description="절대 참석할 수 없는 시간 구간",
        max_length=100,
    )
    avoid: list[TimeIntervalInput] = Field(
        default_factory=list,
        description="가능하지만 피하고 싶은 시간 구간",
        max_length=100,
    )
    prefer: list[TimeIntervalInput] = Field(
        default_factory=list,
        description="선호하는 시간 구간",
        max_length=100,
    )
    covers_time_window: bool = Field(
        ...,
        description="true면 위 hard block 외의 조율방 후보 시간에는 참석 가능하다고 확인한 것",
    )


class GetCandidatesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    room_code: str = Field(..., description="조율방 공유 코드", min_length=20, max_length=64)
    limit: int = Field(default=2, description="반환할 후보 수", ge=1, le=5)


class RoomParticipant(BaseModel):
    name: str
    required: bool


class CreateRoomOutput(BaseModel):
    status: Literal["ready", "invalid_request", "internal_error"]
    room_code: str | None = None
    title: str | None = None
    expires_at: str | None = None
    participants: list[RoomParticipant] = Field(default_factory=list)
    share_message: str
    next_step: str


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
    required_count: int
    avoid_count: int
    prefer_count: int
    unknown_participants: list[str]
    reason: str


class CandidateOutput(BaseModel):
    status: Literal["ready", "needs_input", "blocked", "not_found", "invalid_request", "internal_error"]
    title: str | None = None
    candidates: list[MeetingCandidate] = Field(default_factory=list)
    submitted_participants: list[str] = Field(default_factory=list)
    missing_participants: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
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


def _room_code() -> str:
    return secrets.token_urlsafe(18)


def _initialize_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS coordination_rooms (
                room_code TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    _initialize_db()
    connection = sqlite3.connect(DB_PATH, timeout=5)
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


def _load_room(connection: sqlite3.Connection, room_code: str) -> dict | None:
    _delete_expired(connection)
    row = connection.execute(
        "SELECT state_json FROM coordination_rooms WHERE room_code = ?",
        (room_code,),
    ).fetchone()
    return json.loads(row["state_json"]) if row else None


def _normalize_interval(interval: TimeIntervalInput, room: dict) -> dict:
    zone = ZoneInfo(room["timezone"])
    room_start = datetime.combine(
        _parse_date(room["date_start"]),
        _parse_clock(room["daily_start"]),
        zone,
    )
    room_end = datetime.combine(
        _parse_date(room["date_end"]),
        _parse_clock(room["daily_end"]),
        zone,
    )

    start = _parse_datetime(interval.start)
    end = _parse_datetime(interval.end)
    start = start.replace(tzinfo=zone) if start.tzinfo is None else start.astimezone(zone)
    end = end.replace(tzinfo=zone) if end.tzinfo is None else end.astimezone(zone)
    if start < room_start or end > room_end or end <= start:
        raise ValueError("선호 구간은 조율방 후보 범위 안에 있어야 합니다.")
    if start.date() != end.date():
        raise ValueError("한 시간 구간은 같은 날짜 안에서 시작하고 끝나야 합니다.")
    daily_start = _parse_clock(room["daily_start"])
    daily_end = _parse_clock(room["daily_end"])
    if start.time() < daily_start or end.time() > daily_end:
        raise ValueError("시간 구간은 조율방의 일일 후보 시간 안에 있어야 합니다.")
    return {"start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes")}


def _overlaps(start: datetime, end: datetime, interval: dict) -> bool:
    interval_start = _parse_datetime(interval["start"])
    interval_end = _parse_datetime(interval["end"])
    return start < interval_end and interval_start < end


def _contains(interval: dict, start: datetime, end: datetime) -> bool:
    interval_start = _parse_datetime(interval["start"])
    interval_end = _parse_datetime(interval["end"])
    return interval_start <= start and end <= interval_end


def _candidate_slots(room: dict) -> list[tuple[datetime, datetime]]:
    zone = ZoneInfo(room["timezone"])
    start_date = _parse_date(room["date_start"])
    end_date = _parse_date(room["date_end"])
    daily_start = _parse_clock(room["daily_start"])
    daily_end = _parse_clock(room["daily_end"])
    duration = timedelta(minutes=room["duration_minutes"])
    step = timedelta(minutes=30)
    slots: list[tuple[datetime, datetime]] = []

    current_date = start_date
    while current_date <= end_date:
        cursor = datetime.combine(current_date, daily_start, zone)
        day_end = datetime.combine(current_date, daily_end, zone)
        while cursor + duration <= day_end:
            slots.append((cursor, cursor + duration))
            cursor += step
        current_date += timedelta(days=1)
    return slots


def _rank_candidates(room: dict, limit: int) -> list[MeetingCandidate]:
    required = [participant for participant in room["participants"] if participant["required"]]
    submissions = room["submissions"]
    ranked: list[tuple[tuple, MeetingCandidate]] = []

    for start, end in _candidate_slots(room):
        hard_conflict = False
        available_count = 0
        avoid_count = 0
        prefer_count = 0
        unknown: list[str] = []

        for participant in required:
            submission = submissions.get(participant["id"])
            if submission is None:
                unknown.append(participant["name"])
                continue
            if any(_overlaps(start, end, interval) for interval in submission["hard_blocks"]):
                hard_conflict = True
                break
            if submission["covers_time_window"]:
                available_count += 1
            else:
                unknown.append(participant["name"])
            if any(_contains(interval, start, end) for interval in submission["avoid"]):
                avoid_count += 1
            if any(_contains(interval, start, end) for interval in submission["prefer"]):
                prefer_count += 1

        if hard_conflict:
            continue

        fully_confirmed = available_count == len(required) and not unknown
        reason_parts = [f"가능 확인 {available_count}/{len(required)}"]
        if avoid_count:
            reason_parts.append(f"회피 {avoid_count}명")
        if prefer_count:
            reason_parts.append(f"선호 {prefer_count}명")
        if unknown:
            reason_parts.append(f"미확인 {len(unknown)}명")
        candidate = MeetingCandidate(
            start=start.isoformat(timespec="minutes"),
            end=end.isoformat(timespec="minutes"),
            fully_confirmed=fully_confirmed,
            available_count=available_count,
            required_count=len(required),
            avoid_count=avoid_count,
            prefer_count=prefer_count,
            unknown_participants=unknown,
            reason=" · ".join(reason_parts),
        )
        rank_key = (-available_count, avoid_count, -prefer_count, start.isoformat())
        ranked.append((rank_key, candidate))

    ranked.sort(key=lambda item: item[0])
    return [candidate for _, candidate in ranked[:limit]]


@mcp.tool(
    name="create_coordination_room",
    annotations={
        "title": "조율방 만들기",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def create_coordination_room(params: CreateRoomInput) -> CreateRoomOutput:
    """회의 후보 범위와 참여자를 등록하고 공유할 조율방 코드를 만든다."""
    now = _utc_now()
    expires_at = now + timedelta(days=ROOM_TTL_DAYS)
    room_code = _room_code()
    participants = [
        {"id": secrets.token_hex(16), "name": participant.name, "required": participant.required}
        for participant in params.participants
    ]
    room = {
        "schema_version": 1,
        "room_code": room_code,
        "title": params.title,
        "date_start": params.date_start,
        "date_end": params.date_end,
        "daily_start": params.daily_start,
        "daily_end": params.daily_end,
        "timezone": params.timezone,
        "duration_minutes": params.duration_minutes,
        "participants": participants,
        "submissions": {},
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    try:
        with _connect() as connection:
            _delete_expired(connection)
            connection.execute(
                "INSERT INTO coordination_rooms(room_code, state_json, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (room_code, json.dumps(room, ensure_ascii=False), now.isoformat(), expires_at.isoformat()),
            )
    except sqlite3.Error:
        return CreateRoomOutput(
            status="internal_error",
            share_message="조율방을 저장하지 못했습니다.",
            next_step="잠시 후 다시 시도해 주세요.",
        )

    return CreateRoomOutput(
        status="ready",
        room_code=room_code,
        title=params.title,
        expires_at=expires_at.isoformat(),
        participants=[RoomParticipant(**participant) for participant in participants],
        share_message=(
            f"조율 코드 {room_code}\n각 참여자는 일정 조율 비서에서 이 코드와 함께 "
            "불가 시간, 피하고 싶은 시간, 선호 시간을 말해 주세요."
        ),
        next_step="참여자들이 제출한 뒤 get_coordination_candidates로 후보를 확인하세요.",
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
async def submit_participant_preference(params: SubmitPreferenceInput) -> SubmitPreferenceOutput:
    """조율방 참여자 한 명의 불가·회피·선호 구간을 등록하거나 교체한다."""
    try:
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            room = _load_room(connection, params.room_code)
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
                submission = {
                    "covers_time_window": params.covers_time_window,
                    "hard_blocks": [_normalize_interval(item, room) for item in params.hard_blocks],
                    "avoid": [_normalize_interval(item, room) for item in params.avoid],
                    "prefer": [_normalize_interval(item, room) for item in params.prefer],
                    "submitted_at": _utc_now().isoformat(),
                }
            except ValueError as exc:
                return SubmitPreferenceOutput(status="invalid_request", message=str(exc))

            room["submissions"][participant["id"]] = submission
            connection.execute(
                "UPDATE coordination_rooms SET state_json = ? WHERE room_code = ?",
                (json.dumps(room, ensure_ascii=False), params.room_code),
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
async def get_coordination_candidates(params: GetCandidatesInput) -> CandidateOutput:
    """제출된 조건을 집계해 hard block이 없는 회의 후보와 미응답자를 반환한다."""
    try:
        with _connect() as connection:
            room = _load_room(connection, params.room_code)
            if room is None:
                return CandidateOutput(status="not_found", message="조율방을 찾을 수 없거나 만료되었습니다.")
    except sqlite3.Error:
        return CandidateOutput(status="internal_error", message="조율 결과를 불러오지 못했습니다.")

    submitted_ids = set(room["submissions"])
    submitted = [item["name"] for item in room["participants"] if item["id"] in submitted_ids]
    missing = [item["name"] for item in room["participants"] if item["id"] not in submitted_ids]
    candidates = _rank_candidates(room, params.limit)
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
        risks.append(f"아직 응답하지 않은 참여자: {', '.join(missing)}")
    if any(candidate.unknown_participants for candidate in candidates):
        risks.append("일부 참여자가 후보 범위의 나머지 시간을 가능하다고 확인하지 않았습니다.")
    return CandidateOutput(
        status=status,
        title=room["title"],
        candidates=candidates,
        submitted_participants=submitted,
        missing_participants=missing,
        assumptions=[
            "입력된 시간만 사용했으며 실제 Google/Outlook 캘린더를 조회하지 않았습니다.",
            "후보는 30분 간격으로 계산했습니다.",
        ],
        risks=risks,
        message="확정 전 모든 필수 참여자에게 최종 후보를 다시 확인하세요.",
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
