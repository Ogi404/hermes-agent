#!/usr/bin/env python3
"""Isolated Next home-navigation capture and bounded visual acceptance.

This is component evidence, not authenticated full-application acceptance.
"""
from pathlib import Path
import argparse
import base64
import sys
import hashlib
import json
import math
import os
import signal
import stat
import subprocess
import uuid

STATES = ('normal', 'hover', 'pressed', 'keyboard-focus')
SCHEMES = ('light', 'dark')
BUTTON_FIELDS = ('text', 'href', 'background', 'color', 'outline', 'boxShadow',
                 'focused', 'focusVisible', 'rect')


class PreviewError(Exception):
    pass


def require(value, reason):
    if not value:
        raise PreviewError(reason)


def regular(path, limit):
    path = Path(path)
    require(path.is_absolute(), 'preview_path_invalid')
    # Use the existing controller reader: final open is atomic O_NOFOLLOW,
    # and metadata is checked on that same descriptor, not a separate stat.
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    from plugins.fleet_lifecycle.controller import Halt, read_regular
    try:
        return read_regular(path, limit, owner_uid=os.getuid(), forbidden_mode=0o002)
    except (Halt, OSError):
        raise PreviewError('preview_file_invalid') from None


def rgb(value):
    require(isinstance(value, list) and len(value) == 3
            and all(type(x) is int and 0 <= x <= 255 for x in value), 'preview_rgb_invalid')
    return value


def background_expectations(value):
    """Validate task data without changing the legacy single-colour contract."""
    if isinstance(value, list):
        rgb(value)
        return {scheme: {state: value for state in ('normal', 'keyboard-focus')}
                for scheme in SCHEMES}
    require(isinstance(value, dict) and set(value) == set(SCHEMES), 'preview_rgb_invalid')
    for scheme in SCHEMES:
        require(isinstance(value[scheme], dict) and set(value[scheme]) == set(STATES),
                'preview_rgb_invalid')
        for state in STATES:
            rgb(value[scheme][state])
    return value


def luminance(value):
    values = [x / 255 for x in rgb(value)]
    linear = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in values]
    return sum(x * weight for x, weight in zip(linear, [0.2126, 0.7152, 0.0722]))


def index_observations(data, *, require_desktop=False):
    require(isinstance(data, dict) and data.get('viewport') == {'width': 420, 'height': 520},
            'preview_viewport_changed')
    rows = data.get('observations')
    require(isinstance(rows, list) and len(rows) == 8, 'preview_states_incomplete')
    indexed = {}
    for row in rows:
        if require_desktop:
            capabilities = row.get('capabilities')
            require(isinstance(capabilities, dict) and capabilities.get('hover') is True
                    and capabilities.get('pointerFine') is True, 'preview_desktop_pointer_not_proven')
        pair = (row.get('scheme'), row.get('state'))
        require(pair[0] in SCHEMES and pair[1] in STATES and pair not in indexed,
                'preview_states_incomplete')
        require(row.get('file') == '-'.join(pair) + '.png', 'preview_screenshot_name_invalid')
        buttons = row.get('buttons')
        require(isinstance(buttons, list) and 1 <= len(buttons) <= 20, 'preview_buttons_invalid')
        by_href = {}
        for button in buttons:
            require(isinstance(button, dict) and all(k in button for k in BUTTON_FIELDS),
                    'preview_buttons_invalid')
            href = button['href']
            require(isinstance(href, str) and href not in by_href, 'preview_buttons_invalid')
            rect = button['rect']
            require(isinstance(rect, dict) and set(rect) == {'x', 'y', 'width', 'height'}
                    and all(type(x) in (int, float) and math.isfinite(x) for x in rect.values())
                    and rect['width'] > 0 and rect['height'] > 0, 'preview_layout_invalid')
            by_href[href] = button
        require('/library' in by_href, 'preview_target_missing')
        indexed[pair] = by_href
    return indexed


