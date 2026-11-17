"""Server-side access gate for the private Streamlit dashboards."""
import hashlib
import hmac
import threading
import time
import uuid
from pathlib import Path

import streamlit as st

KEY = '_private_dashboard_authorized'
ATTEMPTS = '_private_dashboard_attempts'
RETRY = '_private_dashboard_retry'
LAST = '_private_dashboard_last_input'
NONCE = '_private_dashboard_login_id'

def idle_minutes():
    try:
        minutes = int(read_secret('APP_IDLE_MINUTES') or '10')
        return minutes if 1 <= minutes <= 120 else 10
    except ValueError:
        return 10

def remaining_seconds(now, last, minutes):
    return max(0., minutes * 60 - (now - last))

def _lock():
    for k in (KEY, LAST, NONCE):
        st.session_state.pop(k, None)
    st.session_state['_private_lock_message'] = True

def _receive_activity():
    ss = st.session_state
    if not ss.get(KEY):
        return
    now = time.monotonic()
    previous = ss.get(LAST, 0.)
    try:
        elapsed = float(ss['_private_idle_watcher'].activity) / 1000
    except (KeyError, AttributeError, TypeError, ValueError):
        return
    if 0 <= elapsed <= 120 and remaining_seconds(now, previous, idle_minutes()):
        ss[LAST] = max(previous, now - elapsed)

@st.cache_resource
def _activity_component():
    return st.components.v2.component('dhan_private_activity', js=Path(__file__).with_name('dashboard_idle.js').read_text(encoding='utf-8'))



def read_secret(name):
    """Never use a widget or a client-side credential for this."""
    try:
        value = st.secrets.get(name, '')
        return str(value).strip() if value is not None else ''
    except (FileNotFoundError, KeyError):
        return ''


@st.cache_resource
def _shared_throttle():
    # Bound guessing even when an unauthenticated visitor reconnects.
    return threading.Lock(), {'credits': 12., 'last': time.monotonic()}


def _take_attempt():
    lock, state = _shared_throttle()
    with lock:
        now = time.monotonic()
        state['credits'] = min(12., state['credits'] + (now-state['last']) / 5.)
        state['last'] = now
        if state['credits'] < 1.:
            return False
        state['credits'] -= 1.
        return True


def _compare(password, attempt):
    return hmac.compare_digest(password.encode('utf-8'), attempt.encode('utf-8'))


def _proof(password):
    return hashlib.sha256(('dhan-dashboard-session-v1\0'+password).encode('utf-8')).digest()


def is_authenticated():
    password = read_secret('APP_PASSWORD')
    proof = st.session_state.get(KEY)
    return (len(password) >= 12 and isinstance(proof, bytes) and hmac.compare_digest(proof, _proof(password))
            and remaining_seconds(time.monotonic(), st.session_state.get(LAST, 0.), idle_minutes()) > 0)


@st.fragment(run_every=3)
def _monitor_idle():
    if not is_authenticated():
        st.rerun()
    _activity_component()(key='_private_idle_watcher',
        data={'id':st.session_state[NONCE], 'limit':idle_minutes()*60,
              'remaining':remaining_seconds(time.monotonic(),st.session_state[LAST],idle_minutes())},
        on_activity_change=_receive_activity,on_idle_change=_lock,width=1,height=1)


def require_password(title):
    if is_authenticated():
        _monitor_idle()
        st.sidebar.caption(f'Locks after {idle_minutes()} minutes without mouse, touch or keyboard activity.')
        if st.sidebar.button('Sign out', key='_private_signout'):
            st.session_state.clear()
            st.rerun()
        return
    if st.session_state.get(KEY):
        _lock()
    st.title(title)
    if st.session_state.pop('_private_lock_message',False):
        st.info('Dashboard locked. Enter your password to continue.')
    st.caption('Private dashboard · enter the password provided by the owner.')
    password = read_secret('APP_PASSWORD')
    if len(password) < 12:
        st.error('Access is disabled until the owner sets APP_PASSWORD (at least 12 characters) in Streamlit Secrets.')
        st.stop()
    with st.form('_private_dashboard_login', clear_on_submit=True):
        candidate = st.text_input('Dashboard password', type='password', max_chars=256)
        submitted = st.form_submit_button('Unlock dashboard', type='primary')
    if submitted:
        now = time.monotonic()
        if now < st.session_state.get(RETRY, 0) or not _take_attempt():
            st.error('Too many attempts. Wait a little and try again.')
        elif _compare(password, candidate):
            st.session_state[KEY] = _proof(password)
            st.session_state[LAST] = now
            st.session_state[NONCE] = uuid.uuid4().hex
            st.session_state.pop(ATTEMPTS, None)
            st.session_state.pop(RETRY, None)
            st.rerun()
        else:
            failures = st.session_state.get(ATTEMPTS, 0) + 1
            st.session_state[ATTEMPTS] = failures
            if failures >= 5:
                st.session_state[RETRY] = now + min(300, 30*2**min(failures-5,4))
            st.error('Incorrect dashboard password.')
    st.stop()
