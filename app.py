import random
import string
import uuid
import os
from collections import defaultdict

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
from roles import BUILTIN_ROLES, DEFAULT_ROLE

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-secret-before-production"
socketio = SocketIO(app, cors_allowed_origins="*")

# Prototype-only storage. Replace this with Redis/Postgres for persistent, multi-process hosting.
rooms = {}
connections = {}  # sid -> {room, device_id, kind}

def code():
    while True:
        value = "".join(random.choices(string.ascii_uppercase, k=4))
        if value not in rooms:
            return value


def new_room(room_code):
    return {
        "code": room_code, "name": f"Grimoire {room_code}", "phase": "LOBBY", "players": {}, "storyteller_sid": None,
        "storytellers": set(), "night_queue": [], "night_index": -1,
        "nomination": None, "votes": {}, "log": ["Room created. Day begins."],
        "night_number": 0, "last_executed": None, "effects": {"protected": set(), "bodyguard": {}, "blocked": set(), "fails_tomorrow": set()},
        "deck": {"seat_count": 0, "role_ids": [], "preview_role_ids": []}, "chat": [], "painter_questions": [], "ready_for_vote": set(), "night_proceed_ready": None,
    }


def serialize_player(player, admin=False, viewer_id=None):
    result = {
        "id": player["id"], "name": player["name"], "alive": player["alive"],
        "drunk": player["drunk"], "poisoned": player["poisoned"],
        "has_ghost_vote": player["has_ghost_vote"],
        "spectator": player.get("spectator", False),
        "disconnected": player.get("disconnected", False),
        "role_finalized": player.get("role_finalized", False),
    }
    if admin or (player["id"] == viewer_id and not player.get("spectator", False)):
        # The Drunk sees their decoy Townsfolk token, never their true role.
        result["role"] = player.get("shown_role", player["role"])
    return result


def room_payload(room, kind, device_id):
    admin = kind == "storyteller"
    payload = {
        "code": room["code"], "phase": room["phase"], "is_storyteller": admin,
        "room_name": room["name"], "role_catalog": list(BUILTIN_ROLES.values()),
        "players": [serialize_player(p, admin, device_id) for p in room["players"].values()],
        "deck": room["deck"],
        "painter_questions": room["painter_questions"] if admin else [],
        "night_queue": room["night_queue"] if admin else [],
        "night_index": room["night_index"] if admin else -1,
        "nomination": room["nomination"], "votes": room["votes"],
        "ready_for_vote": {"count": len(room["ready_for_vote"]), "needed": sum(p["alive"] and not p.get("spectator", False) for p in room["players"].values()), "you_are_ready": device_id in room["ready_for_vote"]},
        "log": room["log"][-8:] if admin else [],
    }
    return payload


def role_of(player):
    return player["role"]


def active(player):
    # A hidden Zombuul remains active after its first apparent death.
    return not player.get("spectator", False) and (player["alive"] or player.get("zombuul_hidden_alive", False))


def wakes_at_night(role):
    """Passive roles (such as Painter) do not enter the automatic night queue."""
    return role.get("night_order", 0) > 0 and (role.get("targets", 0) > 0 or role.get("info", False))


def information(room, actor, truth, alternatives=None):
    """Information interceptor: drunk/poisoned/glitched players always get plausible false data."""
    if actor["drunk"] or actor["poisoned"] or actor["id"] in room["effects"]["fails_tomorrow"]:
        if alternatives:
            return random.choice(alternatives)
        return "Your information is unclear: no reliable result."
    return truth


def private_result(player_id, result):
    for sid, conn in connections.items():
        if conn["kind"] == "player" and conn["device_id"] == player_id:
            socketio.emit("night_result", {"text": result}, to=sid)


def kill(room, target, cause="night"):
    """Resolve death and all automatic lifecycle replacements in one authoritative place."""
    if not target or not active(target):
        return False
    if target["id"] in room["effects"]["protected"] and cause == "night":
        guard_id = room["effects"]["bodyguard"].get(target["id"])
        if guard_id and active(room["players"].get(guard_id)):
            return kill(room, room["players"][guard_id], "bodyguard")
        room["log"].append(f"{target['name']} was protected from a night death.")
        return False
    if role_of(target)["id"] == "zombuul" and not target.get("zombuul_first_death_used"):
        target["zombuul_first_death_used"] = True
        target["alive"] = False
        target["zombuul_hidden_alive"] = True
        room["log"].append(f"{target['name']} appears dead.")
        return True
    target["alive"] = False
    target["has_ability"] = False
    target["has_ghost_vote"] = True
    room["log"].append(f"{target['name']} died ({cause}).")
    if role_of(target)["id"] == "sweetheart":
        candidates = [p for p in room["players"].values() if active(p) and p["id"] != target["id"]]
        if candidates:
            victim = random.choice(candidates); victim["poisoned"] = True
            room["log"].append(f"Sweetheart death poisoned {victim['name']}.")
    return True


