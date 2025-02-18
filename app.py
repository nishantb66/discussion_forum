import os
import time
import threading
import requests
import bcrypt
import jwt
from datetime import datetime, timedelta
from flask import (
    Flask,
    render_template,
    request as flask_request,
    redirect,
    url_for,
    session,
    request,
)
from flask_socketio import SocketIO, join_room, leave_room, emit
from bson import ObjectId
import pymongo
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "fallback-secret")
JWT_SECRET = os.getenv("JWT_SECRET", "default_jwt_secret")

mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
client = pymongo.MongoClient(mongo_uri)

# Discussion forum DB (rooms, messages, chat metadata)
discussion_db = client["discussion_forum"]
rooms_collection = discussion_db["rooms"]
messages_collection = discussion_db["messages"]
chat_users_collection = discussion_db["users"]

# Test DB (for credentials)
test_db = client["test"]
auth_users_collection = test_db["users"]  # credentials (email, password, role)

socketio = SocketIO(app, cors_allowed_origins="*")

index_sockets = {}
rooms_occupancy = defaultdict(set)


@app.context_processor
def inject_datetime():
    return dict(datetime=datetime)


# A keep-alive route for pings
@app.route("/ping")
def ping():
    return "PONG", 200


# Combine login + index in a single route
@app.route("/", methods=["GET", "POST"])
def home():
    """
    - If POST, try to log in the user.
    - If GET, show either the login form or the discussion rooms depending on whether the user is authenticated.
    """
    error = None

    # Handle login attempts
    if flask_request.method == "POST":
        email = flask_request.form.get("email", "").lower()
        password = flask_request.form.get("password", "")
        user = auth_users_collection.find_one({"email": email})

        if user and bcrypt.checkpw(
            password.encode("utf-8"), user["password"].encode("utf-8")
        ):
            # Only allow Executives to access the chat
            if user.get("role") == "Executive":
                # Generate JWT token
                payload = {
                    "email": user["email"],
                    "name": user["name"],
                    "role": user["role"],
                    "emp_id": user["emp_id"],
                    "exp": datetime.utcnow() + timedelta(hours=1),
                }
                token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")

                # Store in session
                session["token"] = token
                session["username"] = user["name"]
                session["email"] = user["email"]
                session["role"] = user["role"]
            else:
                error = "Access denied: Only Executives can access the chat."
        else:
            error = "Invalid email or password."

    # Check if user is authenticated (token in session & valid)
    if "token" in session:
        try:
            jwt.decode(session["token"], JWT_SECRET, algorithms=["HS256"])
            # If the token is valid, show the discussion rooms
            return show_discussion_rooms(error=error)
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            # Clear session and fall back to login form
            session.clear()

    # If we reach here, show login form
    return render_template("index.html", error=error, is_authenticated=False)


