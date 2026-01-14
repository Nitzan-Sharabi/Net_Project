import socket
import json

BUFFERS = {}
HOST = "127.0.0.1"
PORT = 5001
ENC = "utf-8"


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
  LIST              - show available games
  CREATE 2          - create 2-player game (3x3)
  CREATE 3          - create 3-player game (4x4)
  JOIN <id>         - join a game by id (use LIST to get id)
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


def parse_move_input(raw: str):
    raw = raw.strip()
    if not raw:
        return None

    if raw.lower() in ("leave", "quit", "info", "help", "?"):
        return raw.lower()

    parts = raw.split()

    # allow "00" shorthand
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


def prompt_move(state, my_mark):
    n = state["board_size"]
    while True:
        raw = input(f"Your move ({my_mark}). Enter 'row col' (0..{n-1}) or INFO/LEAVE/QUIT: ")
        parsed = parse_move_input(raw)

        if parsed is None:
            print("❌ Invalid format. Example: 0 0  (or shorthand: 00). Try again.")
            continue

        if parsed in ("info", "help", "?"):
            print_game_help(n)
            continue

        if parsed == "leave":
            return "LEAVE"
        if parsed == "quit":
            return "QUIT"

        r, c = parsed
        return ("MOVE", r, c)


def pretty_print_games(ans):
    games = ans.get("games", [])
    if not games:
        print("No games available. Create one with: CREATE 2  or  CREATE 3")
        return
    print("\nAvailable games:")
    for g in games:
        creator = g.get("creator", "unknown")
        print(f"  - {g['id']} | players {g['players']}/{g['max']} | status={g['status']} | creator={creator}")
    print()


def wait_for_types(sock, wanted_types):
    """
    Read messages until we get one of wanted_types.
    If we get unrelated OK/UPDATE (e.g. LEAVE ack), we print it and keep waiting.
    """
    while True:
        msg = recv_json(sock)
        if msg is None:
            return None
        t = msg.get("type")
        if t in wanted_types:
            return msg

        # handle stray messages politely
        if t == "OK":
            if msg.get("msg"):
                print(f"✅ {msg['msg']}")
        elif t == "ERR":
            print(f"❌ {msg.get('msg')}")
            if msg.get("hint"):
                print(f"➡ {msg['hint']}")
        # otherwise ignore (shouldn't happen often)


def main():
    name = input("Enter your name: ").strip() or "player"

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((HOST, PORT))

    msg = recv_json(s)
    if msg:
        print(msg.get("msg", ""))

    send_json(s, {"type": "HELLO", "name": name})
    msg = recv_json(s)
    if msg:
        print(msg.get("msg", ""))
        print("Tip: type INFO to see available commands.\n")

    in_game = False
    last_state = None
    my_mark = None

    # when JOIN returns a state message, we store it here to process immediately
    pending_game_msg = None

    while True:
        if not in_game:
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

            elif op == "CREATE":
                players = int(parts[1]) if len(parts) > 1 else 2
                send_json(s, {"type": "CREATE", "players": players})

                ans = wait_for_types(s, {"OK", "ERR"})
                if ans is None:
                    print("Disconnected.")
                    break

                if ans.get("type") == "ERR":
                    print(f"❌ {ans.get('msg')}")
                    if ans.get("hint"):
                        print(f"➡ {ans['hint']}")
                    continue

                # OK from CREATE MUST include game_id
                game_id = ans.get("game_id")
                if not game_id:
                    # extremely defensive: if we got some other OK, keep waiting for the real CREATE OK
                    ans2 = wait_for_types(s, {"OK", "ERR"})
                    if ans2 and ans2.get("type") == "OK":
                        game_id = ans2.get("game_id")

                if not game_id:
                    print("❌ Internal: CREATE did not return a game_id. Try LIST and see if the game exists.")
                    continue

                print(f"✅ Game created. (game_id={game_id})")
                print("Next step: JOIN <game_id>")

            elif op == "JOIN":
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

                # only now we enter in_game
                in_game = True
                pending_game_msg = ans

            elif op == "QUIT":
                send_json(s, {"type": "QUIT"})
                break

            else:
                print("Unknown command.")
                print("Use: LIST | CREATE 2/3 | JOIN <id> | INFO | QUIT")
                continue

        # ---------------------------
        # In-game loop
        # ---------------------------
        if in_game:
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

                print(f"\nGame {state['id']} | status={state['status']} | turn={state.get('turn')}")
                print("Players:", state["players"])
                print_board(state["board"])

                my_turn = (state.get("turn") == my_mark)

                if state["status"] == "RUNNING" and my_turn:
                    action = prompt_move(state, my_mark)

                    if action == "LEAVE":
                        send_json(s, {"type": "LEAVE"})
                    elif action == "QUIT":
                        send_json(s, {"type": "QUIT"})
                        break
                    else:
                        _, r, c = action
                        send_json(s, {"type": "MOVE", "row": r, "col": c})

                continue

            elif mtype == "ERR":
                print(f"\n❌ Error: {msg.get('msg')}")
                if msg.get("hint"):
                    print(f"➡ Next: {msg['hint']}")

                # if still our turn -> prompt again immediately
                if last_state and last_state.get("status") == "RUNNING" and my_mark is not None:
                    if last_state.get("turn") == my_mark:
                        action = prompt_move(last_state, my_mark)
                        if action == "LEAVE":
                            send_json(s, {"type": "LEAVE"})
                        elif action == "QUIT":
                            send_json(s, {"type": "QUIT"})
                            break
                        else:
                            _, r, c = action
                            send_json(s, {"type": "MOVE", "row": r, "col": c})
                continue

            elif mtype == "OK":
                # Usually LEAVE ack
                if msg.get("msg"):
                    print(f"\n✅ {msg.get('msg')}")
                if "Left game" in (msg.get("msg") or ""):
                    in_game = False
                    last_state = None
                    my_mark = None
                continue

            elif mtype == "END":
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

                # ✅ auto-leave AND WAIT for its OK, so it won't mess with CREATE
                send_json(s, {"type": "LEAVE"})
                ack = wait_for_types(s, {"OK", "ERR"})
                if ack and ack.get("type") == "OK":
                    # optional: print once, or keep quiet
                    # print(f"✅ {ack.get('msg','')}")
                    pass

                in_game = False
                last_state = None
                my_mark = None
                continue

            else:
                # unknown message – just show and continue
                print("[MSG]", msg)
                continue

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
