"""Constrained Slack brief -> held Linear source; never eligibility or execution."""
from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path
import stat

from plugins.fleet_lifecycle.controller import (
    Halt, absolute, canonical, digest, fields, read_regular, require, safe_path,
    strict_json, token,
)


def _project(policy, slug):
    fields(policy, 'schema owner team state_root projects')
    require(type(policy['schema']) is int and policy['schema'] == 1, 'brief_policy_invalid')
    token(policy['owner'], r'U[A-Z0-9]{1,40}')
    token(policy['team'], r'T[A-Z0-9]{1,40}')
    absolute(policy['state_root'])
    projects = policy['projects']
    require(isinstance(projects, list) and 0 < len(projects) <= 32, 'brief_policy_invalid')
    names = []
    for project in projects:
        fields(project, 'slug channel linear_team linear_project held_state')
        token(project['slug'], r'[a-z0-9][a-z0-9_-]{0,79}')
        token(project['channel'], r'C[A-Z0-9]{1,40}')
        for key in ('linear_team', 'linear_project'):
            token(project[key], r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
        require(isinstance(project['held_state'], str) and 0 < len(project['held_state']) <= 80,
                'brief_policy_invalid')
        names.append(project['slug'])
    require(len(names) == len(set(names)), 'brief_policy_invalid')
    matches = [p for p in projects if p['slug'] == slug]
    require(len(matches) == 1, 'brief_project_not_allowed')
    return matches[0]


def _save(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _verify(issue, payload):
    require(isinstance(issue, dict), 'brief_readback_invalid')
    require(issue.get('title') == payload['title'], 'brief_readback_invalid')
    expected = payload['description']
    if issue.get('description') != expected:
        # Linear can remove source-trailing LF at the provenance boundary.
        # Never normalize interior text, spaces, marker bytes or other fields.
        footer = re.search(r'(\n\n<!-- fleet-brief-source:[0-9a-f]{64} -->)\Z', expected)
        require(footer is not None and issue.get('description') == expected[:footer.start()].rstrip('\n') + footer.group(0),
                'brief_readback_invalid')
    require(issue.get('projectId') == payload['project'] and issue.get('teamId') == payload['team']
            and issue.get('status') == payload['state']
            and issue.get('statusType') == 'backlog' and issue.get('labels') == []
            and issue.get('assigneeId') is None and issue.get('delegateId') is None,
            'brief_readback_invalid')
    token(issue.get('id'), r'[A-Z][A-Z0-9]*-[1-9][0-9]*')
    token(issue.get('uuid'), r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
    require(isinstance(issue.get('url'), str) and issue['url'].startswith('https://linear.app/'),
            'brief_readback_invalid')


def _capture_locked(directory, source_hash, payload, call_linear, *, existing_issue_id=None):
    intent = directory / 'intent.json'
    receipt = directory / 'receipt.json'
    if existing_issue_id is not None:
        require(intent.exists(), 'brief_reconciliation_no_intent')
    if intent.exists():
        previous = strict_json(read_regular(safe_path(intent, private=True), 96 * 1024))
        require(previous['source_hash'] == source_hash, 'brief_source_changed')
        require(previous['payload'] == payload, 'brief_source_changed')
        if receipt.exists():
            prior = strict_json(read_regular(safe_path(receipt, private=True), 96 * 1024))
            issue_id = prior['issue_id']
            require(existing_issue_id is None or existing_issue_id == issue_id,
                    'brief_reconciliation_id_mismatch')
        else:
            require(existing_issue_id is not None, 'brief_capture_uncertain')
            issue_id = existing_issue_id
    else:
        _save(intent, {'source_hash': source_hash, 'payload': payload})
        # Only a fresh capture can call save_issue. Reconciliation never does.
        try:
            created = call_linear('save_issue', payload)
            require(isinstance(created, dict) and isinstance(created.get('id'), str),
                    'brief_capture_uncertain')
            issue_id = created['id']
        except Exception:
            raise Halt('brief_capture_uncertain') from None
    issue = call_linear('get_issue', {'id': issue_id, 'fields': [
        'id', 'uuid', 'title', 'description', 'projectId', 'teamId', 'status',
        'statusType', 'labels', 'assigneeId', 'delegateId', 'url',
    ]})
    _verify(issue, payload)
    require(existing_issue_id is None or issue['id'] == existing_issue_id,
            'brief_reconciliation_id_mismatch')
    result = {'stage': 'source_captured_held', 'issue_id': issue['id'],
              'linear_id': issue['uuid'], 'url': issue['url'],
              'source_hash': source_hash, 'execution_authorized': False}
    if existing_issue_id is not None:
        # Method evidence is not a completion marker; receipt publication below
        # remains the success boundary. An interrupted reconciliation is read-only.
        audit = {'mode': 'existing_source_read_only', 'issue_id': issue['id'],
                 'linear_id': issue['uuid'], 'source_hash': source_hash,
                 'intent_sha256': digest(read_regular(safe_path(intent, private=True), 96 * 1024)),
                 'expected_receipt_sha256': digest(canonical(result)), 'source_write_calls': 0}
        audit_path = directory / 'reconciliation.json'
        if audit_path.exists():
            require(strict_json(read_regular(safe_path(audit_path, private=True), 96 * 1024)) == audit,
                    'brief_reconciliation_id_mismatch')
        else:
            _save(audit_path, audit)
    if receipt.exists():
        require(prior == result, 'brief_readback_invalid')
    else:
        _save(receipt, result)
    return result


def capture_brief(policy, slug, ts, read_message, call_linear):
    """Create one held issue; never replay a write whose result is uncertain."""
    return _brief_operation(policy, slug, ts, read_message, call_linear)


def reconcile_brief(policy, slug, ts, issue_id, read_message, call_linear):
    """Verify an explicitly identified existing issue, then certify local state."""
    token(issue_id, r'[A-Z][A-Z0-9]*-[1-9][0-9]*')
    result = _brief_operation(policy, slug, ts, read_message, call_linear, existing_issue_id=issue_id)
    return {**result, 'stage': 'source_reconciled_held', 'source_write_calls': 0}


def _brief_operation(policy, slug, ts, read_message, call_linear, *, existing_issue_id=None):
    """Create one held issue from a verified human message; no caller write fields."""
    project = _project(policy, slug)
    token(ts, r'[0-9]{10,16}[.][0-9]{6}')
    message = read_message(project['channel'], ts)
    require(isinstance(message, dict) and message.get('ts') == ts
            and message.get('user') == policy['owner'] and not message.get('bot_id')
            and message.get('subtype') in (None, 'thread_broadcast'), 'brief_source_invalid')
    text = message.get('text')
    require(isinstance(text, str) and bool(text.strip())
            and len(text.encode('utf-8')) <= 48 * 1024, 'brief_source_invalid')
    identity = {'team': policy['team'], 'channel': project['channel'], 'ts': ts, 'project': slug}
    key = digest(canonical(identity))
    source_hash = digest(canonical({**identity, 'owner': policy['owner'], 'text': text}))
    payload = {'team': project['linear_team'], 'project': project['linear_project'],
               'title': text.strip().splitlines()[0][:200],
               'description': text + '\n\n<!-- fleet-brief-source:' + key + ' -->',
               'state': project['held_state'], 'labels': [], 'priority': 0,
               'assignee': None, 'delegate': None}
    root = Path(policy['state_root'])
    safe_path(root.parent, directory=True, private=True)
    if existing_issue_id is None:
        root.mkdir(mode=0o700, exist_ok=True)
    else:
        require(root.is_dir(), 'brief_reconciliation_no_intent')
    safe_path(root, directory=True, private=True)
    directory = root / key
    if existing_issue_id is None:
        directory.mkdir(mode=0o700, exist_ok=True)
    else:
        require(directory.is_dir(), 'brief_reconciliation_no_intent')
    safe_path(directory, directory=True, private=True)
    if existing_issue_id is not None:
        require((directory / 'intent.json').is_file(), 'brief_reconciliation_no_intent')
    flags = os.O_WRONLY | os.O_NOFOLLOW
    if existing_issue_id is None:
        flags |= os.O_CREAT
    fd = os.open(directory / 'capture.lock', flags, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600, 'brief_lock_invalid')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _capture_locked(directory, source_hash, payload, call_linear, existing_issue_id=existing_issue_id)
    finally:
        os.close(fd)


def _linear_call(name, arguments):
    """Fixed trusted operations, not a Director-registered generic write tool."""
    require(name in ('save_issue', 'get_issue'), 'brief_operation_invalid')
    from tools.mcp_tool_discovery import _get_connected_server_for_call
    from tools.mcp_tool_loop import _run_on_mcp_loop
    server = _get_connected_server_for_call('linear')
    require(server is not None and server.session is not None, 'brief_linear_unavailable')
    async def invoke():
        async with server._rpc_lock:
            return await server.session.call_tool(name, arguments=arguments)
    # Raw single RPC deliberately avoids a write-replaying reconnection wrapper.
    response = _run_on_mcp_loop(invoke, timeout=90)
    data = response.model_dump(by_alias=True)
    require(not data.get('isError'), 'brief_linear_failed')
    value = data.get('structuredContent')
    if isinstance(value, dict):
        return value
    chunks = data.get('content', [])
    require(len(chunks) == 1 and chunks[0].get('type') == 'text', 'brief_linear_failed')
    value = strict_json(chunks[0]['text'])
    require(isinstance(value, dict), 'brief_linear_failed')
    return value


def capture_command(args):
    from hermes_constants import get_hermes_home
    from dotenv import dotenv_values
    from slack_sdk import WebClient
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    home = get_hermes_home()
    policy_path = safe_path(home / 'fleet-brief-policy.json', private=True)
    policy = strict_json(read_regular(policy_path, 64 * 1024))
    _project(policy, args.project)
    client = WebClient(token=dotenv_values(home / '.env').get('SLACK_BOT_TOKEN'), timeout=30)
    require(client.auth_test()['team_id'] == policy['team'], 'brief_workspace_invalid')
    def read_message(channel, ts):
        result = client.conversations_history(channel=channel, oldest=ts, latest=ts, inclusive=True, limit=1)
        messages = result.get('messages', [])
        require(len(messages) == 1, 'brief_source_invalid')
        return messages[0]
    try:
        discover_mcp_tools(allowed_mcp_names=['linear'])
        if getattr(args, 'action', 'capture-brief') == 'reconcile-brief':
            result = reconcile_brief(policy, args.project, args.message_ts, args.issue, read_message, _linear_call)
        else:
            result = capture_brief(policy, args.project, args.message_ts, read_message, _linear_call)
        print(canonical(result).decode())
        return 0
    finally:
        shutdown_mcp_servers()
