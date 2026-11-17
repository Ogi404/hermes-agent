"""Component-preview acceptance and browser containment; fixture images are not live proof."""
import copy
import importlib.util

import pytest


def module():
    assert importlib.util.find_spec('plugins.fleet_lifecycle.navigation_preview') is not None, 'missing component-preview runner'
    from plugins.fleet_lifecycle import navigation_preview
    return navigation_preview


def observations():
    rows = []
    for scheme in ['light', 'dark']:
        for state in ['normal', 'hover', 'pressed', 'keyboard-focus']:
            target = {'text': 'Library', 'href': '/library', 'background': 'rgb(128, 128, 128)',
                      'color': 'rgb(0, 0, 0)', 'outline': 'none', 'boxShadow': 'none',
                      'focused': state == 'keyboard-focus', 'focusVisible': state == 'keyboard-focus',
                      'rect': {'x': 10, 'y': 10, 'width': 100, 'height': 44}}
            neighbor = {**target, 'text': 'Calendar', 'href': '/calendar', 'focused': False, 'focusVisible': False,
                        'rect': {'x': 120, 'y': 10, 'width': 100, 'height': 44}}
            rows.append({'scheme': scheme, 'state': state, 'file': scheme + '-' + state + '.png',
                         'buttons': [target, neighbor]})
    baseline = {'viewport': {'width': 420, 'height': 520}, 'observations': rows}
    candidate = copy.deepcopy(baseline)
    for row in candidate['observations']:
        row['capabilities'] = {'hover': True, 'pointerFine': True}
        target = row['buttons'][0]
        value = {'normal': [236, 72, 153], 'hover': [245, 100, 170],
                 'pressed': [250, 130, 185], 'keyboard-focus': [236, 72, 153]}[row['state']]
        target['backgroundRGB'] = value
        target['textRGB'] = [0, 0, 0]
        if row['state'] == 'keyboard-focus': target['outline'] = 'rgb(0, 0, 0) solid 2px'
    return baseline, candidate


def test_changed_component_checks_color_readability_neighbors_layout_and_states():
    baseline, candidate = observations()
    result = module().validate_observations(baseline, candidate, [236, 72, 153])
    assert result['states_checked'] == 8 and result['neighbor_layout_unchanged'] is True
    assert result['minimum_label_contrast'] >= 4.5


@pytest.mark.parametrize('case', ['grey', 'label', 'route', 'neighbor', 'layout', 'focus', 'hover',
                                  'unreadable', 'missing_state', 'duplicate_state', 'wrong_viewport', 'hover_capability', 'missing_capability'])
def test_visual_acceptance_rejects_missing_or_changed_required_evidence(case):
    baseline, candidate = observations()
    row = candidate['observations'][0]
    target = row['buttons'][0]
    if case == 'grey': target['backgroundRGB'] = [128, 128, 128]
    elif case == 'label': target['text'] = 'Different'
    elif case == 'route': target['href'] = '/different'
    elif case == 'neighbor': row['buttons'][1]['background'] = 'rgb(255, 0, 0)'
    elif case == 'layout': target['rect']['width'] += 1
    elif case == 'focus': candidate['observations'][3]['buttons'][0]['focusVisible'] = False
    elif case == 'hover': candidate['observations'][1]['buttons'][0]['backgroundRGB'] = [236, 72, 153]
    elif case == 'unreadable': target['textRGB'] = [236, 72, 153]
    elif case == 'missing_state': candidate['observations'].pop()
    elif case == 'duplicate_state': candidate['observations'][-1] = copy.deepcopy(candidate['observations'][0])
    elif case == 'hover_capability': row['capabilities']['hover'] = False
    elif case == 'missing_capability': del row['capabilities']
    else: candidate['viewport']['width'] = 800
    with pytest.raises(module().PreviewError):
        module().validate_observations(baseline, candidate, [236, 72, 153])


def test_browser_command_has_pinned_image_no_network_no_credentials_and_readonly_source(tmp_path):
    mod = module()
    argv = mod.browser_command('/fixture/docker', tmp_path / 'workspace', tmp_path / 'out',
                               tmp_path / 'capture.mjs', 'sha256:' + 'e' * 64, 'owned-name', 'own-proof')
    assert argv[:3] == ['/fixture/docker', 'run', '--rm']
    assert '--pull=never' in argv and '--network=none' in argv and '--read-only' in argv
    assert '--cap-drop=ALL' in argv and '--security-opt=no-new-privileges' in argv
    assert '--memory-swap=5120m' in argv
    assert '--label=fleet.preview=own-proof' in argv and '--name=owned-name' in argv
    mounts = [argv[i + 1] for i, x in enumerate(argv) if x == '--mount']
    assert any('dst=/workspace,readonly' in x for x in mounts)
    assert any('dst=/preview/capture.mjs,readonly' in x for x in mounts)
    assert not any('.hermes' in x or '.env' in x or 'docker.sock' in x for x in mounts)
    assert not any('dst=/out' in x for x in mounts)
    assert '/out:rw,nosuid,nodev,size=32m,mode=1777' in argv
    assert argv[-3:] == ['sha256:' + 'e' * 64, 'node', '/preview/capture.mjs']


