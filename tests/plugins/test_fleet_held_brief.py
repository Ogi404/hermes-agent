"""Held Slack-brief intake contracts; real durable state, no live service calls."""
import argparse
import copy
import importlib.util

import pytest

from plugins.fleet_lifecycle.controller import Halt, command, setup_cli


def capture():
    assert importlib.util.find_spec('plugins.fleet_lifecycle.brief') is not None, 'missing constrained held-brief consumer'
    from plugins.fleet_lifecycle.brief import capture_brief
    return capture_brief


@pytest.fixture
def inputs(tmp_path):
    import os
    for parent in (tmp_path, *tmp_path.parents):
        mode = parent.stat()
        if mode.st_uid == os.getuid() and not mode.st_mode & 0o1000:
            os.chmod(parent, 0o700)
    policy = {'schema': 1, 'owner': 'UOWNER', 'team': 'TWORK',
              'state_root': str(tmp_path / 'briefs'),
              'projects': [{'slug': 'fixture-app', 'channel': 'CAPP',
                            'linear_team': '11111111-1111-4111-8111-111111111111',
                            'linear_project': '22222222-2222-4222-8222-222222222222',
                            'held_state': 'Backlog'}]}
    message = {'ts': '1794656260.660269', 'user': 'UOWNER', 'text': 'Make Library pink.\nNo deployment.'}
    calls = []
    created = {}
    def read_message(channel, ts):
        assert channel == 'CAPP' and ts == '1794656260.660269'
        return copy.deepcopy(message)
    def linear(name, args):
        calls.append((name, copy.deepcopy(args)))
        if name == 'save_issue':
            assert 'id' not in args
            created.update(id='ENS-100', uuid='33333333-3333-4333-8333-333333333333',
                           title=args['title'], description=args['description'],
                           projectId=args['project'], teamId=args['team'],
                           status='Backlog', statusType='backlog', labels=[],
                           url='https://linear.app/fixture/issue/ENS-100')
        else:
            assert name == 'get_issue' and args['id'] == 'ENS-100'
        return copy.deepcopy(created)
    return policy, message, calls, read_message, linear, created


def run(inputs):
    policy, message, _, read_message, linear, _ = inputs
    return capture()(policy, 'fixture-app', message['ts'], read_message, linear)


def test_capture_preserves_human_text_and_creates_only_one_held_issue(inputs):
    result = run(inputs)
    policy, message, calls, _, _, _ = inputs
    payload = calls[0][1]
    assert calls[0][0] == 'save_issue'
    assert payload['description'].startswith(message['text'] + '\n\n')
    assert payload['labels'] == [] and payload['state'] == 'Backlog'
    assert payload['assignee'] is None and payload['delegate'] is None
    assert payload['team'] == policy['projects'][0]['linear_team']
    assert payload['project'] == policy['projects'][0]['linear_project']
    assert result['stage'] == 'source_captured_held' and result['issue_id'] == 'ENS-100'
    assert result['execution_authorized'] is False
    assert run(inputs) == result
    assert [name for name, _ in calls].count('save_issue') == 1


@pytest.mark.parametrize('case', ['actor', 'bot', 'timestamp', 'empty', 'extra_policy', 'duplicate_project'])
def test_rejected_source_or_policy_makes_no_linear_call_or_state(inputs, case):
    policy, message, calls, _, _, _ = inputs
    if case == 'actor': message['user'] = 'UOTHER'
    elif case == 'bot': message['bot_id'] = 'BBOT'
    elif case == 'timestamp': message['ts'] = '1794656261.660269'
    elif case == 'empty': message['text'] = '   '
    elif case == 'extra_policy': policy['generic_write'] = True
    else: policy['projects'].append(copy.deepcopy(policy['projects'][0]))
    with pytest.raises(Halt):
        capture()(policy, 'fixture-app', '1794656260.660269', inputs[3], inputs[4])
    assert calls == []
    from pathlib import Path
    assert not Path(policy['state_root']).exists()


