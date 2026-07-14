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


async def expect_tool_error(session: ClientSession, name: str, params: dict) -> None:
    result = await session.call_tool(name, {"params": params})
    if not result.isError:
        raise RuntimeError(f"{name} unexpectedly succeeded: {result.structuredContent}")


async def submit(
    session: ClientSession,
    coordination_id: str,
    participant: str,
    *,
    hard_blocks: list[dict] | None = None,
    avoid: list[dict] | None = None,
    prefer: list[dict] | None = None,
    covers_time_window: bool = True,
) -> dict:
    return await call(session, "submit_participant_preference", {
        "coordination_id": coordination_id,
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
            tools_by_name = {tool.name: tool for tool in tools.tools}
            schema_text = json.dumps([tool.inputSchema for tool in tools.tools])
            assert "covers_time_window" in schema_text
            assert "hard_blocks" in schema_text
            coordinate_tool = tools_by_name["coordinate_schedule"]
            coordinate_schema = coordinate_tool.inputSchema
            assert set(coordinate_schema["properties"]) == {"params"}
            coordinate_params = coordinate_schema["$defs"]["CoordinateScheduleInput"]
            assert set(coordinate_params["required"]) == {
                "title", "date_start", "date_end", "participants",
            }
            assert coordinate_params["properties"]["limit"]["maximum"] == 3
            assert "timezone" not in coordinate_params["properties"]
            participant_schema = coordinate_schema["$defs"]["ParticipantInput"]
            assert "required" not in participant_schema["properties"]
            assert coordinate_tool.outputSchema is not None
            assert "coordination_id" in coordinate_tool.outputSchema["properties"]

            expected_limit_status = os.getenv("VERIFY_CREATE_LIMIT_STATUS")
            if expected_limit_status:
                request = {
                    "title": "생성 제한 확인",
                    "date_start": "2026-07-20",
                    "date_end": "2026-07-20",
                    "participants": [
                        {"name": "가람", "covers_time_window": True},
                        {"name": "나래", "covers_time_window": True},
                    ],
                }
                first = await call(session, "coordinate_schedule", request)
                second = await call(session, "coordinate_schedule", request)
                assert first["status"] == "ready"
                assert second["status"] == expected_limit_status
                print(f"PASS create limit: {expected_limit_status}")
                return

            existing_coordination_id = os.getenv("VERIFY_COORDINATION_ID")
            if existing_coordination_id:
                persisted = await call(session, "get_coordination_candidates", {
                    "coordination_id": existing_coordination_id,
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
                "duration_minutes": 60,
                "limit": 3,
                "participants": [
                    {
                        "name": "지훈",
                        "hard_blocks": [{"start": "2026-07-21T09:00", "end": "2026-07-21T12:00"}],
                        "prefer": [{"start": "2026-07-22T13:00", "end": "2026-07-22T18:00"}],
                        "covers_time_window": True,
                    },
                    {
                        "name": "서연",
                        "avoid": [{"start": "2026-07-24T09:00", "end": "2026-07-24T18:00"}],
                        "covers_time_window": True,
                    },
                    {
                        "name": "민수",
                        "avoid": [{"start": "2026-07-23T09:00", "end": "2026-07-23T12:00"}],
                        "covers_time_window": True,
                    },
                    {"name": "하나", "covers_time_window": True},
                ],
            })
            assert created["status"] == "ready"
            coordination_id = created["coordination_id"]
            assert len(coordination_id) >= 20
            assert coordination_id not in created["share_message"]
            assert "조율 코드" not in created["share_message"]
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
                "coordination_id": "this-room-code-does-not-exist",
                "limit": 2,
            })
            assert missing_room["status"] == "not_found"
            assert coordination_id not in json.dumps(missing_room, ensure_ascii=False)

            await expect_tool_error(session, "coordinate_schedule", {
                "title": "너무 긴 회의",
                "date_start": "2026-07-20",
                "date_end": "2026-07-20",
                "daily_start": "09:00",
                "daily_end": "10:00",
                "duration_minutes": 120,
                "participants": [{"name": "가람"}, {"name": "나래"}],
            })
            await expect_tool_error(session, "coordinate_schedule", {
                "title": "너무 넓은 범위",
                "date_start": "2026-07-01",
                "date_end": "2026-07-15",
                "participants": [{"name": "가람"}, {"name": "나래"}],
            })
            await expect_tool_error(session, "coordinate_schedule", {
                "title": "시간대 포함 입력",
                "date_start": "2026-07-20",
                "date_end": "2026-07-20",
                "participants": [
                    {
                        "name": "가람",
                        "hard_blocks": [
                            {"start": "2026-07-20T09:00+09:00", "end": "2026-07-20T10:00+09:00"},
                        ],
                    },
                    {"name": "나래"},
                ],
            })

            invalid = await submit(
                session,
                coordination_id,
                "지훈",
                hard_blocks=[{"start": "2026-07-19T09:00", "end": "2026-07-19T12:00"}],
            )
            assert invalid["status"] == "invalid_request"

            unknown_participant = await submit(session, coordination_id, "없는 사람")
            assert unknown_participant["status"] == "participant_not_found"

            # Re-submission replaces one participant's snapshot instead of duplicating it.
            repeated = await submit(
                session,
                coordination_id,
                "지훈",
                hard_blocks=[{"start": "2026-07-21T09:00", "end": "2026-07-21T12:00"}],
                prefer=[{"start": "2026-07-22T13:00", "end": "2026-07-22T18:00"}],
            )
            assert repeated["submitted_count"] == 4

            result = await call(session, "get_coordination_candidates", {
                "coordination_id": coordination_id,
                "limit": 3,
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
            completed = await submit(session, incomplete["coordination_id"], "나래")
            assert completed["remaining_participants"] == []
            completed_result = await call(session, "get_coordination_candidates", {
                "coordination_id": incomplete["coordination_id"],
                "limit": 2,
            })
            assert completed_result["status"] == "ready"

            partial = await call(session, "coordinate_schedule", {
                "title": "부분 정보 회의",
                "date_start": "2026-07-20",
                "date_end": "2026-07-20",
                "daily_start": "09:00",
                "daily_end": "12:00",
                "duration_minutes": 60,
                "participants": [
                    {
                        "name": "가람",
                        "hard_blocks": [{"start": "2026-07-20T09:00", "end": "2026-07-20T10:00"}],
                        "covers_time_window": False,
                    },
                    {"name": "나래", "covers_time_window": True},
                ],
            })
            assert partial["status"] == "needs_input"
            assert partial["missing_participants"] == []
            assert all("가람" in candidate["unknown_participants"] for candidate in partial["candidates"])

            invalid_initial = await call(session, "coordinate_schedule", {
                "title": "범위 오류 회의",
                "date_start": "2026-07-20",
                "date_end": "2026-07-20",
                "daily_start": "09:00",
                "daily_end": "12:00",
                "duration_minutes": 60,
                "participants": [
                    {
                        "name": "가람",
                        "hard_blocks": [{"start": "2026-07-19T09:00", "end": "2026-07-19T10:00"}],
                    },
                    {"name": "나래"},
                ],
            })
            assert invalid_initial["status"] == "invalid_request"
            assert invalid_initial["coordination_id"] is None

            overlap_avoid = await call(session, "coordinate_schedule", {
                "title": "부분 회피 구간 회의",
                "date_start": "2026-07-20",
                "date_end": "2026-07-20",
                "daily_start": "09:00",
                "daily_end": "11:00",
                "duration_minutes": 60,
                "limit": 3,
                "participants": [
                    {
                        "name": "가람",
                        "avoid": [{"start": "2026-07-20T09:30", "end": "2026-07-20T10:00"}],
                        "covers_time_window": True,
                    },
                    {"name": "나래", "covers_time_window": True},
                ],
            })
            assert overlap_avoid["status"] == "ready"
            assert overlap_avoid["candidates"][0]["start"].startswith("2026-07-20T10:00")
            nine_oclock = next(
                candidate for candidate in overlap_avoid["candidates"]
                if candidate["start"].startswith("2026-07-20T09:00")
            )
            assert nine_oclock["avoid_count"] == 1

            partial_avoid = await call(session, "coordinate_schedule", {
                "title": "회피만 확인된 회의",
                "date_start": "2026-07-20",
                "date_end": "2026-07-20",
                "daily_start": "09:00",
                "daily_end": "10:00",
                "duration_minutes": 60,
                "limit": 1,
                "participants": [
                    {
                        "name": "가람",
                        "avoid": [{"start": "2026-07-20T09:30", "end": "2026-07-20T10:00"}],
                        "covers_time_window": False,
                    },
                    {"name": "나래", "covers_time_window": True},
                ],
            })
            assert partial_avoid["status"] == "ready"
            assert partial_avoid["candidates"][0]["available_count"] == 2
            assert partial_avoid["candidates"][0]["avoid_count"] == 1

            blocked_room = await call(session, "coordinate_schedule", {
                "title": "후보 없음 회의",
                "date_start": "2026-07-20",
                "date_end": "2026-07-20",
                "daily_start": "09:00",
                "daily_end": "10:00",
                "duration_minutes": 60,
                "participants": [
                    {
                        "name": "가람",
                        "hard_blocks": [{"start": "2026-07-20T09:00", "end": "2026-07-20T10:00"}],
                        "covers_time_window": True,
                    },
                    {"name": "나래", "covers_time_window": True},
                ],
            })
            assert blocked_room["status"] == "blocked"
            assert blocked_room["candidates"] == []

            print("PASS submission coordination smoke test")
            print(f"coordination_id={coordination_id}")


if __name__ == "__main__":
    asyncio.run(main())
