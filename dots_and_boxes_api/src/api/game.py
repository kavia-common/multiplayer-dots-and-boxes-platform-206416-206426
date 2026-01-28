from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional, TypedDict


Dir = Literal["h", "v"]


class Edge(TypedDict):
    r: int
    c: int
    dir: Dir


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_room_code(length: int = 6) -> str:
    """Generate a human-friendly room code."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _clamp_board_size(n: Any) -> int:
    try:
        val = int(n)
    except Exception:
        val = 5
    return max(2, min(12, val))


def make_empty_board(board_size: int) -> dict[str, Any]:
    """
    Create an empty authoritative board.
    Conventions match the frontend: edges.h has shape (N+1)xN, edges.v has shape Nx(N+1),
    boxes has shape NxN and stores owner playerId or null.
    """
    n = _clamp_board_size(board_size)
    edges_h = [[None for _ in range(n)] for _ in range(n + 1)]
    edges_v = [[None for _ in range(n + 1)] for _ in range(n)]
    boxes = [[None for _ in range(n)] for _ in range(n)]
    return {"boardSize": n, "edges": {"h": edges_h, "v": edges_v}, "boxes": boxes}


def is_edge_in_bounds(board: dict[str, Any], edge: Edge) -> bool:
    n = int(board["boardSize"])
    if edge.get("dir") not in ("h", "v"):
        return False
    r = edge.get("r")
    c = edge.get("c")
    if not isinstance(r, int) or not isinstance(c, int):
        return False
    if edge["dir"] == "h":
        return 0 <= r <= n and 0 <= c < n
    return 0 <= r < n and 0 <= c <= n


def is_edge_taken(board: dict[str, Any], edge: Edge) -> bool:
    if not is_edge_in_bounds(board, edge):
        return True
    return board["edges"][edge["dir"]][edge["r"]][edge["c"]] is not None


def _box_complete(board: dict[str, Any], r: int, c: int) -> bool:
    edges = board["edges"]
    return (
        edges["h"][r][c] is not None
        and edges["h"][r + 1][c] is not None
        and edges["v"][r][c] is not None
        and edges["v"][r][c + 1] is not None
    )


def apply_edge(board: dict[str, Any], edge: Edge, player_id: str) -> tuple[dict[str, Any], list[dict[str, int]]]:
    """
    Apply an edge if available and return (new_board, completed_boxes).

    completed_boxes: list of {r,c} completed by this move.
    """
    if is_edge_taken(board, edge):
        return board, []

    # Copy-on-write minimal clone (JSON-safe structures).
    n = int(board["boardSize"])
    next_board = {
        "boardSize": n,
        "edges": {
            "h": [row[:] for row in board["edges"]["h"]],
            "v": [row[:] for row in board["edges"]["v"]],
        },
        "boxes": [row[:] for row in board["boxes"]],
    }

    next_board["edges"][edge["dir"]][edge["r"]][edge["c"]] = player_id

    completed: list[dict[str, int]] = []
    candidates: list[tuple[int, int]] = []

    if edge["dir"] == "h":
        if edge["r"] > 0:
            candidates.append((edge["r"] - 1, edge["c"]))
        if edge["r"] < n:
            candidates.append((edge["r"], edge["c"]))
    else:
        if edge["c"] > 0:
            candidates.append((edge["r"], edge["c"] - 1))
        if edge["c"] < n:
            candidates.append((edge["r"], edge["c"]))

    for br, bc in candidates:
        if not (0 <= br < n and 0 <= bc < n):
            continue
        if next_board["boxes"][br][bc] is not None:
            continue
        if _box_complete(next_board, br, bc):
            next_board["boxes"][br][bc] = player_id
            completed.append({"r": br, "c": bc})

    return next_board, completed


def is_board_full(board: dict[str, Any]) -> bool:
    n = int(board["boardSize"])
    # All edges taken implies game over.
    for r in range(n + 1):
        for c in range(n):
            if board["edges"]["h"][r][c] is None:
                return False
    for r in range(n):
        for c in range(n + 1):
            if board["edges"]["v"][r][c] is None:
                return False
    return True


@dataclass
class Room:
    room_code: str
    created_at: str
    status: Literal["lobby", "playing", "finished"]
    host_player_id: str
    board_size: int
    max_players: int
    players: list[dict[str, Any]]
    board: dict[str, Any]
    turn_index: int
    last_event_id: int
    winner: Optional[dict[str, Any]] = None

    def to_public(self) -> dict[str, Any]:
        return {
            "roomCode": self.room_code,
            "createdAt": self.created_at,
            "status": self.status,
            "hostPlayerId": self.host_player_id,
            "boardSize": self.board_size,
            "maxPlayers": self.max_players,
            "players": self.players,
            "board": self.board,
            "turnIndex": self.turn_index,
            "currentPlayerId": self.players[self.turn_index]["playerId"] if self.players else None,
            "lastEventId": self.last_event_id,
            "winner": self.winner,
        }


def make_room(nickname: str, board_size: int, max_players: int) -> tuple[Room, dict[str, Any]]:
    player_id = secrets.token_urlsafe(12)
    room_code = new_room_code()
    board_size_n = _clamp_board_size(board_size)
    max_p = max(2, min(4, int(max_players or 2)))
    players = [
        {"playerId": player_id, "nickname": nickname or "Player 1", "ready": True, "score": 0, "isHost": True}
    ]
    room = Room(
        room_code=room_code,
        created_at=_utcnow_iso(),
        status="lobby",
        host_player_id=player_id,
        board_size=board_size_n,
        max_players=max_p,
        players=players,
        board=make_empty_board(board_size_n),
        turn_index=0,
        last_event_id=0,
        winner=None,
    )
    return room, {"playerId": player_id}


def add_player(room: Room, nickname: str) -> dict[str, Any]:
    if len(room.players) >= room.max_players:
        raise ValueError("Room is full")
    player_id = secrets.token_urlsafe(12)
    room.players.append(
        {"playerId": player_id, "nickname": nickname or f"Player {len(room.players)+1}", "ready": False, "score": 0, "isHost": False}
    )
    return {"playerId": player_id}


def remove_player(room: Room, player_id: str) -> None:
    idx = next((i for i, p in enumerate(room.players) if p["playerId"] == player_id), None)
    if idx is None:
        return
    was_host = room.players[idx].get("isHost") is True
    room.players.pop(idx)

    if room.turn_index >= len(room.players):
        room.turn_index = 0

    # Transfer host if needed.
    if was_host and room.players:
        room.players[0]["isHost"] = True
        room.host_player_id = room.players[0]["playerId"]


def set_ready(room: Room, player_id: str, ready: bool) -> None:
    p = next((p for p in room.players if p["playerId"] == player_id), None)
    if not p:
        raise ValueError("Unknown player")
    p["ready"] = bool(ready)


def start_game(room: Room, requester_id: str) -> None:
    if room.status != "lobby":
        raise ValueError("Game already started")
    if requester_id != room.host_player_id:
        raise ValueError("Only host can start")
    if len(room.players) < 2:
        raise ValueError("Need at least 2 players")
    if not all(p.get("ready") for p in room.players):
        raise ValueError("All players must be ready")

    # Randomize player order for fairness.
    random.shuffle(room.players)
    # Ensure isHost remains with the original host id.
    for p in room.players:
        p["isHost"] = p["playerId"] == room.host_player_id

    room.turn_index = 0
    room.status = "playing"
    room.board = make_empty_board(room.board_size)
    for p in room.players:
        p["score"] = 0
    room.winner = None


def apply_move(room: Room, player_id: str, edge: Edge) -> dict[str, Any]:
    if room.status != "playing":
        raise ValueError("Game not in playing state")
    if not room.players:
        raise ValueError("No players")
    if room.players[room.turn_index]["playerId"] != player_id:
        raise ValueError("Not your turn")

    if not is_edge_in_bounds(room.board, edge):
        raise ValueError("Edge out of bounds")
    if is_edge_taken(room.board, edge):
        raise ValueError("Edge already taken")

    next_board, completed = apply_edge(room.board, edge, player_id)
    room.board = next_board

    scored = len(completed) > 0
    if scored:
        p = next(p for p in room.players if p["playerId"] == player_id)
        p["score"] += len(completed)
        # Player gets another turn if they completed a box.
    else:
        room.turn_index = (room.turn_index + 1) % len(room.players)

    if is_board_full(room.board):
        room.status = "finished"
        # Winner: max score (ties allowed).
        max_score = max(p["score"] for p in room.players)
        winners = [p for p in room.players if p["score"] == max_score]
        room.winner = {
            "winnerPlayerIds": [p["playerId"] for p in winners],
            "maxScore": max_score,
            "isTie": len(winners) > 1,
        }

    room.last_event_id += 1
    return {
        "eventId": room.last_event_id,
        "move": {"playerId": player_id, "edge": edge, "completedBoxes": completed, "scored": scored},
        "room": room.to_public(),
    }


def request_rematch(room: Room, player_id: str) -> None:
    if player_id not in [p["playerId"] for p in room.players]:
        raise ValueError("Unknown player")
    # Mark ready for rematch; if all ready and finished -> restart.
    p = next(p for p in room.players if p["playerId"] == player_id)
    p["ready"] = True
    if room.status == "finished" and all(pp.get("ready") for pp in room.players):
        # reset
        room.status = "playing"
        room.board = make_empty_board(room.board_size)
        for pp in room.players:
            pp["score"] = 0
        room.turn_index = 0
        room.winner = None
        room.last_event_id += 1
        # keep host as is


def room_from_payload(payload: dict[str, Any]) -> Room:
    """Hydrate Room from persisted payload."""
    return Room(
        room_code=payload["roomCode"],
        created_at=payload.get("createdAt") or _utcnow_iso(),
        status=payload["status"],
        host_player_id=payload["hostPlayerId"],
        board_size=int(payload["boardSize"]),
        max_players=int(payload["maxPlayers"]),
        players=payload["players"],
        board=payload["board"],
        turn_index=int(payload["turnIndex"]),
        last_event_id=int(payload.get("lastEventId") or 0),
        winner=payload.get("winner"),
    )