def broadcast(room):
    for sid, conn in list(connections.items()):
        if conn["room"] == room["code"]:
            socketio.emit("room_state", room_payload(room, conn["kind"], conn["device_id"]), to=sid)


def broadcast_chat(room):
    """Send only public messages, a user's own whispers, and Storyteller whispers."""
    for sid, conn in connections.items():
        if conn["room"] != room["code"]:
            continue
        visible = [m for m in room["chat"] if not m["private"] or conn["kind"] == "storyteller" or conn["device_id"] in (m["sender_id"], m["recipient_id"])]
        socketio.emit("chat_history", visible[-80:], to=sid)


def error(message):
    emit("app_error", {"message": message})


def current_room(required_kind=None):
    conn = connections.get(request.sid)
    if not conn or conn["room"] not in rooms:
        error("Join a room first.")
        return None, None
    if required_kind and conn["kind"] != required_kind:
        error("Only the Storyteller can do that.")
        return None, None
    return rooms[conn["room"]], conn


@app.route("/")
@app.route("/room/<room_code>")
def index(room_code=None):
    return render_template(
        "index.html",
        room_code=(room_code or "").upper(),
        # Empty by default: the browser then connects to the same Render URL
        # that served this page. Set SOCKET_URL only for a separately hosted UI.
        socket_url=os.environ.get("SOCKET_URL", ""),
    )


@socketio.on("create_room")
def create_room(data):
    room_code = code()
    rooms[room_code] = new_room(room_code)
    device_id = data.get("device_id") or str(uuid.uuid4())
    rooms[room_code]["storytellers"].add(device_id)
    rooms[room_code]["storyteller_sid"] = request.sid
    join_room(room_code)
    connections[request.sid] = {"room": room_code, "device_id": device_id, "kind": "storyteller"}
    emit("joined", {"room_code": room_code, "kind": "storyteller"})
    broadcast(rooms[room_code])
    broadcast_chat(rooms[room_code])


@socketio.on("join_room")
def join_game(data):
    room_code = str(data.get("room_code", "")).upper().strip()
    name = str(data.get("name", "")).strip()[:24]
    device_id = data.get("device_id") or str(uuid.uuid4())
    if room_code not in rooms:
        return error("That room code does not exist.")
    if not name:
        return error("Enter a player name.")
    room = rooms[room_code]
    player = room["players"].get(device_id)
    if not player:
        player = {"id": device_id, "name": name, "alive": True, "drunk": False,
                  "poisoned": False, "has_ghost_vote": False, "has_ability": True,
                  "target_history": [], "spectator": False, "role_finalized": False, "role": dict(DEFAULT_ROLE)}
        room["players"][device_id] = player
        room["log"].append(f"{name} joined the game.")
    else:
        player["name"] = name
        player["disconnected"] = False
    join_room(room_code)
    connections[request.sid] = {"room": room_code, "device_id": device_id, "kind": "player"}
    emit("joined", {"room_code": room_code, "kind": "player"})
    broadcast(room)
    broadcast_chat(room)


@socketio.on("resume_room")
def resume_room(data):
    """Restore a browser's room identity after a page refresh."""
    room_code = str(data.get("room_code", "")).upper().strip()
    device_id = data.get("device_id")
    room = rooms.get(room_code)
    if not room or not device_id:
        return error("This room is no longer available.")
    if device_id in room["storytellers"]:
        kind = "storyteller"
        room["storyteller_sid"] = request.sid
    elif device_id in room["players"]:
        kind = "player"
        room["players"][device_id]["disconnected"] = False
    else:
        return error("This device has not joined that room.")
    join_room(room_code)
    connections[request.sid] = {"room": room_code, "device_id": device_id, "kind": kind}
    emit("joined", {"room_code": room_code, "kind": kind, "resumed": True})
    broadcast(room)
    broadcast_chat(room)