def show_discussion_rooms(error=None):
    """
    Helper function that loads all rooms and unread counts, then returns the combined index template
    but with the chat home UI visible instead of the login form.
    """
    username = session["username"]
    rooms = list(rooms_collection.find().sort("created_at", -1))

    # Check if user doc exists in discussion_forum.users
    chat_user = chat_users_collection.find_one({"username": username})
    if not chat_user:
        chat_user = {"username": username, "last_read": {}}
        chat_users_collection.insert_one(chat_user)
    else:
        chat_user["last_read"] = chat_user.get("last_read", {})

    # Build list of rooms with unread counts
    room_info = []
    for room in rooms:
        room_id_str = str(room["_id"])
        last_read_time = chat_user["last_read"].get(room_id_str, datetime.min)
        unread_count = messages_collection.count_documents(
            {"room_id": ObjectId(room_id_str), "timestamp": {"$gt": last_read_time}}
        )
        room_info.append(
            {"id": room_id_str, "name": room["name"], "unread": unread_count}
        )

    return render_template(
        "index.html",
        is_authenticated=True,
        error=error,
        username=username,
        rooms=room_info,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/create_room", methods=["POST"])
def create_room():
    if "token" not in session:
        return redirect(url_for("home"))
    room_name = flask_request.form.get("room_name")
    if room_name:
        rooms_collection.insert_one(
            {"name": room_name, "created_at": datetime.utcnow()}
        )
    return redirect(url_for("home"))


@app.route("/chat/<room_id>")
def chat(room_id):
    if "token" not in session:
        return redirect(url_for("home"))

    room = rooms_collection.find_one({"_id": ObjectId(room_id)})
    if not room:
        return "Room not found.", 404

    messages = list(
        messages_collection.find({"room_id": ObjectId(room_id)}).sort("timestamp", 1)
    )
    username = session["username"]
    chat_users_collection.update_one(
        {"username": username},
        {"$set": {f"last_read.{room_id}": datetime.utcnow()}},
    )
    return render_template("chat.html", room=room, messages=messages, username=username)


# Socket.IO Events
@socketio.on("register_index")
def register_index(data):
    username = data.get("username")
    sid = data.get("sid")
    if username and sid:
        index_sockets[username] = sid


@socketio.on("disconnect")
def on_disconnect():
    pass


@socketio.on("join_room")
def handle_join_room(data):
    username = data.get("username")
    room_id = data.get("room_id")
    join_room(room_id)
    rooms_occupancy[room_id].add(request.sid)
    emit("status", f"{username} has joined the room.", room=room_id)


@socketio.on("leave_room")
def handle_leave_room(data):
    username = data.get("username")
    room_id = data.get("room_id")
    leave_room(room_id)
    chat_users_collection.update_one(
        {"username": username}, {"$set": {f"last_read.{room_id}": datetime.utcnow()}}
    )
    user_data = chat_users_collection.find_one({"username": username})
    last_read_time = user_data.get("last_read", {}).get(room_id, datetime.min)
    unread_count = messages_collection.count_documents(
        {"room_id": ObjectId(room_id), "timestamp": {"$gt": last_read_time}}
    )
    sid = index_sockets.get(username)
    if sid:
        socketio.emit(
            "unread_update", {"room_id": room_id, "unread": unread_count}, room=sid
        )
    emit("status", f"{username} has left the room.", room=room_id)
    rooms_occupancy[room_id].discard(request.sid)
    if len(rooms_occupancy[room_id]) == 0:
        rooms_collection.delete_one({"_id": ObjectId(room_id)})
        messages_collection.delete_many({"room_id": ObjectId(room_id)})
        chat_users_collection.update_many({}, {"$unset": {f"last_read.{room_id}": 1}})
        del rooms_occupancy[room_id]


@socketio.on("send_message")
def handle_send_message(data):
    username = data.get("username")
    room_id = data.get("room_id")
    message_text = data.get("message")
    if not (username and room_id and message_text):
        return
    new_msg = {
        "room_id": ObjectId(room_id),
        "username": username,
        "text": message_text,
        "timestamp": datetime.utcnow(),
    }
    messages_collection.insert_one(new_msg)
    emit(
        "new_message",
        {
            "username": username,
            "text": message_text,
            "timestamp": new_msg["timestamp"].isoformat(),
        },
        room=room_id,
    )
    for user, sid in index_sockets.items():
        chat_user_doc = chat_users_collection.find_one({"username": user})
        last_read_time = chat_user_doc.get("last_read", {}).get(room_id, datetime.min)
        unread_count = messages_collection.count_documents(
            {"room_id": ObjectId(room_id), "timestamp": {"$gt": last_read_time}}
        )
        socketio.emit(
            "unread_update", {"room_id": room_id, "unread": unread_count}, room=sid
        )


# Typing indicator
@socketio.on("typing")
def handle_typing(data):
    username = data.get("username")
    room_id = data.get("room_id")
    emit("typing", {"username": username}, room=room_id, include_self=False)


@socketio.on("stop_typing")
def handle_stop_typing(data):
    username = data.get("username")
    room_id = data.get("room_id")
    emit("stop_typing", {"username": username}, room=room_id, include_self=False)


def keep_alive():
    ping_url = os.getenv("KEEP_ALIVE_URL", "http://localhost:5000/ping")
    interval_seconds = 600
    while True:
        time.sleep(interval_seconds)
        try:
            requests.get(ping_url, timeout=10)
            print(f"Keep-alive ping sent to {ping_url}")
        except Exception as e:
            print(f"Keep-alive ping failed: {e}")


@app.after_request
def add_no_cache_headers(response):
    """
    Add headers to disable caching of responses so that
    after logging out, the browser won't show a stale page
    on 'Back' button.
    """
    response.headers["Cache-Control"] = (
        "no-cache, no-store, must-revalidate, private, max-age=0"
    )
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


if __name__ == "__main__":
    t = threading.Thread(target=keep_alive, daemon=True)
    t.start()
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
