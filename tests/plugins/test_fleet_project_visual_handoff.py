"""Automatic project-build to pinned component-preview handoff, no live services."""
import hashlib
import json
from pathlib import Path

import pytest

from plugins.fleet_lifecycle import runtime as mod
from plugins.fleet_lifecycle.controller import Halt, Plan
from tests.plugins.test_fleet_project_verification import _runtime, WRAPPER_PASS_OUTPUT


def preview(rt, data, tmp_path):
    runner = tmp_path / 'preview.py'
    runner.write_bytes(b'# pinned preview fixture\n')
    script = tmp_path / 'navigation.mjs'
    script.write_bytes(b'// pinned capture fixture\n')
    baseline = tmp_path / 'baseline.json'
    baseline.write_text(json.dumps({'sourceSha256': hashlib.sha256(b'baseline fixture').hexdigest()}))
    data.update(candidate_paths=['src/app/page.tsx'], test_files=[],
                changes=[{'path': 'src/app/page.tsx', 'base_sha256': hashlib.sha256(b'baseline fixture').hexdigest()}])
    data['verification']['commands'] = ['npm run build']
    data['resources'].update(memory_mb=5120, pids=256, scratch_mb=1024)
    source = Path(data['workspace']) / 'src/app/page.tsx'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'candidate fixture')
    rt.git.status_paths = ['src/app/page.tsx']
    rt.git.base_blob = lambda base, path: b'baseline fixture'
    data['verification']['preview'] = {
        'kind': 'next_home_navigation', 'runner': str(runner),
        'runner_sha256': hashlib.sha256(runner.read_bytes()).hexdigest(),
        'script': str(script), 'script_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
        'image': 'sha256:' + 'e' * 64, 'build_root': str(tmp_path / 'build-runs'),
        'baseline': str(baseline), 'baseline_sha256': hashlib.sha256(baseline.read_bytes()).hexdigest(),
        'expected_background_rgb': [236, 72, 153],
    }
    return data['verification']['preview']


def test_frozen_project_recipe_accepts_optional_pinned_preview_without_normalizing(tmp_path, monkeypatch):
    rt, data, calls = _runtime(tmp_path, monkeypatch)
    preview(rt, data, tmp_path)
    old = json.loads(json.dumps(data))
    Plan.validate(data)
    assert data == old


@pytest.mark.parametrize('case', ['python_lane', 'missing_pin', 'tag_image', 'invalid_rgb', 'unknown_field', 'unbounded_budget'])
def test_preview_contract_rejects_unpinned_or_unsupported_execution(tmp_path, monkeypatch, case):
    rt, data, calls = _runtime(tmp_path, monkeypatch)
    spec = preview(rt, data, tmp_path)
    if case == 'python_lane': data['verification']['lane'] = 'python_testenv'
    elif case == 'missing_pin': del spec['script_sha256']
    elif case == 'tag_image': spec['image'] = 'chromium:latest'
    elif case == 'invalid_rgb': spec['expected_background_rgb'] = [True, 72, 153]
    elif case == 'unbounded_budget': data['resources']['scratch_mb'] = 64
    else: spec['arbitrary_command'] = 'run-anything'
    with pytest.raises(Halt): Plan.validate(data)