@socketio.on("heartbeat")
def heartbeat():
    """Application-level activity during an active room keeps hosted services warm."""
    room, conn = current_room()
    if not room:
        return
    if conn["kind"] == "player":
        player = room["players"].get(conn["device_id"])
        if player:
            player["disconnected"] = False
    emit("heartbeat_ack", {"room_code": room["code"]})


@socketio.on("configure_deck")
def configure_deck(data):
    room, _ = current_room("storyteller")
    if not room or room["phase"] != "DECK_BUILD":
        if room: error("The deck can only be changed during deck building.")
        return
    try:
        seat_count = int(data.get("seat_count", 0))
    except (ValueError, TypeError):
        return error("Choose a valid number of seats.")
    role_ids = list(data.get("role_ids", []))
    available = set(BUILTIN_ROLES)
    participant_count = sum(not p.get("spectator", False) for p in room["players"].values())
    if not participant_count <= seat_count <= len(BUILTIN_ROLES) or len(role_ids) != seat_count or any(role_id not in available for role_id in role_ids):
        return error("Select at least one card per joined player, using valid role cards.")
    room["deck"] = {"seat_count": seat_count, "role_ids": role_ids, "preview_role_ids": role_ids}
    room["log"].append(f"Deck configured with {seat_count} cards.")
    broadcast(room)


@socketio.on("preview_deck")
def preview_deck(data):
    room, _ = current_room("storyteller")
    if not room or room["phase"] != "DECK_BUILD":
        return
    role_ids = list(data.get("role_ids", []))[:len(BUILTIN_ROLES)]
    if any(role_id not in BUILTIN_ROLES for role_id in role_ids):
        return error("Deck preview contains an invalid role.")
    room["deck"]["preview_role_ids"] = role_ids
    broadcast(room)


@socketio.on("set_deck_size")
def set_deck_size(data):
    room, _ = current_room("storyteller")
    if not room or room["phase"] != "DECK_BUILD":
        return
    try:
        seat_count = int(data.get("seat_count", 0))
    except (ValueError, TypeError):
        return error("Choose a valid card count.")
    participants = sum(not p.get("spectator", False) for p in room["players"].values())
    if not participants <= seat_count <= len(BUILTIN_ROLES):
        return error("The deck needs at least one card per active player.")
    room["deck"]["seat_count"] = seat_count
    room["deck"]["preview_role_ids"] = room["deck"]["preview_role_ids"][:seat_count]
    broadcast(room)


@socketio.on("deal_roles")
def deal_roles():
    room, _ = current_room("storyteller")
    if not room:
        return
    return error("Roles are assigned privately by the Storyteller after the deck is locked.")


@socketio.on("send_chat")
def send_chat(data):
    room, conn = current_room()
    if not room:
        return
    text = str(data.get("text", "")).strip()[:500]
    recipient_id = data.get("recipient_id") or None
    if not text:
        return error("Write a message first.")
    if conn["kind"] == "storyteller":
        sender_name = "Storyteller"
        if recipient_id and recipient_id not in room["players"]: return error("Recipient not found.")
    else:
        sender = room["players"].get(conn["device_id"])
        if not sender: return error("Player not found.")
        sender_name = sender["name"]
        # Players may whisper only to the Storyteller, never leak private player messages.
        if recipient_id not in (None, "storyteller"): return error("Players can privately message only the Storyteller.")
        if room["phase"] == "NIGHT" and not recipient_id:
            return error("At night, players may chat only privately with the Storyteller.")
    room["chat"].append({"id": str(uuid.uuid4()), "sender_id": conn["device_id"], "sender_name": sender_name,
                         "recipient_id": recipient_id, "text": text, "private": bool(recipient_id)})
    broadcast_chat(room)


@socketio.on("open_deck_builder")
def open_deck_builder():
    room, _ = current_room("storyteller")
    if not room: return
    room["phase"] = "DECK_BUILD"
    room["log"].append("The Storyteller opened deck building for the town to review.")
    broadcast(room)
    socketio.emit("phase_announcement", {"title": "The Grimoire Opens", "subtitle": "The Storyteller is building the deck."}, room=room["code"])