def validate_observations(baseline, candidate, expected_rgb):
    before, after = index_observations(baseline), index_observations(candidate, require_desktop=True)
    expected = background_expectations(expected_rgb)
    contrasts = []
    for pair, old in before.items():
        new = after[pair]
        require(set(old) == set(new), 'preview_navigation_changed')
        for href, button in old.items():
            current = new[href]
            if href != '/library':
                require({k: button[k] for k in BUTTON_FIELDS} == {k: current[k] for k in BUTTON_FIELDS},
                        'preview_neighbor_changed')
                continue
            require(current['text'] == button['text'] == 'Library' and current['rect'] == button['rect'],
                    'preview_target_label_or_layout_changed')
            background, foreground = rgb(current.get('backgroundRGB')), rgb(current.get('textRGB'))
            if pair[1] in expected[pair[0]]:
                require(background == expected[pair[0]][pair[1]], 'preview_target_color_mismatch')
            a, b = luminance(background), luminance(foreground)
            ratio = (max(a, b) + 0.05) / (min(a, b) + 0.05)
            require(ratio >= 4.5, 'preview_label_unreadable')
            contrasts.append(ratio)
    for scheme in SCHEMES:
        normal, hover, pressed, focus = [after[(scheme, state)]['/library'] for state in STATES]
        if isinstance(expected_rgb, list):
            # Legacy pink contracts required three different interaction colours.
            # Explicit task contracts bind each captured state instead, including
            # intentional equality when restoring the original theme behaviour.
            require(len({tuple(x['backgroundRGB']) for x in [normal, hover, pressed]}) == 3,
                    'preview_interaction_states_indistinct')
        require(focus['focused'] is True and focus['focusVisible'] is True
                and (focus['outline'] != normal['outline'] or focus['boxShadow'] != normal['boxShadow']),
                'preview_keyboard_focus_missing')
    return {'states_checked': 8, 'neighbor_layout_unchanged': True, 'desktop_hover_proven': True,
            'minimum_label_contrast': min(contrasts), 'navigation_href_preserved': '/library',
            'scope': 'isolated home navigation component; not full application/backend acceptance'}


def browser_command(docker, workspace, output, script, image, name, proof):
    return [docker, 'run', '--rm', '--pull=never', '--network=none', '--read-only',
            '--name=' + name, '--label=fleet.preview=' + proof,
            '--user=' + str(os.getuid()) + ':' + str(os.getgid()), '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--cpus=1', '--memory=5120m',
            '--memory-swap=5120m', '--pids-limit=256', '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m',
            '-e', 'HOME=/tmp', '--mount', 'type=bind,src=' + str(workspace) + ',dst=/workspace,readonly',
            '--tmpfs', '/out:rw,nosuid,nodev,size=32m,mode=1777',
            '--mount', 'type=bind,src=' + str(script) + ',dst=/preview/capture.mjs,readonly',
            image, 'node', '/preview/capture.mjs']


def owned_container(docker, name, proof):
    result = subprocess.run([docker, 'container', 'inspect', '--format={{json .Config.Labels}}', name],
                            capture_output=True, text=True, timeout=15)
    if result.returncode == 1:
        require(('No such container: ' + name) in result.stderr
                or ('No such object: ' + name) in result.stderr, 'preview_container_state_unknown')
        return False
    require(result.returncode == 0 and json.loads(result.stdout).get('fleet.preview') == proof,
            'preview_container_ownership_mismatch')
    return True


def bounded_capture(argv, output, seconds):
    # This is the trusted controller's existing bounded pipe/session primitive,
    # not a host import of application or candidate product code.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from plugins.fleet_lifecycle.runtime import run
    from plugins.fleet_lifecycle.controller import Halt
    empty_home = output / 'docker-client-home'
    empty_home.mkdir(mode=0o700)
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
           'HOME': str(empty_home), 'DOCKER_CONFIG': str(empty_home)}
    try:
        return run(argv, cwd='/', env=env, seconds=seconds, limit=4 * 1024 * 1024)
    except Halt as exc:
        raise PreviewError(exc.code) from None


