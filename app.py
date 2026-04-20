"""
Server-Side Gaming Leaderboard — Flask + AWS DynamoDB + SNS
"""

import os
import time
import uuid
import json
import logging
from functools import wraps
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, jsonify, session
)
from flask_login import (
    LoginManager, UserMixin, login_user,
    logout_user, login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

# ── Optional AWS imports ──────────────────────────────────────────────────────
try:
    import boto3
    from boto3.dynamodb.conditions import Key, Attr
    from botocore.exceptions import ClientError, NoCredentialsError
    AWS_AVAILABLE = True
except ImportError:
    AWS_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# App Configuration
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Flask-Login ───────────────────────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access the leaderboard."

# ─────────────────────────────────────────────────────────────────────────────
# AWS Configuration
# ─────────────────────────────────────────────────────────────────────────────

AWS_REGION       = os.environ.get("AWS_REGION", "us-east-1")
DYNAMO_TABLE     = os.environ.get("DYNAMO_TABLE", "GameLeaderboard")
USER_TABLE       = os.environ.get("USER_TABLE", "GameUsers")
SNS_TOPIC_ARN    = os.environ.get("SNS_TOPIC_ARN", "")

# ── IAM Least-Privilege policy doc (documentation / audit log) ────────────────
IAM_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "DynamoDBLeastPrivilege",
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Query",
                "dynamodb:Scan"
            ],
            "Resource": [
                f"arn:aws:dynamodb:{AWS_REGION}:*:table/{DYNAMO_TABLE}",
                f"arn:aws:dynamodb:{AWS_REGION}:*:table/{DYNAMO_TABLE}/index/*",
                f"arn:aws:dynamodb:{AWS_REGION}:*:table/{USER_TABLE}"
            ]
        },
        {
            "Sid": "SNSPublishOnly",
            "Effect": "Allow",
            "Action": ["sns:Publish"],
            "Resource": SNS_TOPIC_ARN or "*"
        }
    ]
}

# ─────────────────────────────────────────────────────────────────────────────
# Fraud Detection — score validation
# ─────────────────────────────────────────────────────────────────────────────

MAX_SCORE_PER_SECOND = 50       # points a player can earn per second
MAX_ABSOLUTE_SCORE   = 9_999_999
MIN_SESSION_SECONDS  = 30       # a game must last at least 30 s


def validate_score(score: int, session_duration_seconds: int) -> tuple[bool, str]:
    """
    Server-side fraud detection.
    Returns (is_valid, reason).
    """
    if not isinstance(score, int) or score < 0:
        return False, "Score must be a non-negative integer."
    if score > MAX_ABSOLUTE_SCORE:
        return False, f"Score {score:,} exceeds maximum possible score."
    if session_duration_seconds < MIN_SESSION_SECONDS:
        return False, f"Session too short ({session_duration_seconds}s). Minimum is {MIN_SESSION_SECONDS}s."
    max_possible = MAX_SCORE_PER_SECOND * session_duration_seconds
    if score > max_possible:
        return False, (
            f"Score {score:,} is physically impossible for a "
            f"{session_duration_seconds}s session (max {max_possible:,})."
        )
    return True, "OK"

# ─────────────────────────────────────────────────────────────────────────────
# DynamoDB / Fallback Data Layer
# ─────────────────────────────────────────────────────────────────────────────

# ── In-memory fallback stores ─────────────────────────────────────────────────
_DUMMY_SCORES = [
    {"user_id": "uid1", "username": "ShadowBlade",   "score": 982_450, "updated_at": "2024-06-01T14:22:00"},
    {"user_id": "uid2", "username": "NeonPhoenix",   "score": 875_300, "updated_at": "2024-06-02T09:11:00"},
    {"user_id": "uid3", "username": "VoidWalker",    "score": 761_120, "updated_at": "2024-06-02T18:44:00"},
    {"user_id": "uid4", "username": "CryptoKnight",  "score": 654_890, "updated_at": "2024-06-03T07:05:00"},
    {"user_id": "uid5", "username": "StarForge",     "score": 543_210, "updated_at": "2024-06-03T12:30:00"},
    {"user_id": "uid6", "username": "IronSpectre",   "score": 432_100, "updated_at": "2024-06-04T16:20:00"},
    {"user_id": "uid7", "username": "LunarEdge",     "score": 321_050, "updated_at": "2024-06-04T20:15:00"},
    {"user_id": "uid8", "username": "RubyStorm",     "score": 210_980, "updated_at": "2024-06-05T08:45:00"},
    {"user_id": "uid9", "username": "GhostPulse",    "score": 109_870, "updated_at": "2024-06-05T11:00:00"},
    {"user_id": "uid10","username": "ZeroHour",      "score":  98_760, "updated_at": "2024-06-05T14:10:00"},
]