@socketio.on("lock_deck")
def lock_deck():
    room, _ = current_room("storyteller")
    if not room: return
    participant_count = sum(not p.get("spectator", False) for p in room["players"].values())
    preview = room["deck"].get("preview_role_ids", [])
    if len(preview) != room["deck"]["seat_count"] or room["deck"]["seat_count"] != participant_count:
        return error("Build exactly one card per active player before locking the deck.")
    room["deck"]["role_ids"] = list(preview)
    room["phase"] = "ROLE_ASSIGN"
    room["log"].append("Deck locked. The Storyteller is assigning secret roles.")
    broadcast(room)
    socketio.emit("phase_announcement", {"title": "Roles Are Being Assigned", "subtitle": "Wait for the Storyteller to begin."}, room=room["code"])


@socketio.on("start_game")
def start_game():
    room, _ = current_room("storyteller")
    if not room: return
    if room["phase"] != "ROLE_ASSIGN": return error("Assign roles before starting the game.")
    participants = [p for p in room["players"].values() if not p.get("spectator", False)]
    if not participants or any(p["role"]["id"] == "villager" or not p.get("role_finalized") for p in participants):
        return error("Assign and finalize a built-in role for every player before starting.")
    room["phase"] = "DAY_TALK"
    room["log"].append("The game begins.")
    broadcast(room)
    socketio.emit("phase_announcement", {"title": "The Game Begins", "subtitle": "Day breaks over the town."}, room=room["code"])


@socketio.on("play_again")
def play_again():
    room, _ = current_room("storyteller")
    if not room or room["phase"] in ("LOBBY", "DECK_BUILD", "ROLE_ASSIGN"):
        return error("Start a game before resetting it for another round.")
    room["phase"] = "LOBBY"
    room["deck"] = {"seat_count": 0, "role_ids": [], "preview_role_ids": []}
    room["night_queue"], room["night_index"], room["night_number"] = [], -1, 0
    room["nomination"], room["votes"], room["ready_for_vote"] = None, {}, set()
    room["last_executed"] = None
    room["effects"] = {"protected": set(), "bodyguard": {}, "blocked": set(), "fails_tomorrow": set()}
    room["painter_questions"], room["chat"] = [], []
    for player in room["players"].values():
        player.update({"alive": True, "drunk": False, "poisoned": False, "has_ghost_vote": False,
                       "has_ability": True, "target_history": [], "role_finalized": False, "role": dict(DEFAULT_ROLE)})
        for key in ("shown_role", "painter_question_used", "zombuul_hidden_alive", "zombuul_first_death_used", "poison_expires", "drunk_expires"):
            player.pop(key, None)
    room["log"] = ["A new game is ready. The town is gathering again."]
    broadcast(room)
    broadcast_chat(room)
    socketio.emit("phase_announcement", {"title": "A New Tale Begins", "subtitle": "The room has returned to the lobby."}, room=room["code"])


@socketio.on("set_spectator")
def set_spectator(data):
    room, _ = current_room("storyteller")
    if not room:
        return
    player = room["players"].get(data.get("player_id"))
    if not player: return error("Player not found.")
    player["spectator"] = not player.get("spectator", False)
    player["role"] = dict(DEFAULT_ROLE)
    room["log"].append(f"{player['name']} is now {'a spectator' if player['spectator'] else 'a player'}.")
    broadcast(room)


@socketio.on("kick_player")
def kick_player(data):
    room, _ = current_room("storyteller")
    if not room:
        return
    player_id = data.get("player_id")
    player = room["players"].pop(player_id, None)
    if not player: return error("Player not found.")
    for sid, conn in list(connections.items()):
        if conn["room"] == room["code"] and conn["device_id"] == player_id:
            socketio.emit("kicked", {"message": "The Storyteller removed you from this room."}, to=sid)
            connections.pop(sid, None)
    room["log"].append(f"{player['name']} was removed from the lobby.")
    broadcast(room)


@socketio.on("rename_self")
def rename_self(data):
    room, conn = current_room()
    if not room or conn["kind"] != "player":
        return error("Only players can rename their seat.")
    name = str(data.get("name", "")).strip()[:24]
    if not name:
        return error("Enter a name.")
    room["players"][conn["device_id"]]["name"] = name
    room["log"].append(f"A player is now known as {name}.")
    broadcast(room)


@socketio.on("update_room_settings")
def update_room_settings(data):
    room, _ = current_room("storyteller")
    if not room:
        return
    name = str(data.get("room_name", "")).strip()[:40]
    if not name:
        return error("Give the grimoire a name.")
    room["name"] = name
    room["log"].append(f"Grimoire renamed to {name}.")
    broadcast(room)