@pytest.mark.parametrize('case', ['pass', 'preview_fail', 'missing_image', 'corrupt_image', 'empty_images', 'runner_swap_during_build'])
def test_actual_project_verifier_keeps_build_then_consumes_preview_before_receipt(tmp_path, monkeypatch, case):
    rt, data, calls = _runtime(tmp_path, monkeypatch)
    spec = preview(rt, data, tmp_path)
    run_id = 'a1b2c3d4e5f6'
    workspace = Path(spec['build_root']) / run_id / 'workspace'
    workspace.mkdir(parents=True)
    (workspace / 'src/app').mkdir(parents=True)
    (workspace / 'src/app/page.tsx').write_bytes(b'candidate fixture')
    observed = []
    def fake_run(argv, **kwargs):
        observed.append(argv)
        if argv[0] == rt.docker:
            image = argv[-1]
            return mod.Result(0, (image + '\n').encode(), b'')
        if argv[0] == data['verification']['runner']:
            assert argv[-1] == '--keep-run-dir'
            if case == 'runner_swap_during_build': Path(spec['runner']).write_bytes(b'swapped during build\n')
            return mod.Result(0, WRAPPER_PASS_OUTPUT.replace(b'FLEET EXEC RUN: fixture',
                ('FLEET EXEC RUN: ' + run_id).encode()) + ('RUN DIR PRESERVED: ' + str(workspace.parent) + '\n').encode(), b'')
        assert spec['runner'] in argv
        assert argv[argv.index('--workspace') + 1] == str(workspace)
        target = Path(argv[argv.index('--evidence') + 1])
        target.mkdir()
        import base64
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6nB8AAAAASUVORK5CYII=')
        images = []
        for scheme in ['light', 'dark']:
            for state in ['normal', 'hover', 'pressed', 'keyboard-focus']:
                name = scheme + '-' + state + '.png'
                (target / name).write_bytes(png)
                images.append({'scheme': scheme, 'state': state, 'file': name,
                               'sha256': hashlib.sha256(png).hexdigest(), 'bytes': len(png)})
        receipt = {'status': 'COMPONENT_PREVIEW_VERIFIED', 'kind': 'next_home_navigation',
                   'network': 'none', 'screenshots': images, 'baseline_sha256': spec['baseline_sha256'],
                   'source_sha256': hashlib.sha256(b'candidate fixture').hexdigest()}
        if case == 'missing_image': (target / images[0]['file']).unlink()
        elif case == 'corrupt_image': (target / images[0]['file']).write_bytes(b'changed image')
        elif case == 'empty_images': receipt['screenshots'] = []
        (target / 'receipt.json').write_text(json.dumps(receipt))
        return mod.Result(1 if case == 'preview_fail' else 0, b'preview fixture\n', b'')
    monkeypatch.setattr(mod, 'run', fake_run)
    monkeypatch.setattr(mod.os, 'getuid', lambda: 1000)
    if case != 'pass':
        with pytest.raises(Halt):
            mod.ProjectExecVerifier(rt).verify({'snapshot': rt.git.snapshot, 'owned': []})
        assert not list(Path(data['state_root']).glob('action-*/receipt.json'))
        if case == 'runner_swap_during_build':
            assert any(a[0] == data['verification']['runner'] for a in observed)
            assert not any(spec['runner'] in a for a in observed), 'swapped preview runner must never execute'
            assert not list(Path(data['state_root']).glob('action-*/preview/receipt.json'))
        return
    result = mod.ProjectExecVerifier(rt).verify({'snapshot': rt.git.snapshot, 'owned': []})
    assert result[0]['visual']['status'] == 'COMPONENT_PREVIEW_VERIFIED'
    assert result[0]['visual']['baseline_sha256'] == spec['baseline_sha256']
    assert rt.git.capture_calls == 2
    import sys
    preview_calls = [a for a in observed if spec['runner'] in a]
    assert len(preview_calls) == 1
    assert preview_calls[0][:3] == [sys.executable, '-I', spec['runner']], 'production preview must use isolated selected Python'


def test_tampered_preview_is_rejected_before_build_or_container_process(tmp_path, monkeypatch):
    rt, data, calls = _runtime(tmp_path, monkeypatch)
    spec = preview(rt, data, tmp_path)
    Path(spec['runner']).write_bytes(b'tampered\n')
    monkeypatch.setattr(mod, 'run', lambda *a, **k: calls.append(a))
    monkeypatch.setattr(mod.os, 'getuid', lambda: 1000)
    with pytest.raises(Halt):
        mod.ProjectExecVerifier(rt).verify({'snapshot': rt.git.snapshot, 'owned': []})
    assert calls == []


def test_visual_evidence_is_carried_into_actual_native_review_envelope(tmp_path, monkeypatch):
    rt, data, _ = _runtime(tmp_path, monkeypatch)
    data['context'] = [{'path': 'src/app/page.tsx', 'functions': []}]
    def base_context(*args):
        assert args == ('show', data['base'] + ':src/app/page.tsx')
        return b'immutable baseline context'
    rt.git.call = base_context
    visual = {'status': 'COMPONENT_PREVIEW_VERIFIED', 'kind': 'next_home_navigation',
              'checks': {'states_checked': 8, 'desktop_hover_proven': True},
              'screenshots': [{'file': 'light-normal.png', 'sha256': 'f' * 64}]}
    receipts = [{'lane': 'fleet_project_exec', 'manifest_hash': rt.git.snapshot['manifest_hash'], 'visual': visual}]
    envelope = rt.context({'snapshot': rt.git.snapshot, 'verification': receipts}, 'precommit_review')
    assert envelope['verification'] == receipts
    assert envelope['verification'][0]['visual'] == visual
    assert envelope['binding']['manifest_hash'] == rt.git.snapshot['manifest_hash']
    assert envelope['context'] == [{'path': 'src/app/page.tsx', 'base_context': 'immutable baseline context'}]