_DUMMY_USERS = {
    "admin": {
        "user_id": "admin-uid",
        "username": "admin",
        "password_hash": generate_password_hash("admin123"),
        "role": "admin"
    },
    "player": {
        "user_id": "player-uid",
        "username": "player",
        "password_hash": generate_password_hash("player123"),
        "role": "user"
    }
}

def _get_dynamo_resource():
    if not AWS_AVAILABLE:
        return None, None
    try:
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        scores_table = dynamodb.Table(DYNAMO_TABLE)
        users_table  = dynamodb.Table(USER_TABLE)
        # Quick connectivity test
        scores_table.load()
        return scores_table, users_table
    except Exception as e:
        logger.warning(f"DynamoDB unavailable, using fallback: {e}")
        return None, None

def get_top10():
    """Return top-10 scores sorted descending. Uses GSI if DynamoDB available."""
    scores_table, _ = _get_dynamo_resource()
    if scores_table:
        try:
            resp = scores_table.query(
                IndexName="score-index",
                KeyConditionExpression=Key("gsi_pk").eq("SCORE"),
                ScanIndexForward=False,
                Limit=10
            )
            return resp.get("Items", [])
        except Exception as e:
            logger.error(f"DynamoDB query failed: {e}")

    # Fallback
    return sorted(_DUMMY_SCORES, key=lambda x: x["score"], reverse=True)[:10]


def get_all_scores():
    """Admin: return all scores."""
    scores_table, _ = _get_dynamo_resource()
    if scores_table:
        try:
            resp = scores_table.scan()
            return sorted(resp.get("Items", []), key=lambda x: int(x.get("score", 0)), reverse=True)
        except Exception as e:
            logger.error(f"DynamoDB scan failed: {e}")

    return sorted(_DUMMY_SCORES, key=lambda x: x["score"], reverse=True)


def update_score_atomic(user_id: str, username: str, delta: int):
    """Atomic counter increment via DynamoDB UpdateItem."""
    scores_table, _ = _get_dynamo_resource()
    new_score = None

    if scores_table:
        try:
            resp = scores_table.update_item(
                Key={"user_id": user_id},
                UpdateExpression=(
                    "SET username = :u, gsi_pk = :g, updated_at = :t "
                    "ADD score :d"
                ),
                ExpressionAttributeValues={
                    ":u": username,
                    ":g": "SCORE",
                    ":t": datetime.utcnow().isoformat(),
                    ":d": delta
                },
                ReturnValues="UPDATED_NEW"
            )
            new_score = int(resp["Attributes"]["score"])
        except Exception as e:
            logger.error(f"Atomic update failed: {e}")
    else:
        # Fallback: update in-memory
        for entry in _DUMMY_SCORES:
            if entry["user_id"] == user_id:
                entry["score"] += delta
                new_score = entry["score"]
                break
        else:
            new_score = delta
            _DUMMY_SCORES.append({
                "user_id": user_id,
                "username": username,
                "score": new_score,
                "updated_at": datetime.utcnow().isoformat()
            })

    return new_score


def delete_score(user_id: str):
    scores_table, _ = _get_dynamo_resource()
    if scores_table:
        try:
            scores_table.delete_item(Key={"user_id": user_id})
            return True
        except Exception as e:
            logger.error(f"Delete failed: {e}")
    else:
        global _DUMMY_SCORES
        _DUMMY_SCORES = [s for s in _DUMMY_SCORES if s["user_id"] != user_id]
        return True
    return False


def set_score_direct(user_id: str, username: str, score: int):
    """Admin override — set score directly."""
    scores_table, _ = _get_dynamo_resource()
    if scores_table:
        try:
            scores_table.put_item(Item={
                "user_id": user_id,
                "username": username,
                "score": score,
                "gsi_pk": "SCORE",
                "updated_at": datetime.utcnow().isoformat()
            })
            return True
        except Exception as e:
            logger.error(f"Set score failed: {e}")
    else:
        for entry in _DUMMY_SCORES:
            if entry["user_id"] == user_id:
                entry["score"] = score
                return True
        _DUMMY_SCORES.append({
            "user_id": user_id, "username": username,
            "score": score, "updated_at": datetime.utcnow().isoformat()
        })
        return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
# SNS — Top-10 break-in notification
# ─────────────────────────────────────────────────────────────────────────────