@pytest.mark.parametrize('case', ['pass', 'timeout', 'nonzero', 'foreign_label', 'cleanup_failure'])
def test_browser_capture_owns_cleanup_and_never_accepts_timeout_or_wrong_ownership(tmp_path, monkeypatch, case):
    mod = module()
    import json
    import subprocess
    from types import SimpleNamespace
    calls = []
    alive = [False]
    def execute(argv, **kwargs):
        calls.append(argv)
        if argv[1] == 'run':
            alive[0] = True
            if case == 'timeout': raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
            return SimpleNamespace(returncode=1 if case == 'nonzero' else 0, stdout='capture log', stderr='')
        if argv[1:3] == ['container', 'inspect']:
            if not alive[0]: return SimpleNamespace(returncode=1, stdout='', stderr='Error: No such container: owned-name')
            return SimpleNamespace(returncode=0, stdout=json.dumps({'fleet.preview': 'foreign' if case == 'foreign_label' else 'own-proof'}), stderr='')
        assert argv[1:3] == ['container', 'rm'] and argv[-1] == 'owned-name'
        if case != 'cleanup_failure': alive[0] = False
        return SimpleNamespace(returncode=1 if case == 'cleanup_failure' else 0, stdout='', stderr='')
    monkeypatch.setattr(mod.subprocess, 'run', execute)
    monkeypatch.setattr(mod, 'bounded_capture', lambda argv, output, seconds: execute(argv, timeout=seconds))
    argv = mod.browser_command('/fixture/docker', tmp_path / 'workspace', tmp_path,
                               tmp_path / 'capture.mjs', 'sha256:' + 'e' * 64, 'owned-name', 'own-proof')
    if case == 'pass': mod.capture_browser(argv, '/fixture/docker', 'owned-name', 'own-proof', tmp_path)
    else:
        with pytest.raises(mod.PreviewError):
            mod.capture_browser(argv, '/fixture/docker', 'owned-name', 'own-proof', tmp_path)
    if case == 'foreign_label': assert not any(x[1:3] == ['container', 'rm'] for x in calls)
    elif case != 'cleanup_failure': assert alive == [False]
    assert not (tmp_path / 'receipt.json').exists()



def test_pinned_reader_rejects_final_symlink_swapped_at_actual_open(tmp_path, monkeypatch):
    import io
    import os
    from pathlib import Path
    mod = module()
    target, other = tmp_path / 'baseline.json', tmp_path / 'untrusted.json'
    target.write_bytes(b'original pinned bytes')
    other.write_bytes(b'untrusted swapped bytes')
    opened, swapped = [], []
    actual_io_open, actual_os_open = io.open, os.open
    def swap(file, origin):
        if isinstance(file, (str, Path)) and Path(file) == target and not swapped:
            target.rename(tmp_path / 'preserved-original.json')
            target.symlink_to(other)
            swapped.append(origin)
    def io_boundary(file, *args, **kwargs):
        swap(file, 'io.open')
        return actual_io_open(file, *args, **kwargs)
    def os_boundary(file, flags, *args, **kwargs):
        swap(file, 'os.open')
        if Path(file) == target: opened.append(flags)
        return actual_os_open(file, flags, *args, **kwargs)
    monkeypatch.setattr(io, 'open', io_boundary)
    monkeypatch.setattr(os, 'open', os_boundary)
    with pytest.raises(mod.PreviewError): mod.regular(target, 1024)
    assert swapped == ['os.open']
    assert opened and all(flags & os.O_NOFOLLOW for flags in opened)
    assert actual_io_open(tmp_path / 'preserved-original.json', 'rb').read() == b'original pinned bytes'
    assert actual_io_open(other, 'rb').read() == b'untrusted swapped bytes'


def test_pinned_reader_preserves_normal_reads_size_and_metadata_guards(tmp_path):
    mod = module()
    target = tmp_path / 'pinned.json';target.write_bytes(b'original')
    assert mod.regular(target, 8) == b'original'
    with pytest.raises(mod.PreviewError): mod.regular(target, 7)
    target.chmod(0o666)
    with pytest.raises(mod.PreviewError): mod.regular(target, 8)
    assert target.read_bytes() == b'original'