def test_ambiguous_write_is_never_replayed(inputs):
    policy, message, calls, read_message, _, _ = inputs
    def broken(name, args):
        calls.append((name, args))
        raise TimeoutError('reply lost')
    with pytest.raises(Halt, match='brief_capture_uncertain'):
        capture()(policy, 'fixture-app', message['ts'], read_message, broken)
    with pytest.raises(Halt, match='brief_capture_uncertain'):
        capture()(policy, 'fixture-app', message['ts'], read_message, broken)
    assert len(calls) == 1


def test_wrong_remote_readback_never_certifies_success_or_retries_write(inputs):
    policy, message, calls, read_message, linear, created = inputs
    def wrong(name, args):
        result = linear(name, args)
        if name == 'get_issue': result['labels'] = ['agent-ready']
        return result
    for _ in range(2):
        with pytest.raises(Halt):
            capture()(policy, 'fixture-app', message['ts'], read_message, wrong)
    assert [name for name, _ in calls].count('save_issue') == 1


def test_changed_message_cannot_reuse_existing_capture(inputs):
    run(inputs)
    inputs[1]['text'] = 'A different scope'
    with pytest.raises(Halt, match='brief_source_changed'):
        run(inputs)
    assert [name for name, _ in inputs[2]].count('save_issue') == 1


def test_native_cli_routes_capture_without_loading_an_execution_plan(monkeypatch):
    parser = argparse.ArgumentParser()
    setup_cli(parser)
    try:
        args = parser.parse_args(['capture-brief', '--project', 'fixture-app', '--message-ts', '1794656260.660269'])
    except SystemExit:
        pytest.fail('missing native capture-brief CLI consumer')
    capture()
    from plugins.fleet_lifecycle import brief
    invoked = []
    monkeypatch.setattr(brief, 'capture_command', lambda value: invoked.append(value.project) or 0)
    assert command(args) == 0 and invoked == ['fixture-app']


@pytest.mark.parametrize('remote_error', [False, True])
def test_raw_linear_adapter_uses_actual_sdk_shape_and_never_replays(monkeypatch, remote_error):
    capture()
    import asyncio
    import json
    from types import SimpleNamespace
    from mcp.types import CallToolResult, TextContent
    from plugins.fleet_lifecycle import brief
    from tools import mcp_tool_discovery, mcp_tool_loop
    calls = []
    class Session:
        async def call_tool(self, name, *, arguments):
            calls.append((name, arguments))
            return CallToolResult(content=[TextContent(type='text', text=json.dumps({'id': 'ENS-100'}))],
                                  is_error=remote_error)
    server = SimpleNamespace(session=Session(), _rpc_lock=asyncio.Lock())
    monkeypatch.setattr(mcp_tool_discovery, '_get_connected_server_for_call', lambda name: server if name == 'linear' else None)
    monkeypatch.setattr(mcp_tool_loop, '_run_on_mcp_loop', lambda factory, timeout: asyncio.run(factory()))
    if remote_error:
        with pytest.raises(Halt, match='brief_linear_failed'):
            brief._linear_call('save_issue', {'title': 'Pinned payload'})
    else:
        assert brief._linear_call('save_issue', {'title': 'Pinned payload'}) == {'id': 'ENS-100'}
    assert calls == [('save_issue', {'title': 'Pinned payload'})]
    with pytest.raises(Halt, match='brief_operation_invalid'):
        brief._linear_call('save_comment', {})
    assert len(calls) == 1


