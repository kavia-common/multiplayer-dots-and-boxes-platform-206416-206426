from __future__ import annotations

from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.api.db import create_db_engine, init_db, load_room_state, save_room_state
from src.api.game import Edge, Room, add_player, apply_move, make_room, remove_player, request_rematch, room_from_payload, set_ready, start_game
from src.api.ws_manager import RoomConnectionManager


openapi_tags = [
    {"name": "Health", "description": "Service health endpoints."},
    {"name": "Rooms", "description": "Room lifecycle: create/join/leave/ready/start/move/rematch."},
    {"name": "WebSockets", "description": "Real-time room channel for authoritative state updates."},
    {"name": "Docs", "description": "Developer-facing usage helpers."},
]

app = FastAPI(
    title="Dots & Boxes Multiplayer API",
    description=(
        "Backend for multiplayer Dots & Boxes.\n\n"
        "Provides REST endpoints for room/game lifecycle and a WebSocket channel per room for real-time updates.\n\n"
        "WebSocket usage:\n"
        "- Connect to: /ws/rooms/{roomCode}\n"
        "- Server broadcasts authoritative state snapshots and events.\n"
        "- Client may send JSON actions (optional; REST is also supported).\n"
    ),
    version="0.1.0",
    openapi_tags=openapi_tags,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = create_db_engine()
init_db(engine)
ws_manager = RoomConnectionManager()

# In-memory cache of active rooms (authoritative). Persisted snapshot is source-of-truth for reloads.
ROOMS: dict[str, Room] = {}


def _load_room_or_404(room_code: str) -> Room:
    if room_code in ROOMS:
        return ROOMS[room_code]
    payload = load_room_state(engine, room_code)
    if payload is None:
        raise HTTPException(status_code=404, detail="Room not found")
    room = room_from_payload(payload)
    ROOMS[room_code] = room
    return room


def _persist(room: Room) -> None:
    save_room_state(engine, room.room_code, room.to_public())


async def _broadcast_room(room: Room, event: Optional[dict[str, Any]] = None) -> None:
    msg = {"type": "room_state", "room": room.to_public()}
    if event is not None:
        msg["event"] = event
    await ws_manager.broadcast(room.room_code, msg)


class CreateRoomRequest(BaseModel):
    nickname: Optional[str] = Field(default="Player 1", description="Creator nickname.")
    boardSize: int = Field(default=5, ge=2, le=12, description="Board size N (boxes per row/col).")
    maxPlayers: int = Field(default=2, ge=2, le=4, description="Max players (2-4).")


class CreateRoomResponse(BaseModel):
    room: dict[str, Any] = Field(..., description="Authoritative room state.")
    playerId: str = Field(..., description="Player ID for the creator (use in subsequent calls).")


class JoinRoomRequest(BaseModel):
    nickname: Optional[str] = Field(default=None, description="Nickname for joining player.")


class PlayerResponse(BaseModel):
    playerId: str = Field(..., description="Player ID for the joining player.")


class LeaveRoomRequest(BaseModel):
    playerId: str = Field(..., description="Player ID leaving the room.")


class ReadyRequest(BaseModel):
    playerId: str = Field(..., description="Player ID toggling ready.")
    ready: bool = Field(default=True, description="Ready state.")


class StartGameRequest(BaseModel):
    playerId: str = Field(..., description="Host player ID starting the game.")


class MoveRequest(BaseModel):
    playerId: str = Field(..., description="Player ID submitting the move.")
    edge: Edge = Field(..., description="Edge to draw: {r,c,dir} where dir in {'h','v'}.")


class RematchRequest(BaseModel):
    playerId: str = Field(..., description="Player requesting rematch.")


# PUBLIC_INTERFACE
@app.get("/", tags=["Health"], summary="Health check", operation_id="health_check")
def health_check() -> dict[str, str]:
    """Return a simple health status."""
    return {"message": "Healthy"}


# PUBLIC_INTERFACE
@app.get("/docs/ws", tags=["Docs"], summary="WebSocket usage help", operation_id="websocket_usage_help")
def websocket_usage_help() -> dict[str, Any]:
    """
    Developer help for WebSocket usage.

    Returns:
      Connection URL pattern and example messages.
    """
    return {
        "connectTo": "/ws/rooms/{roomCode}",
        "serverBroadcasts": [
            {"type": "room_state", "room": {"roomCode": "ABC123", "status": "playing", "board": {"..." : "..."}}},
            {"type": "room_state", "event": {"eventId": 3, "move": {"playerId": "..."}}, "room": {"...": "..."}},
        ],
        "clientMaySend": [
            {"type": "ping"},
            {"type": "move", "data": {"playerId": "PLAYER_ID", "edge": {"r": 0, "c": 0, "dir": "h"}}},
        ],
        "note": "REST endpoints are supported for all lifecycle actions; WebSocket is primarily for receiving authoritative state.",
    }


# PUBLIC_INTERFACE
@app.post("/rooms", tags=["Rooms"], summary="Create a new room", operation_id="create_room", response_model=CreateRoomResponse)
async def create_room_endpoint(payload: CreateRoomRequest = Body(...)) -> CreateRoomResponse:
    """
    Create a new room and return the creator's playerId plus initial room state.
    """
    room, creator = make_room(payload.nickname or "Player 1", payload.boardSize, payload.maxPlayers)
    ROOMS[room.room_code] = room
    _persist(room)
    await _broadcast_room(room)
    return CreateRoomResponse(room=room.to_public(), playerId=creator["playerId"])


# PUBLIC_INTERFACE
@app.post("/rooms/{room_code}/join", tags=["Rooms"], summary="Join a room", operation_id="join_room", response_model=PlayerResponse)
async def join_room_endpoint(room_code: str, payload: JoinRoomRequest = Body(...)) -> PlayerResponse:
    """
    Join an existing room by room code.
    """
    room = _load_room_or_404(room_code)
    if room.status != "lobby":
        raise HTTPException(status_code=409, detail="Cannot join after game start")
    try:
        joiner = add_player(room, payload.nickname or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    _persist(room)
    await _broadcast_room(room)
    return PlayerResponse(playerId=joiner["playerId"])


# PUBLIC_INTERFACE
@app.post("/rooms/{room_code}/leave", tags=["Rooms"], summary="Leave a room", operation_id="leave_room")
async def leave_room_endpoint(room_code: str, payload: LeaveRoomRequest = Body(...)) -> dict[str, Any]:
    """
    Leave a room. If the host leaves, host ownership is transferred.
    """
    room = _load_room_or_404(room_code)
    remove_player(room, payload.playerId)
    _persist(room)
    await _broadcast_room(room)
    return {"ok": True, "room": room.to_public()}


# PUBLIC_INTERFACE
@app.post("/rooms/{room_code}/ready", tags=["Rooms"], summary="Set ready state", operation_id="ready_up")
async def ready_up_endpoint(room_code: str, payload: ReadyRequest = Body(...)) -> dict[str, Any]:
    """
    Toggle ready state during lobby or for rematch readiness.
    """
    room = _load_room_or_404(room_code)
    try:
        set_ready(room, payload.playerId, payload.ready)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _persist(room)
    await _broadcast_room(room)
    return {"ok": True, "room": room.to_public()}


# PUBLIC_INTERFACE
@app.post("/rooms/{room_code}/start", tags=["Rooms"], summary="Start game", operation_id="start_game")
async def start_game_endpoint(room_code: str, payload: StartGameRequest = Body(...)) -> dict[str, Any]:
    """
    Start the game if requester is host and all players are ready.
    """
    room = _load_room_or_404(room_code)
    try:
        start_game(room, payload.playerId)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _persist(room)
    await _broadcast_room(room)
    return {"ok": True, "room": room.to_public()}


# PUBLIC_INTERFACE
@app.post("/rooms/{room_code}/move", tags=["Rooms"], summary="Submit a move (draw an edge)", operation_id="draw_edge")
async def draw_edge_endpoint(room_code: str, payload: MoveRequest = Body(...)) -> dict[str, Any]:
    """
    Submit a move. Server validates:
    - game must be playing
    - must be player's turn
    - edge must be in bounds and not taken

    Server applies scoring: completing a box grants +1 and an extra turn.
    """
    room = _load_room_or_404(room_code)
    try:
        event = apply_move(room, payload.playerId, payload.edge)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    _persist(room)
    await _broadcast_room(room, event=event)
    return {"ok": True, "event": event, "room": room.to_public()}


# PUBLIC_INTERFACE
@app.post("/rooms/{room_code}/rematch", tags=["Rooms"], summary="Request rematch", operation_id="rematch")
async def rematch_endpoint(room_code: str, payload: RematchRequest = Body(...)) -> dict[str, Any]:
    """
    Request a rematch. When finished and all players request rematch (ready=true),
    the server restarts the game with the same players and board size.
    """
    room = _load_room_or_404(room_code)
    try:
        request_rematch(room, payload.playerId)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    _persist(room)
    await _broadcast_room(room)
    return {"ok": True, "room": room.to_public()}


# PUBLIC_INTERFACE
@app.get("/rooms/{room_code}", tags=["Rooms"], summary="Get room state", operation_id="get_room")
async def get_room(room_code: str) -> dict[str, Any]:
    """
    Fetch the current authoritative room state.
    """
    room = _load_room_or_404(room_code)
    return {"room": room.to_public()}


# PUBLIC_INTERFACE
@app.websocket("/ws/rooms/{room_code}")
async def ws_room(room_code: str, websocket: WebSocket) -> None:
    """
    WebSocket room channel.

    Usage:
      - Connect to /ws/rooms/{roomCode}
      - Server immediately sends a 'room_state' snapshot.
      - Server broadcasts 'room_state' after any lifecycle change or move.
      - Client may optionally send actions:
          {"type":"ping"}
          {"type":"move","data":{"playerId":"...","edge":{"r":0,"c":0,"dir":"h"}}}

    Notes:
      - Server state is authoritative; clients should render received state.
      - REST endpoints remain supported for all actions.
    """
    await ws_manager.connect(room_code, websocket)
    try:
        room = _load_room_or_404(room_code)
        await ws_manager.send(websocket, {"type": "room_state", "room": room.to_public()})

        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")

            if msg_type == "ping":
                await ws_manager.send(websocket, {"type": "pong"})
                continue

            if msg_type == "move":
                data = msg.get("data") or {}
                player_id = data.get("playerId")
                edge = data.get("edge")
                if not isinstance(player_id, str) or not isinstance(edge, dict):
                    await ws_manager.send(websocket, {"type": "error", "message": "Invalid move payload"})
                    continue
                try:
                    # Edge dict is validated by game logic; keep minimal coercion.
                    event = apply_move(room, player_id, edge)  # type: ignore[arg-type]
                    _persist(room)
                    await _broadcast_room(room, event=event)
                except ValueError as e:
                    await ws_manager.send(websocket, {"type": "error", "message": str(e)})
                continue

            await ws_manager.send(websocket, {"type": "error", "message": f"Unknown message type: {msg_type}"})

    except WebSocketDisconnect:
        # normal disconnect
        pass
    finally:
        await ws_manager.disconnect(room_code, websocket)