def capture_browser(argv, docker, name, proof, output, seconds=240):
    (output / 'container-intent.json').write_text(json.dumps({'name': name, 'proof': proof}))
    try:
        try:
            result = bounded_capture(argv, output, seconds)
        except subprocess.TimeoutExpired:
            raise PreviewError('preview_browser_timeout') from None
        stdout = result.stdout.decode(errors='replace') if isinstance(result.stdout, bytes) else result.stdout
        stderr = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else result.stderr
        (output / 'browser-stdout.log').write_text(stdout)
        (output / 'browser-stderr.log').write_text(stderr)
        require(result.returncode == 0, 'preview_browser_failed')
        return stdout
    finally:
        if owned_container(docker, name, proof):
            result = subprocess.run([docker, 'container', 'rm', '--force', name],
                                    capture_output=True, text=True, timeout=15)
            require(result.returncode == 0, 'preview_cleanup_failed')
        require(not owned_container(docker, name, proof), 'preview_cleanup_failed')
        (output / 'container-cleanup.json').write_text(json.dumps({'name': name, 'owned_container_absent': True}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    for flag in ['workspace', 'evidence', 'spec', 'docker']:
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--seconds', type=int, default=240)
    args = parser.parse_args()
    require(1 <= args.seconds <= 240, 'preview_time_budget_invalid')
    require(os.getuid() != 0, 'root_execution_forbidden')
    spec = json.loads(regular(Path(args.spec), 64 * 1024))
    require(spec['kind'] == 'next_home_navigation', 'preview_kind_invalid')
    script, baseline_path = Path(spec['script']), Path(spec['baseline'])
    script_raw, baseline_raw = regular(script, 1024 * 1024), regular(baseline_path, 1024 * 1024)
    require(hashlib.sha256(script_raw).hexdigest() == spec['script_sha256']
            and hashlib.sha256(baseline_raw).hexdigest() == spec['baseline_sha256'], 'preview_inputs_changed')
    workspace, output = Path(args.workspace), Path(args.evidence)
    require(workspace.is_absolute() and workspace.resolve() == workspace and workspace.is_dir(), 'preview_workspace_invalid')
    output.mkdir(mode=0o700)
    require(output.resolve() == output, 'preview_output_invalid')
    name = 'fleet-component-preview-' + uuid.uuid4().hex
    proof = hashlib.sha256(regular(Path(args.spec), 64 * 1024)).hexdigest()
    raw_packet = capture_browser(browser_command(args.docker, workspace, output, script, spec['image'], name, proof),
                                 args.docker, name, proof, output, seconds=args.seconds)
    packet = json.loads(raw_packet)
    require(isinstance(packet, dict) and set(packet) == {'result', 'files'}, 'preview_packet_invalid')
    expected_names = {scheme + '-' + state + '.png' for scheme in SCHEMES for state in STATES}
    require(isinstance(packet['files'], dict) and set(packet['files']) == expected_names, 'preview_packet_invalid')
    candidate = packet['result']
    for filename, encoded in packet['files'].items():
        raw = base64.b64decode(encoded, validate=True)
        require(0 < len(raw) <= 2 * 1024 * 1024 and raw.startswith(b'\x89PNG\r\n\x1a\n'), 'preview_image_invalid')
        with (output / filename).open('xb') as stream:
            stream.write(raw)
    with (output / 'preview-result.json').open('x') as stream:
        json.dump(candidate, stream)

    source = regular(workspace / 'src/app/page.tsx', 1024 * 1024)
    require(candidate['sourceSha256'] == hashlib.sha256(source).hexdigest(), 'preview_source_mismatch')
    checks = validate_observations(json.loads(baseline_raw), candidate, spec['expected_background_rgb'])
    shots = []
    for scheme in SCHEMES:
        for state in STATES:
            filename = scheme + '-' + state + '.png'
            raw = regular(output / filename, 2 * 1024 * 1024)
            require(raw.startswith(b'\x89PNG\r\n\x1a\n'), 'preview_image_invalid')
            shots.append({'scheme': scheme, 'state': state, 'file': filename,
                          'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
    receipt = {'status': 'COMPONENT_PREVIEW_VERIFIED', 'kind': spec['kind'], 'network': 'none',
               'source_sha256': candidate['sourceSha256'], 'baseline_sha256': spec['baseline_sha256'],
               'screenshots': shots, 'checks': checks, 'owned_browser_container_removed': True}
    with (output / 'receipt.json').open('x') as stream:
        json.dump(receipt, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({'status': receipt['status'], 'screenshots': len(shots)}))
    return 0


if __name__ == '__main__':
    def interrupted(_signum, _frame):
        raise PreviewError('preview_interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        raise SystemExit(main())
    except (PreviewError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'COMPONENT_PREVIEW_NOT_VERIFIED', 'error_type': type(exc).__name__,
                          'reason': str(exc)[:180]}))
        raise SystemExit(1)