def test_capture_command_fetches_exact_message_despite_newer_channel_traffic(inputs, monkeypatch, capsys):
    capture()
    import json
    import os
    from pathlib import Path
    from types import SimpleNamespace
    import dotenv
    import hermes_constants
    import sys
    from tools import mcp_tool_discovery, mcp_tool_lifecycle
    from plugins.fleet_lifecycle import brief
    policy, message, calls, _, linear, _ = inputs
    home = Path(policy['state_root']).parent
    path = home / 'fleet-brief-policy.json'
    path.write_text(json.dumps(policy))
    os.chmod(path, 0o600)
    observed = []
    class Slack:
        def auth_test(self): return {'team_id': policy['team']}
        def conversations_history(self, **kwargs):
            observed.append(kwargs)
            found = message if kwargs.get('latest') == message['ts'] else {
                'ts': '1794656261.660269', 'user': 'UOTHER', 'text': 'Newer traffic'}
            return {'messages': [found]}
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(dotenv, 'dotenv_values', lambda path: {'SLACK_BOT_TOKEN': 'fixture-only'})
    monkeypatch.setitem(sys.modules, 'slack_sdk', SimpleNamespace(WebClient=lambda **kwargs: Slack()))
    monkeypatch.setattr(mcp_tool_discovery, 'discover_mcp_tools', lambda **kwargs: [])
    monkeypatch.setattr(mcp_tool_lifecycle, 'shutdown_mcp_servers', lambda: None)
    monkeypatch.setattr(brief, '_linear_call', linear)
    assert brief.capture_command(SimpleNamespace(project='fixture-app', message_ts=message['ts'])) == 0
    assert observed == [{'channel': 'CAPP', 'oldest': message['ts'], 'latest': message['ts'],
                         'inclusive': True, 'limit': 1}]
    assert json.loads(capsys.readouterr().out)['stage'] == 'source_captured_held'


def test_other_backlog_state_cannot_certify_exact_held_state(inputs):
    policy, message, calls, read_message, linear, _ = inputs
    def wrong(name, args):
        issue = linear(name, args)
        if name == 'get_issue': issue['status'] = 'Another Backlog'
        return issue
    with pytest.raises(Halt, match='brief_readback_invalid'):
        capture()(policy, 'fixture-app', message['ts'], read_message, wrong)
    assert [name for name, _ in calls].count('save_issue') == 1


def test_remote_removal_of_only_trailing_lf_before_provenance_is_accepted(inputs):
    import json
    from pathlib import Path
    policy, message, calls, read_message, linear, _ = inputs
    message['text'] += '\n'
    exact_text = message['text']
    def normalized(name, args):
        issue = linear(name, args)
        if name == 'get_issue':
            body, separator, marker = issue['description'].rpartition('\n\n<!-- fleet-brief-source:')
            assert separator
            issue['description'] = body.rstrip('\n') + separator + marker
        return issue
    result = capture()(policy, 'fixture-app', message['ts'], read_message, normalized)
    assert result['stage'] == 'source_captured_held'
    intent = json.loads(next(Path(policy['state_root']).glob('*/intent.json')).read_text())
    assert intent['payload']['description'].startswith(exact_text + '\n\n<!-- fleet-brief-source:')
    assert message['text'] == exact_text
    assert [name for name, _ in calls].count('save_issue') == 1


@pytest.mark.parametrize('case', ['internal_newline', 'source_space', 'marker', 'suffix'])
def test_footer_tolerance_cannot_hide_source_or_provenance_changes(inputs, case):
    policy, message, calls, read_message, linear, _ = inputs
    message['text'] += '\n'
    def altered(name, args):
        issue = linear(name, args)
        if name == 'get_issue':
            body, separator, marker = issue['description'].rpartition('\n\n<!-- fleet-brief-source:')
            body = body.rstrip('\n')
            if case == 'internal_newline': body = body.replace('\n', '\n\n', 1)
            elif case == 'source_space': body += ' '
            elif case == 'marker': marker = '0' * 64 + ' -->'
            else: marker += '\nextra'
            issue['description'] = body + separator + marker
        return issue
    with pytest.raises(Halt, match='brief_readback_invalid'):
        capture()(policy, 'fixture-app', message['ts'], read_message, altered)
    from pathlib import Path
    assert not list(Path(policy['state_root']).glob('*/receipt.json'))
    assert [name for name, _ in calls].count('save_issue') == 1


def reconciler():
    capture()
    from plugins.fleet_lifecycle import brief
    assert hasattr(brief, 'reconcile_brief'), 'missing read-only existing-source reconciliation'
    return brief.reconcile_brief


def failed_capture(inputs):
    policy, message, calls, read_message, linear, _ = inputs
    message['text'] += '\n'
    def failed_readback(name, args):
        result = linear(name, args)
        if name == 'get_issue': raise Halt('brief_readback_invalid')
        return result
    with pytest.raises(Halt, match='brief_readback_invalid'):
        capture()(policy, 'fixture-app', message['ts'], read_message, failed_readback)
    from pathlib import Path
    intent = next(Path(policy['state_root']).glob('*/intent.json'))
    assert not (intent.parent / 'receipt.json').exists()
    return intent, intent.read_bytes()


