import os
from datetime import datetime
from flask import (
    Flask,
    render_template,
    request as flask_request,
    redirect,
    url_for,
    session,
)
from flask_socketio import SocketIO, join_room, leave_room, emit, disconnect
from bson import ObjectId
import pymongo
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "fallback-secret")

# Initialize Socket.IO (no reference to "request" import)
socketio = SocketIO(app, cors_allowed_origins="*")

# --- MongoDB Setup ---
mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
client = pymongo.MongoClient(mongo_uri)
db = client["discussion_forum"]
rooms_collection = db["rooms"]
messages_collection = db["messages"]
users_collection = db["users"]  # For tracking user data & read statuses

# Global dictionary to track index page socket connections: { username: socket_id }
index_sockets = {}


# -----------------------------------------------
# 1) Context Processor to inject `datetime` into templates
# -----------------------------------------------
@app.context_processor
def inject_datetime():
    """
    This makes the 'datetime' object available in all Jinja templates.
    e.g. in your template: {{ datetime.utcnow().year }}
    """
    return dict(datetime=datetime)


# Mock login approach for demonstration
@app.before_request
def mock_login():
    if "username" not in session:
        session["username"] = f"User_{datetime.now().timestamp()}"


@app.route("/")
def index():
    """
    Home page showing all rooms and the user's unread message counts.
    """
    rooms = list(rooms_collection.find().sort("created_at", -1))
    username = session["username"]
    user_data = users_collection.find_one({"username": username})
    if not user_data:
        # Create user document if doesn't exist
        user_data = {
            "username": username,
            "last_read": {},  # { room_id: datetime_of_last_read }
        }
        users_collection.insert_one(user_data)
    else:
        user_data["last_read"] = user_data.get("last_read", {})

    # Build a list of rooms with unread counts
    room_info = []
    for room in rooms:
        room_id_str = str(room["_id"])
        last_read_time = user_data["last_read"].get(room_id_str, datetime.min)
        unread_count = messages_collection.count_documents(
            {"room_id": ObjectId(room_id_str), "timestamp": {"$gt": last_read_time}}
        )
        room_info.append(
            {"id": room_id_str, "name": room["name"], "unread": unread_count}
        )

    return render_template("index.html", rooms=room_info, username=username)


@app.route("/create_room", methods=["POST"])
def create_room():
    """
    Create a new discussion room.
    """
    room_name = flask_request.form.get("room_name")
    if not room_name:
        return redirect(url_for("index"))
    rooms_collection.insert_one({"name": room_name, "created_at": datetime.utcnow()})
    return redirect(url_for("index"))


@app.route("/chat/<room_id>")
def chat(room_id):
    """
    Chat page for a specific room. Shows existing messages.
    """
    room = rooms_collection.find_one({"_id": ObjectId(room_id)})
    if not room:
        return "Room not found.", 404

    # Load messages from oldest to newest
    messages = list(
        messages_collection.find({"room_id": ObjectId(room_id)}).sort("timestamp", 1)
    )

    # Mark all messages as read for the current user upon entering chat
    username = session["username"]
    users_collection.update_one(
        {"username": username},
        {"$set": {f"last_read.{room_id}": datetime.utcnow()}},
    )

    return render_template("chat.html", room=room, messages=messages, username=username)


# --- Socket.IO Events ---


@socketio.on("register_index")
def register_index(data):
    """
    Registers a socket connection from the index (home) page.
    The client passes its own socket ID to avoid using 'request' from flask_socketio.
    """
    username = data.get("username")
    sid = data.get("sid")  # The client sends socket.id
    if username and sid:
        index_sockets[username] = sid


@socketio.on("disconnect")
def on_disconnect():
    """
    Without 'request' from flask_socketio, we can't directly see which SID disconnected.
    We'll skip removing the user from index_sockets to avoid errors.
    In production, you'd want a real approach to track and remove stale connections.
    """
    pass


@socketio.on("join_room")
def handle_join_room(data):
    """
    User joins a Socket.IO room (which matches our MongoDB room _id).
    """
    username = data.get("username")
    room_id = data.get("room_id")
    join_room(room_id)
    emit("status", f"{username} has joined the room.", room=room_id)


@socketio.on("leave_room")
def handle_leave_room(data):
    """
    User leaves a Socket.IO room.
    Update their last_read timestamp so future messages count as unread.
    """
    username = data.get("username")
    room_id = data.get("room_id")
    leave_room(room_id)
    # Update last_read for this room to now
    users_collection.update_one(
        {"username": username}, {"$set": {f"last_read.{room_id}": datetime.utcnow()}}
    )
    emit("status", f"{username} has left the room.", room=room_id)


@socketio.on("send_message")
def handle_send_message(data):
    """
    Handle sending a message to the specified room.
    """
    username = data.get("username")
    room_id = data.get("room_id")
    message_text = data.get("message")

    if not (username and room_id and message_text):
        return  # Missing data, do nothing

    # Store message in the database
    new_msg = {
        "room_id": ObjectId(room_id),
        "username": username,
        "text": message_text,
        "timestamp": datetime.utcnow(),
    }
    messages_collection.insert_one(new_msg)

    # Emit the message to everyone in that Socket.IO room
    emit(
        "new_message",
        {
            "username": username,
            "text": message_text,
            "timestamp": new_msg["timestamp"].isoformat(),
        },
        room=room_id,
    )

    # After sending, update unread counts for all users on the index page
    for user, sid in index_sockets.items():
        user_data = users_collection.find_one({"username": user})
        last_read_time = user_data.get("last_read", {}).get(room_id, datetime.min)
        unread_count = messages_collection.count_documents(
            {"room_id": ObjectId(room_id), "timestamp": {"$gt": last_read_time}}
        )
        # Because we only have the user's saved sid from 'register_index',
        # we emit to that SID specifically:
        socketio.emit(
            "unread_update", {"room_id": room_id, "unread": unread_count}, room=sid
        )


# For local testing:
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
