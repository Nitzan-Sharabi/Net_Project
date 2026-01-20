import socket
import threading
import json
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


HOST = "127.0.0.1"
PORT = 5001
ENC = "utf-8"

# Per-connection receive buffer (newline-delimited JSON).
BUFFERS: Dict[int, bytes] = {}


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
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"type": "__BAD_JSON__"}


def err(conn: socket.socket, msg: str, hint: str = "") -> None:
    payload = {"type": "ERR", "msg": msg}
    if hint:
        payload["hint"] = hint
    send_json(conn, payload)


def safe_close(conn: socket.socket) -> None:
    try:
        conn.close()
    except Exception:
        pass


def check_winner_3_in_row(board: List[List[str]]) -> Optional[str]:
    n = len(board)
    target = 3

    def in_bounds(r: int, c: int) -> bool:
        return 0 <= r < n and 0 <= c < n

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
    mark: str


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

    def __post_init__(self) -> None:
        self.board = [[" " for _ in range(self.board_size)] for _ in range(self.board_size)]

    def snapshot(self) -> dict:
        return {
            "id": self.game_id,
            "creator": self.creator,
            "players": [{"name": p.name, "mark": p.mark} for p in self.players],
            "max_players": self.max_players,
            "board_size": self.board_size,
            "board": self.board,
            "turn": self.players[self.turn_index].mark if self.players else None,
            "status": self.status,
        }

    def broadcast(self, obj: dict) -> None:
        for p in list(self.players):
            try:
                send_json(p.conn, obj)
            except Exception:
                pass


    def broadcast_collect_dead(self, obj: dict) -> List[socket.socket]:
        dead: List[socket.socket] = []
        for p in list(self.players):
            try:
                send_json(p.conn, obj)
            except Exception:
                dead.append(p.conn)
        return dead

    def broadcast_except(self, obj: dict, except_conn: socket.socket) -> None:
        for p in list(self.players):
            if p.conn is except_conn:
                continue
            try:
                send_json(p.conn, obj)
            except Exception:
                pass


class TicTacToeServer:
    def __init__(self) -> None:
        self.games: Dict[str, Game] = {}
        self.games_lock = threading.Lock()

        # Enforce unique names across active connections.
        self.names_lock = threading.Lock()
        self.active_names: set[str] = set()

        # Connection logging.
        self.conn_lock = threading.Lock()
        self.connected_count = 0

    def log_connect(self, addr: Tuple[str, int]) -> None:
        with self.conn_lock:
            self.connected_count += 1
            print(f"[CONNECTED] {addr} | total={self.connected_count}")

    def log_disconnect(self, addr: Tuple[str, int], name: Optional[str]) -> None:
        with self.conn_lock:
            self.connected_count = max(0, self.connected_count - 1)
            who = name if name else "<unknown>"
            print(f"[DISCONNECTED] {addr} ({who}) | total={self.connected_count}")

    def list_games(self) -> List[dict]:
        # Only JOIN-able games (WAITING).
        with self.games_lock:
            out: List[dict] = []
            for g in self.games.values():
                if g.status != "WAITING":
                    continue
                out.append(
                    {
                        "id": g.game_id,
                        "players": len(g.players),
                        "max": g.max_players,
                        "status": g.status,
                        "creator": g.creator,
                    }
                )
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

    def remove_game(self, game_id: str) -> None:
        with self.games_lock:
            self.games.pop(game_id, None)


server_state = TicTacToeServer()

MARKS_BY_COUNT: Dict[int, List[str]] = {2: ["X", "O"], 3: ["X", "O", "Δ"]}


def remove_player_from_game(g: Game, conn: socket.socket) -> Optional[str]:
    left_name: Optional[str] = None
    new_players: List[Player] = []
    for p in g.players:
        if p.conn is conn:
            left_name = p.name
        else:
            new_players.append(p)
    g.players = new_players
    if g.players:
        g.turn_index %= len(g.players)
    else:
        g.turn_index = 0
    return left_name


def close_game_for_all(g: Game, reason_msg: str) -> None:
    """Close the game and notify all remaining players."""
    g.status = "FINISHED"
    dead = g.broadcast_collect_dead(
        {"type": "END", "result": {"winner": None, "msg": reason_msg}, "state": g.snapshot()}
    )
    handle_dead_conns_after_send(g, dead, already_ending=True)