def test_reconcile_existing_failed_capture_is_read_only_and_preserves_original_intent(inputs):
    reconcile = reconciler()
    policy, message, calls, read_message, linear, _ = inputs
    intent, before = failed_capture(inputs)
    def readonly(name, args):
        assert name == 'get_issue', 'reconciliation may never create/update a remote issue'
        issue = linear(name, args)
        body, separator, marker = issue['description'].rpartition('\n\n<!-- fleet-brief-source:')
        issue['description'] = body.rstrip('\n') + separator + marker
        return issue
    result = reconcile(policy, 'fixture-app', message['ts'], 'ENS-100', read_message, readonly)
    assert result['stage'] == 'source_reconciled_held' and result['source_write_calls'] == 0
    assert result['issue_id'] == 'ENS-100' and result['execution_authorized'] is False
    assert intent.read_bytes() == before
    import json
    receipt = json.loads((intent.parent / 'receipt.json').read_text())
    audit = json.loads((intent.parent / 'reconciliation.json').read_text())
    assert receipt['stage'] == 'source_captured_held'
    assert audit['mode'] == 'existing_source_read_only' and audit['source_write_calls'] == 0
    assert reconcile(policy, 'fixture-app', message['ts'], 'ENS-100', read_message, readonly) == result
    assert run(inputs) == receipt
    assert [name for name, _ in calls].count('save_issue') == 1


def test_reconcile_requires_prior_durable_intent_without_creating_state(inputs):
    reconcile = reconciler()
    policy, message, calls, read_message, linear, _ = inputs
    with pytest.raises(Halt, match='brief_reconciliation_no_intent'):
        reconcile(policy, 'fixture-app', message['ts'], 'ENS-100', read_message, linear)
    from pathlib import Path
    assert calls == [] and not Path(policy['state_root']).exists()


def test_reconcile_cannot_change_already_certified_issue_identity(inputs):
    reconcile = reconciler()
    receipt = run(inputs)
    policy, message, calls, read_message, linear, _ = inputs
    before = copy.deepcopy(calls)
    with pytest.raises(Halt, match='brief_reconciliation_id_mismatch'):
        reconcile(policy, 'fixture-app', message['ts'], 'ENS-101', read_message, linear)
    assert calls == before and receipt['issue_id'] == 'ENS-100'


@pytest.mark.parametrize('case', ['marker', 'readiness', 'other_issue'])
def test_reconcile_rejects_mismatched_remote_scope_without_write_or_receipt(inputs, case):
    reconcile = reconciler()
    intent, before = failed_capture(inputs)
    policy, message, calls, read_message, linear, _ = inputs
    def readonly(name, args):
        assert name == 'get_issue'
        issue = linear(name, args)
        if case == 'marker': issue['description'] = 'different provenance'
        elif case == 'readiness': issue['labels'] = ['agent-ready']
        else: issue['id'] = 'ENS-101'
        return issue
    with pytest.raises(Halt):
        reconcile(policy, 'fixture-app', message['ts'], 'ENS-100', read_message, readonly)
    assert intent.read_bytes() == before
    assert not (intent.parent / 'receipt.json').exists()
    assert not (intent.parent / 'reconciliation.json').exists()
    assert [name for name, _ in calls].count('save_issue') == 1


def test_native_cli_routes_explicit_read_only_reconciliation_without_execution_plan(monkeypatch):
    parser = argparse.ArgumentParser();setup_cli(parser)
    try:args = parser.parse_args(['reconcile-brief', '--project', 'fixture-app', '--message-ts', '1794656260.660269', '--issue', 'ENS-100'])
    except SystemExit:pytest.fail('missing native reconcile-brief CLI consumer')
    capture()
    from plugins.fleet_lifecycle import brief
    observed = []
    monkeypatch.setattr(brief, 'capture_command', lambda value: observed.append((value.action, value.issue)) or 0)
    assert command(args) == 0 and observed == [('reconcile-brief', 'ENS-100')]