@socketio.on("ask_painter_question")
def ask_painter_question(data):
    room, conn = current_room()
    if not room or conn["kind"] != "player":
        return error("Only a Painter can ask this question.")
    player = room["players"].get(conn["device_id"])
    question = str(data.get("question", "")).strip()[:300]
    if not player or player["role"]["id"] != "painter" or player.get("painter_question_used"):
        return error("Your Painter question has already been used, or you are not the Painter.")
    if not question:
        return error("Write a yes-or-no question.")
    player["painter_question_used"] = True
    room["painter_questions"].append({"id": str(uuid.uuid4()), "player_id": player["id"], "player_name": player["name"], "question": question})
    room["log"].append(f"The Painter asked a private question.")
    broadcast(room)


@socketio.on("answer_painter_question")
def answer_painter_question(data):
    room, _ = current_room("storyteller")
    if not room:
        return
    question = next((q for q in room["painter_questions"] if q["id"] == data.get("question_id") and "answer" not in q), None)
    answer = data.get("answer")
    if not question or answer not in ("Yes", "No"):
        return error("Choose an unanswered Painter question and a Yes or No answer.")
    question["answer"] = answer
    private_result(question["player_id"], f"Painter answer: {answer}.")
    room["log"].append(f"The Painter received an answer.")
    broadcast(room)


@socketio.on("toggle_phase")
def toggle_phase():
    room, _ = current_room("storyteller")
    if not room:
        return
    if room["phase"] not in ("DAY_TALK", "DAY_NOMINATION", "NIGHT"):
        return error("Use the lobby controls to set up the game first.")
    if room["phase"] == "DAY_TALK":
        return error("Wait until all living players are ready before opening voting.")
    room["phase"] = "NIGHT" if room["phase"] != "NIGHT" else "DAY_TALK"
    room["nomination"], room["votes"], room["ready_for_vote"] = None, {}, set()
    if room["phase"] == "NIGHT":
        room["night_number"] += 1
        # Temporary Poisoner status lasts through the following day, then clears at next dusk.
        for p in room["players"].values():
            if p.pop("poison_expires", False): p["poisoned"] = False
            if p.pop("drunk_expires", False): p["drunk"] = False
        room["effects"] = {"protected": set(), "bodyguard": {}, "blocked": set(), "fails_tomorrow": set()}
        room["night_queue"] = [p["id"] for p in sorted(room["players"].values(), key=lambda p: p["role"].get("night_order", 999))
                               if active(p) and p.get("has_ability", True) and wakes_at_night(p["role"])
                               and (not p["role"].get("first_night") or room["night_number"] == 1)]
        room["night_index"] = -1
        room["log"].append("Night falls. Build the wake-up order.")
        socketio.emit("phase_announcement", {"title": "Night Falls", "subtitle": "The town closes its eyes."}, room=room["code"])
    else:
        room["night_queue"], room["night_index"] = [], -1
        room["log"].append("Day breaks.")
        socketio.emit("phase_announcement", {"title": "Day Breaks", "subtitle": "The town wakes to debate."}, room=room["code"])
    broadcast(room)


@socketio.on("kill_player")
def kill_player(data):
    room, _ = current_room("storyteller")
    if not room:
        return
    player = room["players"].get(data.get("player_id"))
    if not player:
        return error("Player not found.")
    if player["alive"]:
        kill(room, player, "Storyteller")
    else:
        player["alive"] = True
        player["has_ability"] = True
        player["has_ghost_vote"] = False
        player["zombuul_hidden_alive"] = False
        room["log"].append(f"{player['name']} was revived.")
    broadcast(room)


@socketio.on("set_condition")
def set_condition(data):
    room, _ = current_room("storyteller")
    if not room:
        return
    player, condition = room["players"].get(data.get("player_id")), data.get("condition")
    if not player or condition not in ("drunk", "poisoned"):
        return error("Invalid condition.")
    if player["role"]["id"] == "parson":
        return error("The Parson cannot become drunk or poisoned.")
    player[condition] = not player[condition]
    room["log"].append(f"{player['name']} is now {'not ' if not player[condition] else ''}{condition}.")
    broadcast(room)


