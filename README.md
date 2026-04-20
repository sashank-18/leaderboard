# 🎮 GameBoard — Server-Side Gaming Leaderboard

A full-stack gaming leaderboard built with **Python Flask**, **AWS DynamoDB**, **AWS SNS**, and a
**Glassmorphism** UI themed in Deep Forest (#102C26) and Champagne (#F7E7CE).

---

## 📁 File Structure

```
leaderboard/
├── app.py                    ← Flask backend (Boto3, RBAC, fraud detection)
├── requirements.txt
├── README.md
└── templates/
    ├── login.html            ← Gaming Glassmorphism login page
    ├── register.html         ← Registration with password strength meter
    └── leaderboard.html      ← Live leaderboard + Admin Console
```

---

## 🚀 Quick Start (No AWS Required)

```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```

Demo credentials:
| Role  | Username | Password  |
|-------|----------|-----------|
| Admin | admin    | admin123  |
| User  | player   | player123 |

---

## ☁️ AWS Setup

### 1. DynamoDB Tables

**GameLeaderboard** (scores table)
```
Primary Key:  user_id (String)
GSI:          score-index
  - Partition: gsi_pk (String) = "SCORE"
  - Sort:      score (Number)
  - Projection: ALL
```

**GameUsers** (auth table)
```
Primary Key:  username (String)
```

### 2. SNS Topic
Create a standard SNS topic and set `SNS_TOPIC_ARN` env var.
Subscribe your email to receive Top-10 break-in notifications.

### 3. IAM Least-Privilege Policy
The exact policy is served at `GET /iam_policy` (admin only).
Attach it to your EC2/Lambda execution role — it grants only the
DynamoDB actions needed and SNS Publish to the specific topic.

### 4. Environment Variables

```bash
export SECRET_KEY="your-production-secret"
export AWS_REGION="us-east-1"
export DYNAMO_TABLE="GameLeaderboard"
export USER_TABLE="GameUsers"
export SNS_TOPIC_ARN="arn:aws:sns:us-east-1:123456789:GameLeaderboard-Alerts"
```

---

## 🧠 Architecture Highlights

### Atomic Score Counters
`UpdateItem` with `ADD score :delta` — race-condition-safe, no read-modify-write.

### GSI for Top-10
All score rows share `gsi_pk = "SCORE"`. A single `Query` on the GSI with
`ScanIndexForward=False, Limit=10` returns the sorted top 10 in O(log n).

### Fraud Detection
Every score submission is validated server-side:
- Score must be a non-negative integer ≤ 9,999,999
- Session must be ≥ 30 seconds
- Score ÷ session_seconds must not exceed 50 pts/sec

### SNS Top-10 Alerts
After every valid score update, the server checks if the user is in the
refreshed Top-10 and fires an SNS `Publish` if so.

### RBAC
| Endpoint              | User | Admin |
|-----------------------|------|-------|
| View leaderboard      | ✅   | ✅    |
| Submit score          | ✅   | ✅    |
| Edit any score        | ❌   | ✅    |
| Delete any score      | ❌   | ✅    |
| View IAM policy JSON  | ❌   | ✅    |

---

## 🎨 UI Features

- **Glassmorphism cards** with `backdrop-filter: blur(24px)`
- **Deep Forest** (#102C26) background + **Champagne** (#F7E7CE) text
- **#1 rank pulse glow** animation (gold shimmer loop)
- **Staggered row entrance** (CSS animation-delay per row)
- **Score bar** proportional to leader's score, per rank color
- **Password strength meter** on register page
- **Live clock** and 30-second auto-refresh via `/api/leaderboard`