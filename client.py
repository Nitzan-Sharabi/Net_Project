import socket
import json
import select
import time

BUFFERS = {}
HOST = "127.0.0.1"
PORT = 5001
ENC = "utf-8"

try:
    import msvcrt  # Windows standard lib
    HAS_MS = True
except Exception:
    HAS_MS = False


def send_json(conn, obj):
    conn.sendall((json.dumps(obj) + "\n").encode(ENC))


def recv_line(conn):
    key = conn.fileno()
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


def recv_json(conn):
    line = recv_line(conn)
    if line is None:
        return None
    return json.loads(line)


def print_lobby_help():
    print("""
Commands (Lobby):
  LIST              - show available games (WAITING only)
  CREATE 2          - create 2-player game (3x3) and auto-join
  CREATE 3          - create 3-player game (4x4) and auto-join
  JOIN <id>         - join an existing game by id
  INFO              - show this help
  QUIT              - disconnect and exit
""".strip())


def print_game_help(n):
    print(f"""
Commands (In Game):
  row col           - make a move (example: 0 2)   [valid range: 0..{n-1}]
  00                - shorthand for 0 0
  INFO              - show this help
  LEAVE             - leave game and return to lobby
  QUIT              - disconnect and exit
""".strip())


def print_board(board):
    n = len(board)
    print()
    header = "   " + " ".join([str(i) for i in range(n)])
    print(header)
    for r in range(n):
        row = " ".join(board[r])
        print(f"{r}  {row}")
    print()


def pretty_print_games(ans):
    games = ans.get("games", [])
    if not games:
        print("No available games. Create one with: CREATE 2 or CREATE 3")
        return
    print("\nAvailable games (WAITING):")
    for g in games:
        creator = g.get("creator", "unknown")
        print(f"  - {g['id']} | players {g['players']}/{g['max']} | status={g['status']} | creator={creator}")
    print()


def wait_for_types(sock, wanted_types):
    while True:
        msg = recv_json(sock)
        if msg is None:
            return None
        t = msg.get("type")
        if t in wanted_types:
            return msg
        # swallow stray OK/ERR nicely
        if t == "OK" and msg.get("msg"):
            print(f"✅ {msg['msg']}")
        if t == "ERR":
            print(f"❌ {msg.get('msg')}")
            if msg.get("hint"):
                print(f"➡ {msg['hint']}")


def drain_socket(sock):
    """Remove any pending messages when we are in lobby to avoid garbage under LIST."""
    while True:
        r, _, _ = select.select([sock], [], [], 0)
        if not r:
            return
        msg = recv_json(sock)
        if msg is None:
            return
        # ignore; could optionally log


def find_turn_player_name(state):
    turn_mark = state.get("turn")
    if not turn_mark:
        return None
    for p in state.get("players", []):
        if p.get("mark") == turn_mark:
            return p.get("name")
    return None


def parse_move_input(raw: str):
    raw = raw.strip()
    if not raw:
        return None

    if raw.lower() in ("leave", "quit", "info", "help", "?"):
        return raw.lower()

    parts = raw.split()
    if len(parts) == 1 and len(parts[0]) == 2 and parts[0].isdigit():
        parts = [parts[0][0], parts[0][1]]

    if len(parts) != 2:
        return None

    try:
        r = int(parts[0])
        c = int(parts[1])
    except ValueError:
        return None

    return (r, c)


def timed_input_windows(prompt, sock, abort_types={"END", "GAME_UPDATE", "START", "WAIT", "ERR"}):
    """
    Windows-only: non-blocking input.
    While user types, we also check socket. If a message arrives (e.g., END), return (None, msg).
    Otherwise return (line, None).
    """
    print(prompt, end="", flush=True)
    buf = ""
    while True:
        # socket ready?
        r, _, _ = select.select([sock], [], [], 0)
        if r:
            msg = recv_json(sock)
            return None, msg

        # keyboard?
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                print()
                return buf, None
            if ch == "\b":
                if buf:
                    buf = buf[:-1]
                    print("\b \b", end="", flush=True)
            else:
                buf += ch
                print(ch, end="", flush=True)

        time.sleep(0.05)


def prompt_move(state, my_name, my_mark, sock):
    n = state["board_size"]
    while True:
        prompt = f"Your move: {my_name} ({my_mark}). Enter 'row col' (0..{n-1}) or INFO/LEAVE/QUIT: "

        if HAS_MS:
            raw, incoming = timed_input_windows(prompt, sock)
            if incoming is not None:
                # we got a message while waiting for input -> abort prompt and let main loop process it
                return ("INCOMING", incoming)
        else:
            raw = input(prompt)

        parsed = parse_move_input(raw)

        if parsed is None:
            print("❌ Invalid format. Example: 0 0 (or shorthand: 00). Try again.")
            continue

        if parsed in ("info", "help", "?"):
            print_game_help(n)
            continue

        if parsed == "leave":
            return ("LEAVE",)
        if parsed == "quit":
            return ("QUIT",)

        r, c = parsed
        return ("MOVE", r, c)


