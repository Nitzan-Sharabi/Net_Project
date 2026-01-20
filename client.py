import socket
import json
import select
import sys
import time


HOST = "127.0.0.1"
PORT = 5001
ENC = "utf-8"

# Per-socket receive buffer (newline-delimited JSON).
BUFFERS = {}


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


def wait_for_types(sock, wanted_types):
    """Block until a message type in wanted_types arrives (prints OK/ERR along the way)."""
    while True:
        msg = recv_json(sock)
        if msg is None:
            return None
        t = msg.get("type")
        if t in wanted_types:
            return msg
        if t == "OK" and msg.get("msg"):
            print(f"✅ {msg['msg']}")
        if t == "ERR":
            print(f"❌ {msg.get('msg')}")
            if msg.get("hint"):
                print(f"➡ {msg['hint']}")


def drain_socket(sock):
    """Drain any pending messages (used when returning to lobby)."""
    while True:
        r, _, _ = select.select([sock], [], [], 0)
        if not r:
            return
        msg = recv_json(sock)
        if msg is None:
            return


def print_lobby_help():
    print(
        """
Commands (Lobby):
  LIST              - show available games (WAITING only)
  CREATE 2          - create 2-player game (3x3) and auto-join
  CREATE 3          - create 3-player game (4x4) and auto-join
  JOIN <id>         - join an existing game by id
  INFO              - show this help
  QUIT              - disconnect and exit
""".strip()
    )


def print_game_help(n):
    print(
        f"""
Commands (In Game):
  row col           - make a move (example: 0 2)   [only on your turn]  [valid range: 0..{n-1}]
  00                - shorthand for 0 0           [only on your turn]
  INFO              - show this help
  LEAVE             - leave game and return to lobby (works anytime)
  QUIT              - disconnect and exit
""".strip()
    )


def print_board(board):
    n = len(board)
    print()
    print("   " + " ".join(str(i) for i in range(n)))
    for r in range(n):
        print(f"{r}  " + " ".join(board[r]))
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


def find_turn_player_name(state):
    turn_mark = state.get("turn")
    if not turn_mark:
        return None
    for p in state.get("players", []):
        if p.get("mark") == turn_mark:
            return p.get("name")
    return None


