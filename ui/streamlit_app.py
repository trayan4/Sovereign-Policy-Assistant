#!/usr/bin/env python3
"""Simple Streamlit front end for the Sovereign Policy Assistant. Talks to
the FastAPI backend (app/server.py) over HTTP - it never touches the
LangGraph pipeline, Chroma, or Ollama directly, so this stays a thin,
independently deployable UI layer over an already-tested API."""

import os

import requests
import streamlit as st

API_URL = os.environ.get("API_URL", "http://localhost:8000")
KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://keycloak:8080")
KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "sovereign-policy")
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "sovereign-policy-app")
TOKEN_URL = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"

st.set_page_config(page_title="Sovereign Policy Assistant", layout="wide")
st.title("Sovereign Policy Assistant")
st.caption(
    "Answers are grounded in the bank's own policy library and run fully "
    "on-premise. Every answer is backed by an exact source citation."
)

CASE_LABELS = {
    "normal": ("✅", "Answered from policy"),
    "contradiction": ("⚠️", "Two policies disagree"),
    "expired": ("⏱️", "Source policy has expired"),
    "out_of_scope": ("❔", "Not covered by any policy"),
}


def _login(username: str, password: str) -> bool:
    """Direct Access Grant (Resource Owner Password Credentials): this app
    exchanges the username/password for a token itself, server-side,
    rather than redirecting the browser through Keycloak's own login page
    (the more familiar OAuth flow for a public/JS client). That's a
    deliberate choice, not a shortcut: Streamlit is a trusted first-party
    server-side app, not an untrusted browser client, which is exactly the
    scenario this grant type exists for - and it sidesteps the redirect-
    URL/issuer-mismatch problems that come with the browser reaching
    Keycloak through a different network path than this server does."""
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": KEYCLOAK_CLIENT_ID,
                "grant_type": "password",
                "username": username,
                "password": password,
            },
            timeout=10,
        )
    except requests.RequestException as e:
        st.error(f"Couldn't reach the identity service: {e}")
        return False
    if resp.status_code != 200:
        st.error("Invalid username or password.")
        return False
    st.session_state["access_token"] = resp.json()["access_token"]
    st.session_state["username"] = username
    return True


if "access_token" not in st.session_state:
    st.subheader("Log in")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        login_clicked = st.form_submit_button("Log in", type="primary")
    if login_clicked and username and password:
        if _login(username, password):
            st.rerun()
    st.stop()

with st.sidebar:
    st.caption(f"Logged in as **{st.session_state['username']}**")
    if st.button("Log out"):
        st.session_state.clear()
        st.rerun()
    st.header("Ask a question")
    department = st.text_input("Department (optional)", placeholder="e.g. Retail Banking")
    st.caption("Used only for the query log / compliance view below.")

with st.form("ask_form"):
    question = st.text_input(
        "Ask in English or Arabic",
        placeholder="e.g. What is the maximum daily cash deposit limit at the Dubai Main Branch?",
    )
    ask_clicked = st.form_submit_button("Ask", type="primary")

if ask_clicked and question.strip():
    with st.spinner("Checking the policy library..."):
        try:
            resp = requests.post(
                f"{API_URL}/ask",
                json={"question": question, "department": department or None},
                headers={"Authorization": f"Bearer {st.session_state['access_token']}"},
                timeout=120,
            )
            if resp.status_code == 401:
                st.error("Your session has expired - please log in again.")
                st.session_state.clear()
                st.rerun()
            resp.raise_for_status()
            st.session_state["last_result"] = resp.json()
        except requests.RequestException as e:
            st.error(f"Couldn't reach the assistant: {e}")

if "last_result" in st.session_state:
    result = st.session_state["last_result"]
    icon, label = CASE_LABELS.get(result["case"], ("", result["case"]))

    col_answer, col_sources = st.columns([3, 2])

    with col_answer:
        st.subheader(f"{icon} {label}")
        direction = "rtl" if result["language"] == "ar" else "ltr"
        st.markdown(
            f'<div dir="{direction}" style="font-size:1.15rem; line-height:1.7;">'
            f'{result["answer"]}</div>',
            unsafe_allow_html=True,
        )
        st.caption(f"🔢 {result.get('total_tokens', 0)} tokens (embedding + generation, from Ollama)")

    with col_sources:
        st.subheader("Source")
        if not result["citations"]:
            st.info("No policy document matched this question closely enough to cite.")
        for c in result["citations"]:
            with st.container(border=True):
                title_line = f"**{c['title']}**"
                if c.get("confidential"):
                    title_line += "  🔒 :orange[Confidential]"
                st.markdown(title_line)
                st.caption(f"{c['doc_id']} · v{c['version']}")
                status_word = "CURRENT" if c["status"] == "current" else "EXPIRED"
                status_color = "green" if c["status"] == "current" else "red"
                st.markdown(f":{status_color}[{status_word}]  ·  Effective {c['effective_date']}")
                st.caption(f"Approved by {c['approver_name']}, {c['approver_role']}")
                if c["governs_note"]:
                    st.warning(c["governs_note"])

st.divider()

with st.expander("Compliance dashboard"):
    tab_departments, tab_conflicts = st.tabs(["Query volume by department", "Pending policy conflicts"])

    with tab_departments:
        try:
            depts = requests.get(f"{API_URL}/departments", timeout=10).json()
            if depts:
                st.dataframe(depts, use_container_width=True, hide_index=True)
            else:
                st.info("No queries logged yet.")
        except requests.RequestException as e:
            st.error(f"Couldn't load department summary: {e}")

    with tab_conflicts:
        st.caption(
            "Candidate conflicts between current policies that don't explicitly "
            "declare a relationship - flagged automatically, never published "
            "until a human confirms them here."
        )
        try:
            pending = requests.get(f"{API_URL}/admin/pending-relationships", timeout=10).json()
        except requests.RequestException as e:
            st.error(f"Couldn't load pending relationships: {e}")
            pending = []

        if not pending:
            st.success("Nothing pending review.")
        for p in pending:
            with st.container(border=True):
                st.markdown(f"**{p['source_doc']}** vs **{p['target_doc']}**")
                st.caption(f"Confidence: {p['confidence']}")
                st.text(p["evidence"])
                col_approve, col_reject = st.columns(2)
                if col_approve.button("Approve", key=f"approve_{p['id']}"):
                    requests.post(
                        f"{API_URL}/admin/pending-relationships/{p['id']}/review",
                        json={"approve": True},
                        timeout=10,
                    )
                    st.rerun()
                if col_reject.button("Reject", key=f"reject_{p['id']}"):
                    requests.post(
                        f"{API_URL}/admin/pending-relationships/{p['id']}/review",
                        json={"approve": False},
                        timeout=10,
                    )
                    st.rerun()
