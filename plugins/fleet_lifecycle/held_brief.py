"""Authenticated captured source -> born-blocked native task, never eligibility."""
from __future__ import annotations

import copy
from pathlib import Path

from plugins.fleet_lifecycle import brief
from plugins.fleet_lifecycle.controller import (
    Halt, canonical, digest, read_regular, require, safe_path, strict_json, token,
)


def project_captured_brief(brief_policy, lifecycle_policy, slug, ts, read_message,
                          call_linear, normalize_source, project_id, *, board, conn):
    from plugins.fleet_lifecycle.held_projection import project_held_contract
    from plugins.fleet_lifecycle.intake import project_policy

    project = brief._project(brief_policy, slug)
    project_policy(lifecycle_policy, slug, board=board)
    token(ts, r'[0-9]{10,16}[.][0-9]{6}')
    message = read_message(project['channel'], ts)
    require(isinstance(message, dict) and message.get('ts') == ts
            and message.get('user') == brief_policy['owner'] and not message.get('bot_id')
            and message.get('subtype') in (None, 'thread_broadcast'), 'brief_source_invalid')
    text = message.get('text')
    require(isinstance(text, str) and bool(text.strip())
            and len(text.encode()) <= 48 * 1024, 'brief_source_invalid')
    identity = {'team': brief_policy['team'], 'channel': project['channel'], 'ts': ts, 'project': slug}
    key = digest(canonical(identity))
    source_hash = digest(canonical({**identity, 'owner': brief_policy['owner'], 'text': text}))
    directory = safe_path(Path(brief_policy['state_root']) / key, directory=True, private=True)
    try:
        paths = [safe_path(directory / n, private=True) for n in ('intent.json', 'receipt.json')]
        before = [read_regular(p, 96 * 1024) for p in paths]
    except OSError:
        raise Halt('brief_capture_invalid') from None
    intent, receipt = [strict_json(raw) for raw in before]
    payload = {'team': project['linear_team'], 'project': project['linear_project'],
               'title': text.strip().splitlines()[0][:200],
               'description': text + '\n\n<!-- fleet-brief-source:' + key + ' -->',
               'state': project['held_state'], 'labels': [], 'priority': 0,
               'assignee': None, 'delegate': None}
    require(intent == {'source_hash': source_hash, 'payload': payload}, 'brief_source_changed')
    require(receipt.get('stage') == 'source_captured_held' and receipt.get('source_hash') == source_hash
            and receipt.get('execution_authorized') is False, 'brief_capture_invalid')
    token(receipt.get('issue_id'), r'[A-Z][A-Z0-9]*-[1-9][0-9]*')
    try:
        issue = call_linear('get_issue', {'id': receipt['issue_id'], 'includeRelations': True,
                                        'includeCustomerNeeds': False, 'includeReleases': False})
    except Exception:
        raise Halt('brief_source_read_failed') from None
    brief._verify(issue, payload)
    require(issue['id'] == receipt['issue_id'] and issue['uuid'] == receipt['linear_id']
            and issue['url'] == receipt['url'], 'brief_readback_invalid')
    contract = normalize_source(copy.deepcopy(issue))
    require(isinstance(contract, dict), 'held_snapshot_invalid')
    source_fields = {'issue_id': 'id', 'linear_id': 'uuid', 'title': 'title',
                     'description': 'description', 'status': 'status', 'url': 'url'}
    require(all(contract.get(k) == issue[v] for k, v in source_fields.items())
            and contract.get('repository') == slug, 'held_snapshot_invalid')
    # Source edits cannot be repaired by altering the frozen capture; no writes
    # or audit additions to those artifacts are part of projection.
    require(all(read_regular(p, 96 * 1024) == raw for p, raw in zip(paths, before)),
            'brief_source_changed')
    return project_held_contract(lifecycle_policy, contract, project_id, board=board, conn=conn)


_NORMALIZE = r'''
import datetime,hashlib,importlib.util,json,os,sys
p=json.load(sys.stdin);path=p['script'];raw=open(path,'rb').read()
if hashlib.sha256(raw).hexdigest()!=p['script_sha256']:raise RuntimeError('producer_changed')
spec=importlib.util.spec_from_file_location('trusted_installed_producer',path)
m=importlib.util.module_from_spec(spec);exec(compile(raw,path,'exec'),m.__dict__)
from pathlib import Path
m.HERMES_AGENT=Path(p['core']);m.HERMES_HOME=Path(p['home']);m.STATE_DIR=Path(p['state'])
m.ROUTES_FILE=Path(p['routes']);m.MANIFEST_FILE=Path(p['manifest'])
def deny(*a,**kw):raise RuntimeError('normalization_network_forbidden')
m.call_linear=deny
route=m.load_route(p['slug'])
if route['hermes_project']!=p['slug'] or route['board']!=p['board']:raise RuntimeError('route_changed')
issue=p['issue']
if issue.get('labels')!=[] or issue.get('project')!=route['linear_project']:raise RuntimeError('source_changed')
if not isinstance(issue.get('updatedAt'),str) or not issue['updatedAt']:raise RuntimeError('source_timestamp_missing')
relations=issue.get('relations')
if not isinstance(relations,dict):raise RuntimeError('source_relations_missing')
normalized,redactions=m.normalize_relations(relations)
if redactions or normalized['blockedBy'] or normalized['duplicateOf']:raise RuntimeError('source_relations_blocked')
contract=m.build_issue(issue,route,route['linear_project'])
contract['relations']=normalized
contract['contract_sha256']=m.contract_hash(contract)
if contract['redactions'] or contract['blocked']:raise RuntimeError('source_redacted')
contract['fetched_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
for field in ['script','routes','manifest']:
 if hashlib.sha256(open(p[field],'rb').read()).hexdigest()!=p[field+'_sha256']:raise RuntimeError('producer_changed')
print(json.dumps(contract))
'''