def main():
    name = input("Enter your name: ").strip() or "player"

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((HOST, PORT))

    msg = recv_json(s)
    if msg and msg.get("msg"):
        print(msg["msg"])

    send_json(s, {"type": "HELLO", "name": name})
    msg = recv_json(s)
    if msg and msg.get("msg"):
        print(msg["msg"])
        print("Tip: type INFO to see available commands.\n")

    in_game = False
    last_state = None
    my_mark = None
    pending_game_msg = None

    while True:
        if not in_game:
            drain_socket(s)
            cmd = input("Command [LIST | CREATE 2/3 | JOIN <id> | INFO | QUIT]: ").strip()
            if not cmd:
                continue

            parts = cmd.split()
            op = parts[0].upper()

            if op in ("INFO", "HELP", "?"):
                print_lobby_help()
                continue

            if op == "LIST":
                send_json(s, {"type": "LIST"})
                ans = wait_for_types(s, {"GAMES", "ERR"})
                if ans is None:
                    print("Disconnected.")
                    break
                if ans.get("type") == "GAMES":
                    pretty_print_games(ans)
                continue

            if op == "CREATE":
                players = int(parts[1]) if len(parts) > 1 else 2
                send_json(s, {"type": "CREATE", "players": players, "name": name})

                ans = wait_for_types(s, {"OK", "ERR"})
                if ans is None:
                    print("Disconnected.")
                    break
                if ans.get("type") == "ERR":
                    print(f"❌ {ans.get('msg')}")
                    if ans.get("hint"):
                        print(f"➡ {ans['hint']}")
                    continue

                game_id = ans.get("game_id")
                if not game_id:
                    print("❌ CREATE failed (no game_id). Try LIST.")
                    continue

                # ✅ AUTO-JOIN
                send_json(s, {"type": "JOIN", "game_id": game_id, "name": name})
                ans2 = wait_for_types(s, {"GAME_UPDATE", "WAIT", "START", "ERR"})
                if ans2 is None:
                    print("Disconnected.")
                    break
                if ans2.get("type") == "ERR":
                    print(f"❌ {ans2.get('msg')}")
                    if ans2.get("hint"):
                        print(f"➡ {ans2['hint']}")
                    continue

                in_game = True
                pending_game_msg = ans2
                continue

            if op == "JOIN":
                if len(parts) < 2:
                    print("Usage: JOIN <game_id> (Tip: use LIST)")
                    continue

                game_id = parts[1]
                send_json(s, {"type": "JOIN", "game_id": game_id, "name": name})

                ans = wait_for_types(s, {"GAME_UPDATE", "WAIT", "START", "ERR"})
                if ans is None:
                    print("Disconnected.")
                    break
                if ans.get("type") == "ERR":
                    print(f"❌ {ans.get('msg')}")
                    if ans.get("hint"):
                        print(f"➡ {ans['hint']}")
                    continue

                in_game = True
                pending_game_msg = ans
                continue

            if op == "QUIT":
                send_json(s, {"type": "QUIT"})
                break

            print("Unknown command. Use INFO.")
            continue

        # -------- In game --------
        msg = pending_game_msg if pending_game_msg is not None else recv_json(s)
        pending_game_msg = None

        if msg is None:
            print("Disconnected.")
            break

        mtype = msg.get("type")

        if mtype in ("GAME_UPDATE", "START", "WAIT"):
            state = msg.get("state")
            if not state:
                continue
            last_state = state

            if my_mark is None:
                for p in state["players"]:
                    if p["name"] == name:
                        my_mark = p["mark"]
                        break

            if msg.get("msg"):
                print(f"\nℹ {msg['msg']}")

            turn_name = find_turn_player_name(state)
            turn_mark = state.get("turn")
            print(f"\nGame {state['id']} | status={state['status']} | turn={turn_name} ({turn_mark})")
            print("Players:", state["players"])
            print_board(state["board"])

            my_turn = (turn_mark == my_mark)

            if state["status"] == "RUNNING" and my_turn:
                action = prompt_move(state, name, my_mark, s)

                if action[0] == "INCOMING":
                    # message arrived while user was typing -> handle it now
                    pending_game_msg = action[1]
                    continue

                if action[0] == "LEAVE":
                    send_json(s, {"type": "LEAVE"})
                    continue

                if action[0] == "QUIT":
                    send_json(s, {"type": "QUIT"})
                    break

                if action[0] == "MOVE":
                    _, r, c = action
                    send_json(s, {"type": "MOVE", "row": r, "col": c})
                    continue

            continue

        if mtype == "ERR":
            print(f"\n❌ Error: {msg.get('msg')}")
            if msg.get("hint"):
                print(f"➡ Next: {msg['hint']}")
            continue

        if mtype == "OK":
            if msg.get("msg"):
                print(f"\n✅ {msg['msg']}")
            if "Left game" in (msg.get("msg") or ""):
                in_game = False
                last_state = None
                my_mark = None
            continue

        if mtype == "END":
            state = msg.get("state")
            if state:
                print_board(state["board"])
            res = msg.get("result", {})
            if res.get("winner"):
                print("🏆 Winner:", res["winner"])
            elif res.get("draw"):
                print("🤝 Draw.")
            else:
                print("Game ended:", res.get("msg", ""))

            # auto-leave and wait for OK so lobby won't get garbage
            send_json(s, {"type": "LEAVE"})
            wait_for_types(s, {"OK", "ERR"})

            in_game = False
            last_state = None
            my_mark = None
            continue

        print("[MSG]", msg)

    try:
        BUFFERS.pop(s.fileno(), None)
    except:
        pass
    try:
        s.close()
    except:
        pass


if __name__ == "__main__":
    main()