def handle_dead_conns_after_send(g: Game, dead: List[socket.socket], already_ending: bool = False) -> None:
    if not dead:
        return

    # Remove dead players
    for dc in dead:
        remove_player_from_game(g, dc)

    # Requirement: any disconnect closes the game for everyone
    if (not already_ending) and len(g.players) >= 1 and g.status != "FINISHED":
        close_game_for_all(g, "A player disconnected. Game ended.")



    # If nobody left, remove the game object
    if len(g.players) == 0:
        server_state.remove_game(g.game_id)


def client_thread(conn: socket.socket, addr: Tuple[str, int]) -> None:
    current_game: Optional[Game] = None
    player: Optional[Player] = None

    # Track name reservation for this connection.
    name_registered = False
    client_name: Optional[str] = None

    try:
        send_json(conn, {"type": "WELCOME", "msg": "Connected to server."})

        while True:
            msg = recv_json(conn)
            if msg is None:
                break

            mtype = msg.get("type")

            if mtype == "HELLO":
                if name_registered:
                    err(conn, "Name already set.", "Continue in lobby (LIST/CREATE/JOIN) or QUIT.")
                    continue

                proposed = (msg.get("name") or "").strip()
                if not proposed:
                    err(conn, "Name cannot be empty.", "Enter a non-empty name.")
                    continue

                with server_state.names_lock:
                    if proposed in server_state.active_names:
                        err(conn, "Name already exists.", "Please choose a different name.")
                        continue
                    server_state.active_names.add(proposed)

                client_name = proposed
                name_registered = True
                print(f"[NAME] {addr} -> {client_name}")
                send_json(conn, {"type": "OK", "msg": f"Hello {client_name}."})

            elif mtype == "LIST":
                send_json(conn, {"type": "GAMES", "games": server_state.list_games()})

            elif mtype == "CREATE":
                if not name_registered or not client_name:
                    err(conn, "You must set a unique name first.", "Send HELLO with your chosen name.")
                    continue
                if current_game is not None:
                    err(conn, "Already in a game.", "Use LEAVE to return to lobby, then CREATE again.")
                    continue

                max_players = int(msg.get("players", 2))
                if max_players not in (2, 3):
                    err(conn, "Only 2 or 3 players supported.", "Use: CREATE 2  or  CREATE 3")
                    continue

                g = server_state.create_game(max_players, creator=client_name)
                send_json(conn, {"type": "OK", "msg": "Game created.", "game_id": g.game_id})

            elif mtype == "JOIN":
                if not name_registered or not client_name:
                    err(conn, "You must set a unique name first.", "Send HELLO with your chosen name.")
                    continue
                if current_game is not None:
                    err(conn, "Already in a game.", "Use LEAVE to return to lobby, then JOIN another game.")
                    continue

                game_id = msg.get("game_id")
                if not game_id:
                    err(conn, "Missing game_id.", "Use JOIN <id> (Tip: use LIST).")
                    continue

                g = server_state.get_game(game_id)
                if not g:
                    err(conn, "Game not found.", "Use LIST to get a valid id.")
                    continue

                with g.lock:
                    if g.status != "WAITING":
                        err(conn, "Game already started/finished.", "Use LIST to find a WAITING game.")
                        continue
                    if len(g.players) >= g.max_players:
                        err(conn, "Game is full.", "Use LIST and join another game.")
                        continue

                    # Assign mark by join order.
                    mark = MARKS_BY_COUNT[g.max_players][len(g.players)]
                    player = Player(conn=conn, name=client_name, mark=mark)
                    g.players.append(player)
                    current_game = g

                    # IMPORTANT FLOW FIX:
                    # If the game becomes full now, switch to RUNNING BEFORE sending state to clients.
                    is_full = (len(g.players) == g.max_players)
                    if is_full:
                        g.status = "RUNNING"
                        g.turn_index = 0

                    snap = g.snapshot()

                    send_json(
                        conn,
                        {
                            "type": "JOINED",
                            "msg": f"Joined game {g.game_id} as {mark}.",
                            "you": {"name": client_name, "mark": mark},
                            "state": snap,
                        },
                    )

                    g.broadcast_except(
                        {"type": "GAME_UPDATE", "msg": f"{client_name} joined as {mark}.", "state": snap},
                        except_conn=conn,
                    )

                    if is_full:
                        dead = g.broadcast_collect_dead({"type": "START", "msg": "Game started! X plays first.", "state": snap})
                        handle_dead_conns_after_send(g, dead)
                    else:
                        dead = g.broadcast_collect_dead({"type": "WAIT", "msg": "Waiting for more players...", "state": snap})
                        handle_dead_conns_after_send(g, dead)

            elif mtype == "INFO":
                send_json(conn, {"type": "OK", "msg": "Use client-side INFO for commands."})

            elif mtype == "LEAVE":
                if not current_game or not player:
                    err(conn, "Not in a game.", "Use LIST/CREATE/JOIN first.")
                    continue

                g = current_game
                with g.lock:
                    left_name = remove_player_from_game(g, conn) or "player"

                    # Requirement: leaving closes the game for everyone.
                    if g.status in ("WAITING", "RUNNING") and len(g.players) >= 1:
                        close_game_for_all(g, f"Player {left_name} left. Game ended.")
                    else:
                        g.status = "FINISHED"

                if len(g.players) == 0:
                    server_state.remove_game(g.game_id)

                current_game = None
                player = None
                send_json(conn, {"type": "OK", "msg": "Left game. You can LIST/CREATE/JOIN again."})

            elif mtype == "MOVE":
                if not current_game or not player:
                    err(conn, "Not in a game.", "JOIN a game first.")
                    continue

                g = current_game
                with g.lock:
                    if g.status != "RUNNING":
                        err(conn, "Game not running.", "Wait for START or JOIN another game.")
                        continue

                    # Turn check.
                    if not g.players or g.players[g.turn_index].mark != player.mark:
                        err(conn, "Not your turn.", "Allowed while waiting: INFO or LEAVE (or QUIT).")
                        continue

                    r = int(msg.get("row"))
                    c = int(msg.get("col"))

                    if not (0 <= r < g.board_size and 0 <= c < g.board_size):
                        err(conn, "Out of bounds.", f"Use row/col in range 0..{g.board_size - 1}.")
                        continue
                    if g.board[r][c] != " ":
                        err(conn, "Cell is not empty.", "Choose a different empty cell.")
                        continue

                    g.board[r][c] = player.mark

                    winner = check_winner_3_in_row(g.board)
                    if winner:
                        close_game_for_all(g, f"Winner: {winner}")
                    elif board_full(g.board):
                        close_game_for_all(g, "Draw.")
                    else:
                        g.turn_index = (g.turn_index + 1) % len(g.players)
                        dead = g.broadcast_collect_dead({"type": "GAME_UPDATE", "state": g.snapshot()})
                        handle_dead_conns_after_send(g, dead)

                if g.status == "FINISHED" and len(g.players) == 0:
                    server_state.remove_game(g.game_id)

            elif mtype == "QUIT":
                send_json(conn, {"type": "OK", "msg": "Bye"})
                break

            elif mtype == "__BAD_JSON__":
                err(conn, "Bad message format (invalid JSON).", "Try again.")
                continue

            else:
                err(conn, f"Unknown type {mtype}", "Use LIST/CREATE/JOIN/MOVE/LEAVE/QUIT.")

    except Exception as e:
        print(f"[ERROR] {addr} ({client_name}): {e!r}")

    finally:
        # If connection dies while in a game: close game for everyone (same semantics as LEAVE).
        if current_game and player:
            g = current_game
            with g.lock:
                left_name = remove_player_from_game(g, conn) or "player"
                if g.status in ("WAITING", "RUNNING") and len(g.players) >= 1:
                    close_game_for_all(g, f"Player {left_name} disconnected. Game ended.")
                else:
                    g.status = "FINISHED"
            if len(g.players) == 0:
                server_state.remove_game(g.game_id)

        # Release name reservation.
        if name_registered and client_name:
            with server_state.names_lock:
                server_state.active_names.discard(client_name)

        safe_close(conn)
        server_state.log_disconnect(addr, client_name)


def main() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
    s.bind((HOST, PORT))
    s.listen()
    print(f"[LISTENING] on {HOST}:{PORT}")

    while True:
        conn, addr = s.accept()
        server_state.log_connect(addr)
        t = threading.Thread(target=client_thread, args=(conn, addr), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