def notify_top10_entry(username: str, score: int, rank: int):
    if not AWS_AVAILABLE or not SNS_TOPIC_ARN:
        logger.info(f"[SNS-MOCK] {username} broke into Top 10 at rank #{rank} with {score:,} pts")
        return
    try:
        sns = boto3.client("sns", region_name=AWS_REGION)
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"🏆 New Top-10 Entry — #{rank}",
            Message=(
                f"Player '{username}' just broke into the Top 10!\n"
                f"Rank: #{rank}\nScore: {score:,}\n"
                f"Time: {datetime.utcnow().isoformat()}Z"
            )
        )
        logger.info(f"SNS notification sent for {username}")
    except Exception as e:
        logger.error(f"SNS publish failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# User Model & Auth
# ─────────────────────────────────────────────────────────────────────────────

class User(UserMixin):
    def __init__(self, user_id, username, role):
        self.id       = user_id
        self.username = username
        self.role     = role

    def is_admin(self):
        return self.role == "admin"


def find_user_by_username(username: str):
    _, users_table = _get_dynamo_resource()
    if users_table:
        try:
            resp = users_table.get_item(Key={"username": username})
            return resp.get("Item")
        except Exception as e:
            logger.error(f"User lookup failed: {e}")
    return _DUMMY_USERS.get(username)


def create_user(username: str, password: str, role: str = "user"):
    _, users_table = _get_dynamo_resource()
    user_data = {
        "user_id":       str(uuid.uuid4()),
        "username":      username,
        "password_hash": generate_password_hash(password),
        "role":          role
    }
    if users_table:
        try:
            users_table.put_item(Item=user_data)
        except Exception as e:
            logger.error(f"User creation failed: {e}")
    else:
        _DUMMY_USERS[username] = user_data
    return user_data


@login_manager.user_loader
def load_user(user_id):
    # Search fallback store
    for u in _DUMMY_USERS.values():
        if u["user_id"] == user_id:
            return User(u["user_id"], u["username"], u["role"])
    return None

# ─────────────────────────────────────────────────────────────────────────────
# RBAC Decorator
# ─────────────────────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin():
            flash("Admin access required.", "error")
            return redirect(url_for("leaderboard"))
        return f(*args, **kwargs)
    return decorated

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("leaderboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("leaderboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user_data = find_user_by_username(username)

        if user_data and check_password_hash(user_data["password_hash"], password):
            user = User(user_data["user_id"], user_data["username"], user_data["role"])
            login_user(user)
            flash(f"Welcome back, {username}! 🎮", "success")
            return redirect(url_for("leaderboard"))

        flash("Invalid username or password.", "error")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("leaderboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        if not username or len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif find_user_by_username(username):
            flash("Username already taken.", "error")
        else:
            create_user(username, password)
            flash("Account created! Please log in. 🎉", "success")
            return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/leaderboard")
@login_required
def leaderboard():
    top10 = get_top10()
    # Enrich with rank
    for i, entry in enumerate(top10, start=1):
        entry["rank"] = i
    is_admin = current_user.is_admin()
    all_scores = get_all_scores() if is_admin else []
    return render_template(
        "leaderboard.html",
        top10=top10,
        all_scores=all_scores,
        is_admin=is_admin,
        username=current_user.username
    )


@app.route("/submit_score", methods=["POST"])
@login_required
def submit_score():
    """User submits a score increment with fraud detection."""
    try:
        data = request.get_json()
        score_delta       = int(data.get("score", 0))
        session_duration  = int(data.get("session_duration", 0))

        valid, reason = validate_score(score_delta, session_duration)
        if not valid:
            return jsonify({"success": False, "error": f"Fraud detected: {reason}"}), 400

        new_score = update_score_atomic(
            current_user.id, current_user.username, score_delta
        )

        # Check if user broke into top-10
        top10 = get_top10()
        top10_ids = [e["user_id"] for e in top10]
        if current_user.id in top10_ids:
            rank = top10_ids.index(current_user.id) + 1
            notify_top10_entry(current_user.username, new_score, rank)

        return jsonify({"success": True, "new_score": new_score})
    except (ValueError, TypeError) as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/admin/edit_score", methods=["POST"])
@login_required
@admin_required
def admin_edit_score():
    user_id  = request.form.get("user_id")
    username = request.form.get("username")
    try:
        new_score = int(request.form.get("score", 0))
    except ValueError:
        flash("Invalid score value.", "error")
        return redirect(url_for("leaderboard"))

    if set_score_direct(user_id, username, new_score):
        flash(f"Score updated for {username}: {new_score:,}", "success")
    else:
        flash("Failed to update score.", "error")
    return redirect(url_for("leaderboard"))


@app.route("/admin/delete_score/<user_id>", methods=["POST"])
@login_required
@admin_required
def admin_delete_score(user_id):
    if delete_score(user_id):
        flash("Score entry deleted.", "success")
    else:
        flash("Failed to delete entry.", "error")
    return redirect(url_for("leaderboard"))


@app.route("/api/leaderboard")
@login_required
def api_leaderboard():
    """JSON endpoint for live refresh."""
    top10 = get_top10()
    for i, e in enumerate(top10, 1):
        e["rank"] = i
        e["score"] = int(e["score"])
    return jsonify(top10)


@app.route("/iam_policy")
@login_required
@admin_required
def iam_policy():
    """Admin: view the IAM least-privilege policy."""
    return jsonify(IAM_POLICY)

# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5000)