def normalize_installed_source(home, policy, slug, board, issue):
    """Use the fixed installed producer in an isolated, credentialless child.

    The deployed fetcher currently changes its process environment at import;
    never import it into the multiplexed gateway. Only its pure normalization
    runs here, and its network adapter is explicitly denied.
    """
    import os
    import subprocess
    import sys

    state = safe_path(home / 'state', directory=True, private=True)
    paths = {'script': state / 'fetch-linear-reconcile-snapshot.py',
             'routes': state / 'linear-reconcile-routes.json', 'manifest': state / 'fleet-projects.json'}
    packet = {'home': str(home), 'state': str(state), 'core': policy['installation']['core'],
              'slug': slug, 'board': board, 'issue': issue}
    for field, path in paths.items():
        raw = read_regular(safe_path(path), 1024 * 1024)
        packet[field], packet[field + '_sha256'] = str(path), digest(raw)
    data = canonical(packet)
    require(len(data) <= 256 * 1024, 'held_snapshot_invalid')
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
           'HERMES_HOME': str(home), 'FLEET_HERMES_HOME': str(home),
           'FLEET_HERMES_AGENT': policy['installation']['core'], 'PYTHONDONTWRITEBYTECODE': '1',
           'HOME': str(home.parent.parent.parent)}
    result = subprocess.run([sys.executable, '-I', '-c', _NORMALIZE], input=data, env=env,
                            cwd=state, capture_output=True, timeout=30)
    require(result.returncode == 0 and not result.stderr and len(result.stdout) <= 96 * 1024,
            'held_source_normalization_failed')
    for field, path in paths.items():
        require(digest(read_regular(path, 1024 * 1024)) == packet[field + '_sha256'],
                'identity_drift')
    return strict_json(result.stdout)


def private_board_database(root, board):
    """Validate native layout beneath an owner-private root, not private ancestors.

    Native kanban/boards directories can be group-writable. Other principals
    cannot reach them through the required owner-only Hermes root. Keep the
    database owner-only and reject symlinks or native overrides outside root.
    """
    import os
    from hermes_cli import kanban_db as kb

    root = safe_path(root, directory=True, private=True)
    database = safe_path(kb.kanban_db_path(board=board))
    require(database.is_relative_to(root) and database == database.resolve(), "identity_drift")
    info = database.lstat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "identity_drift")
    return database


def project_command(args):
    import sqlite3
    from contextlib import closing
    from dotenv import dotenv_values
    from slack_sdk import WebClient
    from hermes_constants import get_hermes_home, get_default_hermes_root
    from hermes_cli import kanban_db as kb
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from plugins.fleet_lifecycle.intake import project_policy

    home = get_hermes_home()
    policy = strict_json(read_regular(safe_path(home / 'fleet-lifecycle-policy.json', private=True), 128 * 1024))
    require(policy['installation']['profiles']['director']['home'] == str(home)
            and home == get_default_hermes_root(home=home) / 'profiles' / 'director', 'identity_drift')
    bp = strict_json(read_regular(safe_path(home / 'fleet-brief-policy.json', private=True), 64 * 1024))
    project = brief._project(bp, args.project)
    matches = [p for p in policy['projects'] if p['slug'] == args.project]
    require(len(matches) == 1, 'identity_drift')
    board = matches[0]['board']
    selected = project_policy(policy, args.project, board=board)
    root = get_default_hermes_root(home=home)
    registry = safe_path(home / 'projects.db')
    with closing(sqlite3.connect(registry.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        rows = conn.execute('SELECT id,primary_path FROM projects WHERE slug=? AND archived=0',
                            (args.project,)).fetchall()
    require(len(rows) == 1 and rows[0][1] == selected['repository'], 'identity_drift')
    database = private_board_database(root, board)
    client = WebClient(token=dotenv_values(home / '.env').get('SLACK_BOT_TOKEN'), timeout=30)
    require(client.auth_test()['team_id'] == bp['team'], 'brief_workspace_invalid')
    require(policy['installation']['slack']['team'] == bp['team']
            and policy['approval']['actor'] == bp['owner'], 'identity_drift')
    def read_message(channel, ts):
        messages = client.conversations_history(channel=channel, oldest=ts, latest=ts,
                                                 inclusive=True, limit=1).get('messages', [])
        require(len(messages) == 1, 'brief_source_invalid')
        return messages[0]
    try:
        discover_mcp_tools(allowed_mcp_names=['linear'])
        with closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            result = project_captured_brief(bp, policy, args.project, args.message_ts, read_message,
                                           brief._linear_call,
                                           lambda issue: normalize_installed_source(home, policy, args.project, board, issue),
                                           rows[0][0], board=board, conn=conn)
        print(canonical(result).decode())
        return 0
    finally:
        shutdown_mcp_servers()