@socketio.on("assign_role")
def assign_role(data):
    room, _ = current_room("storyteller")
    if not room:
        return
    player = room["players"].get(data.get("player_id"))
    role_name = str(data.get("role_name", ""))
    role = BUILTIN_ROLES.get(role_name)
    if not player or player.get("spectator") or not role:
        return error("Choose a built-in role and player.")
    if room["phase"] == "ROLE_ASSIGN":
        allowed_copies = room["deck"]["role_ids"].count(role_name)
        already_assigned = sum(p["role"]["id"] == role_name for p in room["players"].values() if p["id"] != player["id"])
        if not allowed_copies or already_assigned >= allowed_copies:
            return error("That role is not available in the locked deck.")
    player["role"] = dict(role); player["role_finalized"] = False; player.pop("shown_role", None)
    if role["id"] == "drunk":
        player["drunk"] = True
        player["shown_role"] = dict(random.choice([r for r in BUILTIN_ROLES.values() if r["team"] == "Townsfolk" and r["id"] != "parson"]))
    room["log"].append(f"Assigned {role['name']} to {player['name']}.")
    for sid, conn in connections.items():
        if conn["room"] == room["code"] and conn["kind"] == "player" and conn["device_id"] == player["id"]:
            socketio.emit("role_assigned", {"role": player.get("shown_role", player["role"])}, to=sid)
    broadcast(room)


@socketio.on("finalize_role")
def finalize_role(data):
    room, _ = current_room("storyteller")
    if not room or room["phase"] != "ROLE_ASSIGN":
        return error("Roles can only be finalized during role assignment.")
    player = room["players"].get(data.get("player_id"))
    if not player or player.get("spectator") or player["role"]["id"] == "villager":
        return error("Assign a role before finalizing this seat.")
    player["role_finalized"] = True
    room["log"].append(f"Finalized {player['name']}'s role.")
    broadcast(room)


@socketio.on("wake_next_role")
def wake_next_role():
    room, _ = current_room("storyteller")
    if not room or room["phase"] != "NIGHT":
        return error("Wake roles only during Night.")
    if 0 <= room["night_index"] < len(room["night_queue"]):
        current_id = room["night_queue"][room["night_index"]]
        if room.get("night_proceed_ready") != current_id:
            return error("Wait for the current player to press Proceed before waking the next role.")
    room["night_index"] += 1
    room["night_proceed_ready"] = None
    if room["night_index"] >= len(room["night_queue"]):
        room["night_index"] = len(room["night_queue"])
        room["log"].append("Night order complete.")
        return broadcast(room)
    player_id = room["night_queue"][room["night_index"]]
    player = room["players"][player_id]
    room["log"].append(f"Waking {player['name']} ({player['role']['name']}).")
    role = role_of(player)
    living = [p for p in room["players"].values() if active(p)]
    evil = [p for p in room["players"].values() if p["role"]["team"] in ("Minion", "Demon")]
    if role["id"] == "watchman":
        minions = [p for p in living if p["role"]["team"] == "Minion"]
        private_result(player_id, information(room, player, f"A Minion is {random.choice(minions)['name']}." if minions else "There is no living Minion.", ["A Minion is Rowan.", "There is no Minion."]))
    elif role["id"] == "investigator":
        minions = [p for p in room["players"].values() if p["role"]["team"] == "Minion"]
        if minions:
            suspect = random.choice(minions); innocent = random.choice([p for p in living if p["id"] != suspect["id"]])
            private_result(player_id, information(room, player, f"One of {suspect['name']} or {innocent['name']} is the {suspect['role']['name']}.", [f"One of {suspect['name']} or {innocent['name']} is a Minion."]))
    elif role["id"] == "bounty_hunter":
        choices = [p for p in living if p["role"]["team"] != "Demon"]
        if choices: private_result(player_id, information(room, player, f"{random.choice(choices)['name']} is not the Demon.", ["Someone you trust is not the Demon."]))
    elif role["id"] == "oracle":
        count = sum(not p["alive"] and p["role"]["team"] in ("Minion", "Demon") for p in room["players"].values())
        private_result(player_id, information(room, player, f"{count} dead player(s) are evil.", [f"{random.randint(0, max(2,len(evil)))} dead player(s) are evil."]))
    elif role["id"] == "gravedigger" and room["last_executed"]:
        executed = room["players"].get(room["last_executed"])
        if executed: private_result(player_id, information(room, player, f"The executed player was {executed['role']['name']}.", ["The executed player was the Villager."]))
    elif role["id"] == "spy":
        raw = ", ".join(f"{p['name']}: {p['role']['name']} ({'alive' if p['alive'] else 'dead'})" for p in room["players"].values())
        private_result(player_id, information(room, player, "Grimoire: " + raw, ["Grimoire: the pages are unreadable."]))
    for sid, conn in connections.items():
        if conn["room"] == room["code"] and conn["kind"] == "player" and conn["device_id"] == player_id:
            shown = player.get("shown_role", role)
            socketio.emit("wake_up", {"role": shown, "target_count": role.get("targets", 0), "prompt": "Select a player to target, then submit your action."}, to=sid)
    broadcast(room)


