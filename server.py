import socket
import threading
import json
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

BUFFERS = {}
HOST = "127.0.0.1"
PORT = 5001
ENC = "utf-8"


def send_json(conn: socket.socket, obj: dict) -> None:
    conn.sendall((json.dumps(obj) + "\n").encode(ENC))


def recv_line(conn: socket.socket) -> Optional[str]:
    key = id(conn)
    buf = BUFFERS.get(key, b"")

    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            BUFFERS.pop(key, None)
            return None
        buf += chunk

    line, rest = buf.split(b"\n", 1)
    BUFFERS[key] = rest
    return line.decode(ENC, errors="replace")


def recv_json(conn: socket.socket) -> Optional[dict]:
    line = recv_line(conn)
    if line is None:
        return None
    return json.loads(line)


def err(conn: socket.socket, msg: str, hint: str = "") -> None:
    payload = {"type": "ERR", "msg": msg}
    if hint:
        payload["hint"] = hint
    send_json(conn, payload)


def check_winner_3_in_row(board: List[List[str]]) -> Optional[str]:
    n = len(board)
    target = 3

    def in_bounds(r, c): return 0 <= r < n and 0 <= c < n

    directions = [(0, 1), (1, 0), (1, 1), (1, -1)]
    for r in range(n):
        for c in range(n):
            mark = board[r][c]
            if mark == " ":
                continue
            for dr, dc in directions:
                ok = True
                for k in range(1, target):
                    rr, cc = r + dr * k, c + dc * k
                    if not in_bounds(rr, cc) or board[rr][cc] != mark:
                        ok = False
                        break
                if ok:
                    return mark
    return None


def board_full(board: List[List[str]]) -> bool:
    return all(cell != " " for row in board for cell in row)


@dataclass
class Player:
    conn: socket.socket
    name: str
    mark: str  # "X","O","Δ"


@dataclass
class Game:
    game_id: str
    max_players: int
    board_size: int
    creator: str
    players: List[Player] = field(default_factory=list)
    board: List[List[str]] = field(default_factory=list)
    turn_index: int = 0
    status: str = "WAITING"  # WAITING / RUNNING / FINISHED
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.board = [[" " for _ in range(self.board_size)] for _ in range(self.board_size)]

    def broadcast(self, obj: dict) -> None:
        for p in list(self.players):
            try:
                send_json(p.conn, obj)
            except:
                pass

    def snapshot(self) -> dict:
        return {
            "id": self.game_id,
            "creator": self.creator,
            "players": [{"name": p.name, "mark": p.mark} for p in self.players],
            "max_players": self.max_players,
            "board_size": self.board_size,
            "board": self.board,
            "turn": self.players[self.turn_index].mark if self.players else None,
            "status": self.status
        }


class TicTacToeServer:
    def __init__(self):
        self.games: Dict[str, Game] = {}
        self.games_lock = threading.Lock()

    def list_games(self) -> List[dict]:
        # ✅ show only JOIN-able games (WAITING)
        with self.games_lock:
            out = []
            for g in self.games.values():
                if g.status != "WAITING":
                    continue
                out.append({
                    "id": g.game_id,
                    "players": len(g.players),
                    "max": g.max_players,
                    "status": g.status,
                    "creator": g.creator
                })
            return out

    def create_game(self, max_players: int, creator: str) -> Game:
        board_size = max_players + 1
        game_id = uuid.uuid4().hex[:6].upper()
        g = Game(game_id=game_id, max_players=max_players, board_size=board_size, creator=creator)
        with self.games_lock:
            self.games[game_id] = g
        return g

    def get_game(self, game_id: str) -> Optional[Game]:
        with self.games_lock:
            return self.games.get(game_id)

    def remove_game_if_empty(self, game_id: str) -> None:
        with self.games_lock:
            g = self.games.get(game_id)
            if g and len(g.players) == 0:
                self.games.pop(game_id, None)


server_state = TicTacToeServer()

MARKS_BY_COUNT = {
    2: ["X", "O"],
    3: ["X", "O", "Δ"],
}


def safe_close(conn: socket.socket):
    try:
        conn.close()
    except:
        pass


def remove_player_from_game(g: Game, conn: socket.socket) -> Optional[str]:
    name = None
    new_players = []
    for p in g.players:
        if p.conn is conn:
            name = p.name
        else:
            new_players.append(p)
    g.players = new_players

    if g.players:
        g.turn_index %= len(g.players)
    else:
        g.turn_index = 0

    return name