def parse_input(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None

    low = raw.lower()
    if low in ("leave", "quit", "info", "help", "?"):
        return low

    parts = raw.split()
    if len(parts) == 1 and len(parts[0]) == 2 and parts[0].isdigit():
        parts = [parts[0][0], parts[0][1]]

    if len(parts) != 2:
        return None

    try:
        r = int(parts[0])
        c = int(parts[1])
    except Exception:
        return None

    return (r, c)


def timed_input_windows(prompt, sock):
    """Windows: allow socket updates while typing."""
    print(prompt, end="", flush=True)
    buf = ""
    while True:
        r, _, _ = select.select([sock], [], [], 0)
        if r:
            msg = recv_json(sock)
            return None, msg

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


def timed_input_posix(prompt, sock):
    """POSIX: wait for either a socket message or a full line from stdin."""
    print(prompt, end="", flush=True)
    while True:
        r, _, _ = select.select([sock, sys.stdin], [], [], 0.1)
        if sock in r:
            msg = recv_json(sock)
            return None, msg
        if sys.stdin in r:
            line = sys.stdin.readline()
            if line == "":
                return "quit", None
            return line.rstrip("\n"), None


def timed_input(prompt, sock):
    if HAS_MS:
        return timed_input_windows(prompt, sock)
    return timed_input_posix(prompt, sock)


def prompt_action(state, my_name, my_mark, sock, allow_move: bool):
    """Prompt in-game without blocking server updates.

    - If allow_move: accept MOVE/INFO/LEAVE/QUIT
    - Else: accept INFO/LEAVE/QUIT only
    """
    n = state["board_size"]
    status = state.get("status")

    turn_name = find_turn_player_name(state)

    while True:
        if allow_move:
            prompt = f"Your move: {my_name} ({my_mark}). Enter 'row col' (0..{n-1}) or INFO/LEAVE/QUIT: "
        else:
            if status == "WAITING":
                prompt = "Waiting for players... You may type INFO/LEAVE/QUIT: "
            else:
                who = turn_name if turn_name else "the other player"
                prompt = f"It's {who}'s turn. You may type INFO/LEAVE/QUIT: "

        raw, incoming = timed_input(prompt, sock)
        if incoming is not None:
            return ("INCOMING", incoming)

        parsed = parse_input(raw)
        if parsed is None:
            if allow_move:
                print("❌ Invalid format. Example: 0 0 (or shorthand: 00). Try again.")
            else:
                if status == "WAITING":
                    print("❌ Invalid command. Allowed now: INFO / LEAVE / QUIT (game not started yet).")
                else:
                    print("❌ Invalid command. Allowed now: INFO / LEAVE / QUIT.")
            continue

        if parsed in ("info", "help", "?"):
            print_game_help(n)
            continue
        if parsed == "leave":
            return ("LEAVE",)
        if parsed == "quit":
            return ("QUIT",)

        # Move
        if not allow_move:
            if status == "WAITING":
                print("❌ Game not started yet. Allowed: INFO / LEAVE / QUIT.")
            else:
                print("❌ It's not your turn. Only INFO/LEAVE/QUIT are allowed. Type INFO for help.")
            continue

        r, c = parsed
        return ("MOVE", r, c)


def leave_and_wait_ok(sock):
    """Send LEAVE and wait until we get OK (ignore START/WAIT/GAME_UPDATE noise)."""
    send_json(sock, {"type": "LEAVE"})
    while True:
        msg = recv_json(sock)
        if msg is None:
            return None
        t = msg.get("type")
        if t == "OK":
            print(f"✅ {msg.get('msg', '')}".strip())
            return msg
        if t == "ERR":
            print(f"❌ {msg.get('msg')}")
            if msg.get("hint"):
                print(f"➡ {msg['hint']}")
            return msg
        if t == "END":
            res = msg.get("result", {})
            if res.get("msg"):
                print(f"Game ended: {res['msg']}")


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((HOST, PORT))

    msg = recv_json(s)
    if msg and msg.get("msg"):
        print(msg["msg"])

    # Unique name loop
    while True:
        name = input("Enter your name: ").strip()
        if not name:
            name = "player"
        send_json(s, {"type": "HELLO", "name": name})
        ans = wait_for_types(s, {"OK", "ERR"})
        if ans is None:
            print("Disconnected.")
            return
        if ans.get("type") == "OK":
            print(ans.get("msg", ""))
            print("Tip: type INFO to see commands.\n")
            break
        print(f"❌ {ans.get('msg')}")
        if ans.get("hint"):
            print(f"➡ {ans['hint']}")

    in_game = False
    last_state = None
    my_mark = None
    pending_msg = None

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
                send_json(s, {"type": "CREATE", "players": players})
                ans = wait_for_types(s, {"OK", "ERR"})
                if ans is None:
                    print("Disconnected.")
                    break
                if ans.get("type") == "ERR":
                    print(f"❌ {ans.get('msg')}")
                    continue

                game_id = ans.get("game_id")
                if not game_id:
                    print("❌ CREATE failed (no game_id).")
                    continue

                send_json(s, {"type": "JOIN", "game_id": game_id})
                ans2 = wait_for_types(s, {"JOINED", "ERR"})
                if ans2 is None:
                    print("Disconnected.")
                    break
                if ans2.get("type") == "ERR":
                    print(f"❌ {ans2.get('msg')}")
                    continue

                in_game = True
                pending_msg = ans2
                continue

            if op == "JOIN":
                if len(parts) < 2:
                    print("Usage: JOIN <game_id> (Tip: use LIST)")
                    continue

                game_id = parts[1]
                send_json(s, {"type": "JOIN", "game_id": game_id})
                ans = wait_for_types(s, {"JOINED", "ERR"})
                if ans is None:
                    print("Disconnected.")
                    break
                if ans.get("type") == "ERR":
                    print(f"❌ {ans.get('msg')}")
                    continue

                in_game = True
                pending_msg = ans
                continue

            if op == "QUIT":
                send_json(s, {"type": "QUIT"})
                break

            print("Unknown command. Use INFO.")
            continue

        # -------- In game --------
        msg = pending_msg if pending_msg is not None else recv_json(s)
        pending_msg = None

        if msg is None:
            print("Disconnected.")
            break

        mtype = msg.get("type")

        if mtype == "JOINED":
            state = msg.get("state")
            if msg.get("msg"):
                print(f"\nℹ {msg['msg']}")
            if state:
                last_state = state
                you = msg.get("you", {})
                if you.get("mark"):
                    my_mark = you["mark"]
            continue

        if mtype in ("WAIT", "START", "GAME_UPDATE"):
            state = msg.get("state")
            if not state:
                continue
            last_state = state

            if my_mark is None:
                for p in state.get("players", []):
                    if p.get("name") == name:
                        my_mark = p.get("mark")
                        break

            if msg.get("msg"):
                print(f"\nℹ {msg['msg']}")

            turn_name = find_turn_player_name(state)
            turn_mark = state.get("turn")
            print(f"\nGame {state['id']} | status={state['status']} | turn={turn_name} ({turn_mark})")
            print("Players:", state.get("players"))
            print_board(state.get("board"))

            # FLOW FIX: don't prompt during transient WAITING when game is already full
            if state.get("status") == "WAITING":
                try:
                    if len(state.get("players", [])) == int(state.get("max_players")):
                        continue
                except Exception:
                    pass

            if state.get("status") in ("WAITING", "RUNNING"):
                my_turn = (state.get("status") == "RUNNING") and (my_mark is not None) and (turn_mark == my_mark)
                action = prompt_action(state, name, my_mark, s, allow_move=my_turn)

                if action[0] == "INCOMING":
                    pending_msg = action[1]
                    continue

                if action[0] == "LEAVE":
                    leave_and_wait_ok(s)
                    in_game = False
                    last_state = None
                    my_mark = None
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

            if in_game and last_state and my_mark is not None and last_state.get("status") in ("WAITING", "RUNNING"):
                turn_mark = last_state.get("turn")
                my_turn = (last_state.get("status") == "RUNNING") and (turn_mark == my_mark)
                action = prompt_action(last_state, name, my_mark, s, allow_move=my_turn)

                if action[0] == "INCOMING":
                    pending_msg = action[1]
                    continue
                if action[0] == "LEAVE":
                    leave_and_wait_ok(s)
                    in_game = False
                    last_state = None
                    my_mark = None
                    continue
                if action[0] == "QUIT":
                    send_json(s, {"type": "QUIT"})
                    break
                if action[0] == "MOVE":
                    _, r, c = action
                    send_json(s, {"type": "MOVE", "row": r, "col": c})
                    continue
            continue

        if mtype == "END":
            res = msg.get("result", {})
            state = msg.get("state")
            if state:
                print_board(state.get("board"))
            if res.get("msg"):
                print(f"Game ended: {res['msg']}")
            elif res.get("winner"):
                print("🏆 Winner:", res["winner"])
            else:
                print("Game ended.")

            # Return to lobby cleanly
            leave_and_wait_ok(s)
            in_game = False
            last_state = None
            my_mark = None
            continue

        if mtype == "OK":
            if msg.get("msg"):
                print(f"✅ {msg['msg']}")
            continue

        print("[MSG]", msg)

    try:
        BUFFERS.pop(s.fileno(), None)
    except Exception:
        pass
    try:
        s.close()
    except Exception:
        pass

    except KeyboardInterrupt:
        try:
            send_json(s, {"type": "QUIT"})
        except Exception:
            pass
    finally:
        try:
            s.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
