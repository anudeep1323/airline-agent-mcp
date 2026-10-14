"""
Chat UI for the airline agent. It's just another HTTP client of airline_service.py: login -> JWT -> /chat -> /approve.

  terminal 1:  export JWT_SECRET=... ANTHROPIC_API_KEY=...; python3 -m uvicorn airline_service:app --port 8000
  terminal 2:  python3 -m streamlit run ui.py            -> http://localhost:8501
"""
import os
import uuid

import httpx
import streamlit as st

API = os.environ.get("AIRLINE_API", "http://127.0.0.1:8000")
st.set_page_config(page_title="SkyLine support", page_icon="✈️")
S = st.session_state                                   # survives Streamlit's rerun-on-every-click
S.setdefault("token", None)
S.setdefault("history", [])                           # [{"role", "text", "meta"}]
S.setdefault("pending", [])                           # approvals waiting for a click
S.setdefault("conversation_id", uuid.uuid4().hex[:8])


def api(method, path, **kwargs):
    headers = {"Authorization": f"Bearer {S.token}"} if S.token else {}
    r = httpx.request(method, API + path, headers=headers, timeout=90, **kwargs)
    if r.status_code == 401 and S.token:              # token expired (30 min): back to the login box
        S.token = None
        st.warning("Session expired, please log in again.")
        st.stop()
    return r


# ---- sidebar: login ---------------------------------------------------------------------
if S.token is None:
    st.title("✈️ SkyLine support")
    st.info("Log in from the sidebar on the left to start chatting.")
with st.sidebar:
    st.header("SkyLine")
    if S.token is None:
        with st.form("login"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Log in"):
                r = api("POST", "/v1/login", json={"username": username, "password": password})
                if r.status_code == 200:
                    S.token, S.username = r.json()["access_token"], username
                    st.rerun()
                st.error(r.json().get("detail", r.text))  # 401 wrong password, 429 rate limited
        st.caption("Dev accounts are in DEV_USERS in airline_server.py")
        st.stop()
    st.write(f"Logged in as **{S.username}**")
    st.caption(f"conversation {S.conversation_id}")
    if st.button("Log out"):
        for k in ("token", "history", "pending"):
            S.pop(k)
        S.conversation_id = uuid.uuid4().hex[:8]
        st.rerun()

# ---- chat history ----------------------------------------------------------------------
st.title("✈️ SkyLine support")
for m in S.history:
    with st.chat_message(m["role"]):
        st.markdown(m["text"])
        if m.get("meta"):
            with st.expander("behind the scenes"):
                st.json(m["meta"])

# ---- approvals: the HUMAN decides (POST /approve calls confirm_cancel, no LLM involved) ---
for a in list(S.pending):
    with st.container(border=True):
        st.markdown(f"**Approval needed:** {a['summary']}")
        yes, no = st.columns(2)
        clicked = "approve" if yes.button("Approve", key="y" + a["approval_id"], type="primary") else \
                  "reject" if no.button("Reject", key="n" + a["approval_id"]) else None
        if clicked:
            r = api("POST", "/approve", json={"conversation_id": S.conversation_id,
                                              "approval_id": a["approval_id"], "approve": clicked == "approve"})
            body = r.json()
            text = (f"✅ Cancelled {body['confirmation']}, seat {body['seat_freed']} released." if body.get("cancelled")
                    else "Kept your booking." if r.status_code == 200 else f"⚠️ {body.get('detail')}")
            S.history.append({"role": "assistant", "text": text, "meta": {"status": r.status_code, **body}})
            S.pending.remove(a)
            st.rerun()

# ---- new message --------------------------------------------------------------------------
if prompt := st.chat_input("Ask about flights or your bookings"):
    S.history.append({"role": "user", "text": prompt})
    with st.spinner("Thinking..."):
        r = api("POST", "/chat", json={"conversation_id": S.conversation_id, "message": prompt})
    if r.status_code != 200:
        S.history.append({"role": "assistant", "text": f"⚠️ {r.status_code}: {r.text[:200]}"})
    else:
        body = r.json()
        S.history.append({"role": "assistant", "text": body["answer"],
                          "meta": {"request_id": r.headers.get("x-request-id"), "cost": body["cost"],
                                   "ms": body["ms"], "steps": body["steps"]}})
        S.pending += body["approvals"]
    st.rerun()
