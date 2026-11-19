"""Authenticated existing capture -> reusable held native projection consumer."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_contract_guard import _extract_contract, _contract_hash
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home
from tests.plugins.test_fleet_task_intake import source
from tests.plugins.test_fleet_held_brief import inputs
from plugins.fleet_lifecycle.brief import capture_brief
from plugins.fleet_lifecycle.controller import Halt, command, setup_cli


def consumer():
    assert importlib.util.find_spec('plugins.fleet_lifecycle.held_brief') is not None,'missing source-facing captured-brief projection caller'
    from plugins.fleet_lifecycle.held_brief import project_captured_brief
    return project_captured_brief


@pytest.fixture
def captured_source(source,inputs):
    policy,row,_proposal,_gate=source
    bp,message,calls,read_message,linear,created=inputs
    bp['projects'][0]['slug']='hermes-agent'
    receipt=capture_brief(bp,'hermes-agent',message['ts'],read_message,linear)
    base,error=_extract_contract(row['body']);assert error is None
    def normalize(raw):
        assert raw['id']==receipt['issue_id'] and raw['uuid']==receipt['linear_id']
        c=copy.deepcopy(base)
        c.update(issue_id=raw['id'],linear_id=raw['uuid'],title=raw['title'],description=raw['description'],status=raw['status'],url=raw['url'],agent_ready=False,labels=[],fetched_at='2026-11-19T00:00:00Z')
        c['contract_sha256']=_contract_hash(c)
        return c
    def readonly(name,args):
        assert name=='get_issue','projection must not create or update Linear'
        return linear(name,args)
    return bp,policy,message,calls,read_message,readonly,normalize,row['project_id'],created


def invoke(data,conn):
    bp,policy,message,_calls,read_message,linear,normalize,pid,_created=data
    return consumer()(bp,policy,'hermes-agent',message['ts'],read_message,linear,normalize,pid,board='default',conn=conn)


def test_existing_authenticated_capture_projects_once_without_changing_source_or_capture(captured_source):
    bp,policy,message,calls,read_message,linear,normalize,pid,created=captured_source
    original={str(p):p.read_bytes() for p in Path(bp['state_root']).rglob('*') if p.is_file()}
    before_created=copy.deepcopy(created)
    with kbc.connect() as conn:
        result=invoke(captured_source,conn)
        assert result['stage']=='held_native_projected' and result['execution_authorized'] is False
        assert invoke(captured_source,conn)==result
        assert conn.execute('SELECT COUNT(*) FROM tasks WHERE idempotency_key=?',('linear:'+created['uuid'],)).fetchone()[0]==1
        assert not conn.execute('SELECT 1 FROM task_runs').fetchone()
    assert {str(p):p.read_bytes() for p in Path(bp['state_root']).rglob('*') if p.is_file()}==original
    assert created==before_created and [n for n,_ in calls].count('save_issue')==1


@pytest.mark.parametrize('case',['message_edit','actor','missing_receipt','wrong_receipt_id','source_scope','readiness','normalizer_scope'])
def test_rejected_captured_source_never_creates_native_task(captured_source,case):
    bp,policy,message,calls,read_message,linear,normalize,pid,created=captured_source
    if case=='message_edit':message['text']='changed scope'
    elif case=='actor':message['user']='UOTHER'
    elif case=='missing_receipt':next(Path(bp['state_root']).glob('*/receipt.json')).unlink()
    elif case=='wrong_receipt_id':
        p=next(Path(bp['state_root']).glob('*/receipt.json'));v=json.loads(p.read_text());v['issue_id']='ENS-999';p.write_text(json.dumps(v))
    elif case=='source_scope':created['description']='different source'
    elif case=='readiness':created['labels']=['agent-ready']
    else:
        def wrong(raw):
            c=normalize(raw);c['description']='normalizer changed scope';c['contract_sha256']=_contract_hash(c);return c
        data=list(captured_source);data[6]=wrong;captured_source=tuple(data)
    with kbc.connect() as conn:
        before=[tuple(r) for r in conn.execute('SELECT * FROM task_events ORDER BY id')]
        with pytest.raises(Halt):invoke(captured_source,conn)
        assert [tuple(r) for r in conn.execute('SELECT * FROM task_events ORDER BY id')]==before
    assert [n for n,_ in calls].count('save_issue')==1


def test_native_cli_owns_project_existing_capture_without_generic_write_fields(monkeypatch):
    parser=argparse.ArgumentParser();setup_cli(parser)
    try:args=parser.parse_args(['project-brief','--project','hermes-agent','--message-ts','1794656260.660269'])
    except SystemExit:pytest.fail('missing native project-brief executable consumer')
    consumer()
    from plugins.fleet_lifecycle import held_brief
    observed=[]
    monkeypatch.setattr(held_brief,'project_command',lambda a:observed.append((a.project,a.message_ts)) or 0)
    assert command(args)==0 and observed==[('hermes-agent','1794656260.660269')]
    assert not any(hasattr(args,k) for k in ['issue','body','policy','project_id','ready'])
