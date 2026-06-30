"""Slack leave-request form -> Google Sheet ("Leaves" tab).

A Socket Mode Slack app: the `/leave` command opens a modal with From date,
To date, and Leave type (Sick / Casual). On submit it appends the record to the
"Leaves" tab of the attendance Google Sheet and DMs a confirmation.

Setup (one-time) — see SLACK_LEAVE_SETUP in the README notes:
  * Slack app -> Socket Mode: ON, create an App-Level Token (scope
    connections:write) -> put it in .env as SLACK_APP_TOKEN=xapp-...
  * Bot Token Scopes: commands, chat:write  (then reinstall the app)
  * Slash Commands -> create  /leave  (any description; no URL needed)

Run (keep it running to accept submissions):
  .venv/Scripts/python.exe leave_app.py
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

SPREADSHEET_ID = os.environ.get("ATTENDANCE_SPREADSHEET_ID", "1W3H2uMFG__KTSXDw0trJi71M1RahQS65_bao8EC4sOI")
KEY_PATH = os.environ.get("GOOGLE_SA_KEY_PATH", "credentials/google_credentials.json")
LEAVES_HEADERS = ["Submitted At", "Employee", "Slack User ID", "From Date", "To Date",
                  "Leave Type", "Days", "Reason"]

import logging  # noqa: E402

from slack_bolt import App  # noqa: E402
# websocket-client based handler — more stable than the built-in client on
# Windows setups that show "Failed to check the state of sock" SSL errors.
from slack_bolt.adapter.socket_mode.websocket_client import SocketModeHandler  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = App(token=os.environ["SLACK_BOT_TOKEN"])


@app.error
def on_error(error, body, logger):
    logger.exception(f"Bolt listener error: {error}")


def _leaves_ws():
    """Return the Leaves worksheet, creating it (with headers) if absent."""
    import gspread
    from google.oauth2.service_account import Credentials

    key = Path(KEY_PATH)
    if not key.is_absolute():
        key = ROOT / key
    creds = Credentials.from_service_account_file(
        str(key), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    sh = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
    try:
        return sh.worksheet("Leaves")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Leaves", rows=200, cols=len(LEAVES_HEADERS))
        ws.update(values=[LEAVES_HEADERS], range_name="A1", value_input_option="RAW")
        ws.freeze(rows=1)
        return ws


def _modal() -> dict:
    return {
        "type": "modal",
        "callback_id": "leave_modal",
        "title": {"type": "plain_text", "text": "Apply for Leave"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input", "block_id": "from_date",
             "label": {"type": "plain_text", "text": "From date"},
             "element": {"type": "datepicker", "action_id": "val",
                         "placeholder": {"type": "plain_text", "text": "Select a date"}}},
            {"type": "input", "block_id": "to_date",
             "label": {"type": "plain_text", "text": "To date"},
             "element": {"type": "datepicker", "action_id": "val",
                         "placeholder": {"type": "plain_text", "text": "Select a date"}}},
            {"type": "input", "block_id": "leave_type",
             "label": {"type": "plain_text", "text": "Leave type"},
             "element": {"type": "static_select", "action_id": "val",
                         "placeholder": {"type": "plain_text", "text": "Choose"},
                         "options": [
                             {"text": {"type": "plain_text", "text": "Sick Leave"}, "value": "Sick Leave"},
                             {"text": {"type": "plain_text", "text": "Casual Leave"}, "value": "Casual Leave"},
                         ]}},
            {"type": "input", "block_id": "reason", "optional": True,
             "label": {"type": "plain_text", "text": "Reason (optional)"},
             "element": {"type": "plain_text_input", "action_id": "val", "multiline": True}},
        ],
    }


def open_leave_modal(ack, body, client):
    ack()
    client.views_open(trigger_id=body["trigger_id"], view=_modal())


# Register the handler for whichever command name was created in Slack.
for _cmd in ("/leave", "/leaverequest"):
    app.command(_cmd)(open_leave_modal)


@app.view("leave_modal")
def handle_leave_submit(ack, body, view, client):
    v = view["state"]["values"]
    frm = v["from_date"]["val"]["selected_date"]
    to = v["to_date"]["val"]["selected_date"]
    leave_type = v["leave_type"]["val"]["selected_option"]["value"]
    reason = (v["reason"]["val"].get("value") or "").strip()

    # Validate: To date must be on/after From date.
    if dt.date.fromisoformat(to) < dt.date.fromisoformat(frm):
        ack(response_action="errors",
            errors={"to_date": "To date must be on or after the From date."})
        return
    ack()

    days = (dt.date.fromisoformat(to) - dt.date.fromisoformat(frm)).days + 1  # inclusive
    user_id = body["user"]["id"]
    try:
        name = client.users_info(user=user_id)["user"]["profile"].get("real_name") or user_id
    except Exception:  # noqa: BLE001
        name = user_id
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _leaves_ws().append_row(
        [stamp, name, user_id, frm, to, leave_type, days, reason],
        value_input_option="USER_ENTERED",
    )

    client.chat_postMessage(
        channel=user_id,
        text=(f":white_check_mark: Your *{leave_type}* from *{frm}* to *{to}* "
              f"({days} day{'s' if days != 1 else ''}) has been recorded."),
    )


if __name__ == "__main__":
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise SystemExit(
            "Missing SLACK_APP_TOKEN (xapp-...). Enable Socket Mode on the Slack app, "
            "create an App-Level Token with 'connections:write', and add it to .env."
        )
    print("Leave app running (Socket Mode). Use /leave in Slack. Ctrl+C to stop.")
    SocketModeHandler(app, app_token).start()
