"""A chat created before ``display.busy_input_mode`` changed follows the profile's current mode.

A local session freezes its profile's config at creation, so ``config.get busy`` kept answering the
creation-time mode for every chat that already existed: ``hermes config set display.busy_input_mode
steer`` (or ``/busy steer`` typed in one chat) reached only the chats created afterwards, and an
older Desktop or TUI chat went on redirecting the running turn. Main's ``config.get busy`` read
config.yaml on every call. A session's own ``/busy`` still wins, an unset or invalid profile value
leaves the frozen one, and a bypass launch never reads the profile.
"""
import json
from types import SimpleNamespace

import pytest

from gateway.session_contract import SessionRef

_NO_FILE = object()
_INTERRUPT, _STEER = {'busy_input_mode': 'interrupt'}, {'busy_input_mode': 'steer'}


def _write_profile(home, config):
    (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')


def _session(monkeypatch, home, *, source='gui', frozen=None, profile=_NO_FILE, override=None,
             ignore_user_config=False):
    """A live local session whose policy froze ``frozen`` as its ``display`` section, in a profile
    home whose config.yaml now holds ``profile``."""
    from gateway import session_policy, session_setting_commands
    if profile is not _NO_FILE:
        _write_profile(home, profile)
    live = SimpleNamespace(source=SimpleNamespace(platform='local'), busy_input_mode=override)
    authority = SimpleNamespace(sessions={'s': live}, authorize=lambda actor, ref, capability: None,
                                runner=SimpleNamespace(), profile_id=str(home),
                                db=SimpleNamespace(db_path=str(home / 'state.db')))
    policy = SimpleNamespace(source=source, ignore_user_config=ignore_user_config,
                             config=lambda *a, **k: {'display': frozen} if frozen is not None else {})
    monkeypatch.setattr(session_policy, 'policy_for_source', lambda runner, src: policy)
    monkeypatch.setattr(session_setting_commands, 'local_authority_live', lambda runner, src: (authority, live))
    return SimpleNamespace(authority=authority, actor=None), live


async def _busy(connection, **write):
    from gateway.session_busy_controls import busy_config
    params = {'session_id': 's', 'key': 'busy', **write}
    return await busy_config(connection, SessionRef('p', 's'), params, write=bool(write))


@pytest.mark.asyncio
@pytest.mark.parametrize(('source', 'frozen', 'profile', 'override', 'bypass', 'expected'), [
    # The mode changed after the chat was created: the chat follows it.
    ('gui', _INTERRUPT, {'display': _STEER}, None, False, 'steer'),
    ('cli', _INTERRUPT, {'display': {'busy_input_mode': 'queue'}}, None, False, 'queue'),
    ('tui', None, {'display': _STEER}, None, False, 'steer'),
    ('gui', _STEER, {'display': {'busy_input_mode': ' Interrupt '}}, None, False, 'interrupt'),
    # This chat's own /busy is the explicit choice.
    ('gui', _INTERRUPT, {'display': _STEER}, 'queue', False, 'queue'),
    # Nothing explicit (or nothing valid) in the profile now: the frozen mode stands.
    ('gui', _STEER, {'display': {}}, None, False, 'steer'),
    ('gui', _STEER, {}, None, False, 'steer'),
    ('gui', _STEER, {'display': {'busy_input_mode': 'loud'}}, None, False, 'steer'),
    ('gui', _STEER, {'display': {'busy_input_mode': 3}}, None, False, 'steer'),
    ('gui', _STEER, {'display': 'compact'}, None, False, 'steer'),
    ('gui', _STEER, _NO_FILE, None, False, 'steer'),
    # ...and with neither, the surface default (main's TUI queues).
    ('tui', None, {}, None, False, 'queue'),
    ('gui', None, {}, None, False, 'interrupt'),
    # A bypass launch never reads the profile; a cron route is not a user's chat.
    ('gui', _INTERRUPT, {'display': _STEER}, None, True, 'interrupt'),
    ('cron', _INTERRUPT, {'display': _STEER}, None, False, 'interrupt'),
])
async def test_existing_chat_reports_the_profiles_current_busy_mode(
        monkeypatch, tmp_path, source, frozen, profile, override, bypass, expected):
    from gateway.session_setting_commands import session_busy_mode
    connection, live = _session(monkeypatch, tmp_path, source=source, frozen=frozen, profile=profile,
                                override=override, ignore_user_config=bypass)
    assert await _busy(connection) == {'key': 'busy', 'value': expected, 'scope': 'session'}
    # `/busy status` reads the same answer.
    assert session_busy_mode(connection.authority.runner, live) == expected


@pytest.mark.asyncio
async def test_each_later_change_reaches_the_chat_without_a_restart(monkeypatch, tmp_path):
    connection, _ = _session(monkeypatch, tmp_path, frozen=_INTERRUPT, profile={'display': _INTERRUPT})
    assert (await _busy(connection))['value'] == 'interrupt'
    for mode in ('steer', 'queue', 'interrupt', 'steer'):
        _write_profile(tmp_path, {'display': {'busy_input_mode': mode, 'compact': mode == 'queue'}})
        assert (await _busy(connection))['value'] == mode


@pytest.mark.asyncio
async def test_a_session_busy_write_stays_session_scoped(monkeypatch, tmp_path):
    connection, live = _session(monkeypatch, tmp_path, frozen=_INTERRUPT, profile={'display': _STEER})
    before = (tmp_path / 'config.yaml').read_bytes()
    assert await _busy(connection, value='queue') == {'key': 'busy', 'value': 'queue', 'scope': 'session'}
    assert live.busy_input_mode == 'queue' and (await _busy(connection))['value'] == 'queue'
    # A settings write it is not: the profile keeps its own mode for every other chat.
    assert (tmp_path / 'config.yaml').read_bytes() == before


def test_profile_mode_is_none_without_a_profile_home(tmp_path):
    from gateway.session_setting_commands import profile_busy_input_mode
    policy = SimpleNamespace(source='gui', ignore_user_config=False)
    assert profile_busy_input_mode(None, policy) is None
    assert profile_busy_input_mode(SimpleNamespace(), policy) is None  # an authority with no store