def client_thread(conn: socket.socket, addr: Tuple[str, int]):
    current_game: Optional[Game] = None
    player: Optional[Player] = None
    client_name: str = "player"

    try:
        send_json(conn, {"type": "WELCOME", "msg": "Connected to server."})

        while True:
            msg = recv_json(conn)
            if msg is None:
                break

            mtype = msg.get("type")

            if mtype == "HELLO":
                client_name = msg.get("name", "player")
                send_json(conn, {"type": "OK", "msg": f"Hello {client_name}."})

            elif mtype == "LIST":
                send_json(conn, {"type": "GAMES", "games": server_state.list_games()})

            elif mtype == "CREATE":
                if current_game is not None:
                    err(conn, "Already in a game.", "Use LEAVE to return to lobby, then CREATE again.")
                    continue

                max_players = int(msg.get("players", 2))
                creator = msg.get("name", client_name)  # ✅ always set creator
                if max_players not in (2, 3):
                    err(conn, "Only 2 or 3 players supported.", "Use: CREATE 2  or  CREATE 3")
                    continue

                g = server_state.create_game(max_players, creator=creator)
                send_json(conn, {"type": "OK", "msg": "Game created.", "game_id": g.game_id})

            elif mtype == "JOIN":
                if current_game is not None:
                    err(conn, "Already in a game.", "Use LEAVE to return to lobby, then JOIN another game.")
                    continue

                game_id = msg.get("game_id")
                name = msg.get("name", client_name)
                g = server_state.get_game(game_id)

                if not g:
                    err(conn, "Game not found.", "Use LIST to get a valid id, then JOIN <id>.")
                    continue

                with g.lock:
                    if g.status != "WAITING":
                        err(conn, "Game already started/finished.", "Use LIST and join a WAITING game.")
                        continue
                    if len(g.players) >= g.max_players:
                        err(conn, "Game is full.", "Create a new game (CREATE 2/3) or JOIN a different one.")
                        continue

                    marks = MARKS_BY_COUNT[g.max_players]
                    mark = marks[len(g.players)]
                    player = Player(conn=conn, name=name, mark=mark)
                    g.players.append(player)
                    current_game = g

                    g.broadcast({"type": "GAME_UPDATE", "msg": f"{name} joined as {mark}.", "state": g.snapshot()})

                    if len(g.players) == g.max_players:
                        g.status = "RUNNING"
                        g.broadcast({"type": "START", "msg": "Game started! X plays first.", "state": g.snapshot()})
                    else:
                        send_json(conn, {"type": "WAIT", "msg": "Waiting for more players...", "state": g.snapshot()})

            elif mtype == "LEAVE":
                # ✅ Works also for FINISHED: just remove player, no extra END
                if not current_game or not player:
                    err(conn, "Not in a game.", "Use LIST/CREATE/JOIN first.")
                    continue

                g = current_game

                with g.lock:
                    left_name = remove_player_from_game(g, conn) or "player"

                    # If RUNNING and someone leaves -> end for remaining players
                    if g.status == "RUNNING" and len(g.players) >= 1:
                        g.status = "FINISHED"
                        g.broadcast({
                            "type": "END",
                            "result": {"winner": None, "msg": f"Player {left_name} left. Game ended."},
                            "state": g.snapshot()
                        })
                    else:
                        # WAITING or FINISHED: just update (optional)
                        g.broadcast({"type": "GAME_UPDATE", "msg": f"{left_name} left the game.", "state": g.snapshot()})

                server_state.remove_game_if_empty(g.game_id)

                current_game = None
                player = None
                send_json(conn, {"type": "OK", "msg": "Left game. You can LIST/CREATE/JOIN again."})

            elif mtype == "MOVE":
                if not current_game or not player:
                    err(conn, "Not in a game.", "JOIN a game first.")
                    continue

                r = int(msg.get("row"))
                c = int(msg.get("col"))
                g = current_game

                with g.lock:
                    if g.status != "RUNNING":
                        err(conn, "Game not running.", "Wait for START or JOIN another game.")
                        continue
                    if not g.players or g.players[g.turn_index].mark != player.mark:
                        err(conn, "Not your turn.", "Wait for your turn.")
                        continue
                    if not (0 <= r < g.board_size and 0 <= c < g.board_size):
                        err(conn, "Out of bounds.", f"Use row/col in range 0..{g.board_size - 1}.")
                        continue
                    if g.board[r][c] != " ":
                        err(conn, "Cell is not empty.", "Choose a different empty cell.")
                        continue

                    g.board[r][c] = player.mark

                    winner = check_winner_3_in_row(g.board)
                    if winner:
                        g.status = "FINISHED"
                        g.broadcast({"type": "END", "result": {"winner": winner}, "state": g.snapshot()})
                    elif board_full(g.board):
                        g.status = "FINISHED"
                        g.broadcast({"type": "END", "result": {"winner": None, "draw": True}, "state": g.snapshot()})
                    else:
                        g.turn_index = (g.turn_index + 1) % len(g.players)
                        g.broadcast({"type": "GAME_UPDATE", "state": g.snapshot()})

            elif mtype == "QUIT":
                send_json(conn, {"type": "OK", "msg": "Bye"})
                break

            else:
                err(conn, f"Unknown type {mtype}", "Use LIST/CREATE/JOIN/MOVE/LEAVE/QUIT.")

    except:
        pass
    finally:
        # cleanup on disconnect (closing window / network drop)
        if current_game and player:
            g = current_game
            with g.lock:
                left_name = remove_player_from_game(g, conn) or "player"
                if g.status == "RUNNING" and len(g.players) >= 1:
                    g.status = "FINISHED"
                    g.broadcast({
                        "type": "END",
                        "result": {"winner": None, "msg": f"Player {left_name} disconnected. Game ended."},
                        "state": g.snapshot()
                    })
                else:
                    g.broadcast({"type": "GAME_UPDATE", "state": g.snapshot()})
            server_state.remove_game_if_empty(g.game_id)

        safe_close(conn)


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, PORT))
    s.listen()
    print(f"[LISTENING] on {HOST}:{PORT}")

    while True:
        conn, addr = s.accept()
        print("[CONNECTED]", addr)
        t = threading.Thread(target=client_thread, args=(conn, addr), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