@socketio.on("proceed_night")
def proceed_night():
    room, conn = current_room()
    if not room or conn["kind"] != "player" or room["phase"] != "NIGHT":
        return error("You cannot proceed right now.")
    current_id = room["night_queue"][room["night_index"]] if 0 <= room["night_index"] < len(room["night_queue"]) else None
    if conn["device_id"] != current_id:
        return error("It is not your turn to proceed.")
    actor = room["players"].get(current_id)
    if actor["role"].get("targets", 0) and not any(item["night"] == room["night_number"] for item in actor["target_history"]):
        return error("Submit your night action before proceeding.")
    room["night_proceed_ready"] = current_id
    room["log"].append(f"{actor['name']} is ready for the next night role.")
    broadcast(room)


@socketio.on("nominate")
def nominate(data):
    room, conn = current_room()
    if not room or conn["kind"] != "player":
        return error("Only a living player can nominate.")
    nominator = room["players"].get(conn["device_id"])
    target = room["players"].get(data.get("target_id"))
    if room["phase"] != "DAY_NOMINATION" or nominator.get("spectator") or not nominator["alive"] or not target:
        return error("Nominations open only after the town has finished discussing.")
    room["nomination"], room["votes"] = {"by": nominator["id"], "target": target["id"]}, {}
    room["log"].append(f"{nominator['name']} nominated {target['name']}.")
    broadcast(room)


@socketio.on("ready_for_voting")
def ready_for_voting():
    room, conn = current_room()
    if not room or conn["kind"] != "player": return
    player = room["players"].get(conn["device_id"])
    if room["phase"] != "DAY_TALK" or not player or player.get("spectator") or not player["alive"]:
        return error("You cannot mark yourself ready right now.")
    room["ready_for_vote"].add(player["id"])
    needed = {p["id"] for p in room["players"].values() if p["alive"] and not p.get("spectator", False)}
    if needed and needed.issubset(room["ready_for_vote"]):
        room["phase"] = "DAY_NOMINATION"
        room["log"].append("The town finished discussing. Voting is now open.")
        socketio.emit("phase_announcement", {"title": "Voting Opens", "subtitle": "Nominations may now be made."}, room=room["code"])
    broadcast(room)


@socketio.on("cast_ghost_vote")
def cast_ghost_vote():
    room, conn = current_room()
    if not room or conn["kind"] != "player":
        return
    player = room["players"].get(conn["device_id"])
    if room["phase"] != "DAY_NOMINATION" or player.get("spectator") or not room["nomination"] or player["alive"] or not player["has_ghost_vote"]:
        return error("You cannot spend a ghost vote now.")
    player["has_ghost_vote"] = False
    room["votes"][player["id"]] = "ghost"
    room["log"].append(f"{player['name']} spent their ghost vote.")
    broadcast(room)


@socketio.on("cast_vote")
def cast_vote():
    room, conn = current_room()
    if not room or conn["kind"] != "player":
        return
    player = room["players"].get(conn["device_id"])
    if room["phase"] != "DAY_NOMINATION" or player.get("spectator") or not room["nomination"] or not player["alive"]:
        return error("Only living players may vote during an active Day nomination.")
    room["votes"][player["id"]] = "living"
    broadcast(room)


