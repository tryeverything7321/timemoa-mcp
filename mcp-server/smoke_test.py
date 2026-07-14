from __future__ import annotations

import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def call(session: ClientSession, name: str, params: dict) -> dict:
    result = await session.call_tool(name, {"params": params})
    if result.isError:
        raise RuntimeError(f"{name} failed: {result.content}")
    if result.structuredContent:
        return result.structuredContent
    if not result.content:
        raise RuntimeError(f"{name} returned no content")
    return json.loads(result.content[0].text)


async def submit(
    session: ClientSession,
    room_code: str,
    participant: str,
    *,
    hard_blocks: list[dict] | None = None,
    avoid: list[dict] | None = None,
    prefer: list[dict] | None = None,
    covers_time_window: bool = True,
) -> dict:
    return await call(session, "submit_participant_preference", {
        "room_code": room_code,
        "participant": participant,
        "hard_blocks": hard_blocks or [],
        "avoid": avoid or [],
        "prefer": prefer or [],
        "covers_time_window": covers_time_window,
    })


async def main() -> None:
    url = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp")
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            expected = {
                "coordinate_schedule",
                "submit_participant_preference",
                "get_coordination_candidates",
            }
            if names != expected:
                raise RuntimeError(f"Unexpected tool surface: {sorted(names)}")
            schema_text = json.dumps([tool.inputSchema for tool in tools.tools])
            assert "covers_time_window" in schema_text
            assert "hard_blocks" in schema_text

            existing_room_code = os.getenv("VERIFY_ROOM_CODE")
            if existing_room_code:
                persisted = await call(session, "get_coordination_candidates", {
                    "room_code": existing_room_code,
                    "limit": 2,
                })
                assert persisted["status"] in {"ready", "needs_input", "blocked"}
                print("PASS persisted room lookup")
                return

            created = await call(session, "coordinate_schedule", {
                "title": "런칭 Go/No-Go 회의",
                "date_start": "2026-07-20",
                "date_end": "2026-07-24",
                "daily_start": "09:00",
                "daily_end": "18:00",
                "timezone": "Asia/Seoul",
                "duration_minutes": 60,
                "limit": 5,
                "participants": [
                    {
                        "name": "지훈",
                        "required": True,
                        "hard_blocks": [{"start": "2026-07-21T09:00", "end": "2026-07-21T12:00"}],
                        "prefer": [{"start": "2026-07-22T13:00", "end": "2026-07-22T18:00"}],
                        "covers_time_window": True,
                    },
                    {
                        "name": "서연",
                        "required": True,
                        "avoid": [{"start": "2026-07-24T09:00", "end": "2026-07-24T18:00"}],
                        "covers_time_window": True,
                    },
                    {
                        "name": "민수",
                        "required": True,
                        "avoid": [{"start": "2026-07-23T09:00", "end": "2026-07-23T12:00"}],
                        "covers_time_window": True,
                    },
                    {"name": "하나", "required": True, "covers_time_window": True},
                ],
            })
            assert created["status"] == "ready"
            room_code = created["room_code"]
            assert len(room_code) >= 20
            assert created["missing_participants"] == []
            assert created["candidates"][0]["start"].startswith("2026-07-22T13:00")
            assert all(candidate["fully_confirmed"] for candidate in created["candidates"])
            assert all(
                not candidate["start"].startswith("2026-07-21T09:")
                and not candidate["start"].startswith("2026-07-21T10:")
                and not candidate["start"].startswith("2026-07-21T11:")
                for candidate in created["candidates"]
            )
            created_text = json.dumps(created, ensure_ascii=False)
            assert "hard_blocks" not in created_text
            assert "2026-07-21T12:00" not in created_text

            missing_room = await call(session, "get_coordination_candidates", {
                "room_code": "this-room-code-does-not-exist",
                "limit": 2,
            })
            assert missing_room["status"] == "not_found"
            assert room_code not in json.dumps(missing_room, ensure_ascii=False)

            invalid = await submit(
                session,
                room_code,
                "지훈",
                hard_blocks=[{"start": "2026-07-19T09:00", "end": "2026-07-19T12:00"}],
            )
            assert invalid["status"] == "invalid_request"

            # Re-submission replaces one participant's snapshot instead of duplicating it.
            repeated = await submit(
                session,
                room_code,
                "지훈",
                hard_blocks=[{"start": "2026-07-21T09:00", "end": "2026-07-21T12:00"}],
                prefer=[{"start": "2026-07-22T13:00", "end": "2026-07-22T18:00"}],
            )
            assert repeated["submitted_count"] == 4

            result = await call(session, "get_coordination_candidates", {
                "room_code": room_code,
                "limit": 5,
            })
            assert result["status"] == "ready"
            assert result["missing_participants"] == []
            assert result["candidates"]
            assert all(candidate["fully_confirmed"] for candidate in result["candidates"])
            assert result["candidates"][0]["start"].startswith("2026-07-22T13:00")
            assert all(
                not candidate["start"].startswith("2026-07-21T09:")
                and not candidate["start"].startswith("2026-07-21T10:")
                and not candidate["start"].startswith("2026-07-21T11:")
                for candidate in result["candidates"]
            )
            result_text = json.dumps(result, ensure_ascii=False)
            assert "hard_blocks" not in result_text
            assert "2026-07-21T12:00" not in result_text

            incomplete = await call(session, "coordinate_schedule", {
                "title": "추가 확인이 필요한 회의",
                "date_start": "2026-07-20",
                "date_end": "2026-07-20",
                "daily_start": "09:00",
                "daily_end": "12:00",
                "timezone": "Asia/Seoul",
                "duration_minutes": 60,
                "participants": [
                    {
                        "name": "가람",
                        "hard_blocks": [{"start": "2026-07-20T09:00", "end": "2026-07-20T10:00"}],
                        "covers_time_window": True,
                    },
                    {"name": "나래"},
                ],
            })
            assert incomplete["status"] == "needs_input"
            assert incomplete["missing_participants"] == ["나래"]
            assert all("나래" in candidate["unknown_participants"] for candidate in incomplete["candidates"])
            completed = await submit(session, incomplete["room_code"], "나래")
            assert completed["remaining_participants"] == []
            completed_result = await call(session, "get_coordination_candidates", {
                "room_code": incomplete["room_code"],
                "limit": 2,
            })
            assert completed_result["status"] == "ready"

            blocked_room = await call(session, "coordinate_schedule", {
                "title": "후보 없음 회의",
                "date_start": "2026-07-20",
                "date_end": "2026-07-20",
                "daily_start": "09:00",
                "daily_end": "10:00",
                "timezone": "Asia/Seoul",
                "duration_minutes": 60,
                "participants": [
                    {
                        "name": "가람",
                        "required": True,
                        "hard_blocks": [{"start": "2026-07-20T09:00", "end": "2026-07-20T10:00"}],
                        "covers_time_window": True,
                    },
                    {"name": "나래", "required": True, "covers_time_window": True},
                ],
            })
            assert blocked_room["status"] == "blocked"
            assert blocked_room["candidates"] == []

            print("PASS submission coordination smoke test")
            print(f"room_code={room_code}")


if __name__ == "__main__":
    asyncio.run(main())