@socketio.on("resolve_nomination")
def resolve_nomination():
    room, _ = current_room("storyteller")
    if not room or room["phase"] != "DAY_NOMINATION" or not room["nomination"]:
        return error("There is no nomination to resolve.")
    target = room["players"].get(room["nomination"]["target"])
    votes = len(room["votes"])
    living_count = sum(p["alive"] for p in room["players"].values())
    # A prototype majority threshold; the Storyteller may still decide timing and nomination.
    if votes > living_count / 2 and target:
        room["last_executed"] = target["id"]
        kill(room, target, "execution")
        if target["role"]["id"] == "scarlet_woman":
            pass
        if target["role"]["team"] == "Demon":
            successors = [p for p in room["players"].values() if active(p) and p["role"]["id"] == "scarlet_woman"]
            if living_count >= 5 and successors:
                successors[0]["role"] = dict(BUILTIN_ROLES["imp"])
                room["log"].append(f"{successors[0]['name']} became the Demon.")
        if target["role"]["id"] == "parson": room["phase"] = "NIGHT"
    else:
        room["last_executed"] = None
        if living_count == 3 and any(p["role"]["id"] == "mayor" and p["alive"] for p in room["players"].values()):
            room["log"].append("Good wins: the Mayor survived a no-execution day with three living players.")
    room["nomination"], room["votes"] = None, {}
    broadcast(room)


@socketio.on("night_action")
def night_action(data):
    room, conn = current_room()
    if not room or conn["kind"] != "player":
        return
    actor = room["players"].get(conn["device_id"])
    target_ids = data.get("target_ids") or [data.get("target_id")]
    targets = [room["players"].get(target_id) for target_id in target_ids if room["players"].get(target_id)]
    current_id = room["night_queue"][room["night_index"]] if 0 <= room["night_index"] < len(room["night_queue"]) else None
    if room["phase"] != "NIGHT" or not actor or not targets or actor["id"] != current_id:
        return error("It is not your role's turn to act.")
    role = role_of(actor); target = targets[0]
    if role.get("targets", 0) and len(targets) != role["targets"]:
        return error(f"{role['name']} must choose {role['targets']} target(s).")
    actor["target_history"].append({"night": room["night_number"], "targets": [p["id"] for p in targets]})
    if role["id"] == "exorcist" and target["role"]["team"] == "Demon":
        room["effects"]["blocked"].add(target["id"]); private_result(target["id"], f"The Exorcist, {actor['name']}, has blocked you tonight.")
    elif role["id"] == "innkeeper":
        room["effects"]["protected"].update(p["id"] for p in targets); victim = random.choice(targets); victim["drunk"] = True; victim["drunk_expires"] = True
    elif role["id"] == "bodyguard": room["effects"]["protected"].add(target["id"]); room["effects"]["bodyguard"][target["id"]] = actor["id"]
    elif role["id"] == "monk": room["effects"]["protected"].add(target["id"])
    elif role["id"] == "alchemist": private_result(actor["id"], information(room, actor, f"{target['name']} is {target['role']['team']}.", [f"{target['name']} is Good.", f"{target['name']} is Evil."]))
    elif role["id"] == "poisoner": target["poisoned"] = True; target["poison_expires"] = True
    elif role["id"] == "glitch": room["effects"]["fails_tomorrow"].add(target["id"])
    elif role["id"] in ("imp", "zombuul"):
        if actor["id"] in room["effects"]["blocked"]: room["log"].append(f"{actor['name']}'s demon action was blocked.")
        elif role["id"] == "zombuul" and room["last_executed"]: room["log"].append("Zombuul cannot attack after an execution.")
        elif role["id"] == "imp" and target["id"] == actor["id"]:
            kill(room, actor, "Imp self-kill"); minions=[p for p in room["players"].values() if active(p) and p["role"]["team"] == "Minion"]
            if minions: successor=random.choice(minions); successor["role"]=dict(BUILTIN_ROLES["imp"]); room["log"].append(f"{successor['name']} became the Imp.")
        else: kill(room, target, "Demon")
    elif role["id"] == "lunatic":
        demons=[p for p in room["players"].values() if p["role"]["team"] == "Demon"]
        for demon in demons: private_result(demon["id"], f"The Lunatic chose {target['name']}.")
    room["log"].append(f"Private {role['name']} action received from {actor['name']}.")
    broadcast(room)


@socketio.on("disconnect")
def disconnected():
    conn = connections.pop(request.sid, None)
    if not conn or conn["kind"] != "player":
        return
    room = rooms.get(conn["room"])
    player = room and room["players"].get(conn["device_id"])
    if player:
        player["disconnected"] = True
        room["log"].append(f"{player['name']} disconnected. The Storyteller can decide how to proceed.")
        broadcast(room)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    socketio.run(app, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)
