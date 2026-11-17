"""Pinned build-output to component-preview adapter; no import-time effects."""
from pathlib import Path
import os
import re
import sys

from plugins.fleet_lifecycle.controller import (
    Halt, canonical, digest, read_regular, require, safe_path, strict_json,
)


STATES = ('normal', 'hover', 'pressed', 'keyboard-focus')
SCHEMES = ('light', 'dark')


def preflight(runtime, spec):
    for path_key, hash_key in [('runner', 'runner_sha256'), ('script', 'script_sha256'),
                                ('baseline', 'baseline_sha256')]:
        path = safe_path(Path(spec[path_key]))
        require(digest(read_regular(path, 1024 * 1024)) == spec[hash_key], 'preview_identity_changed')
    baseline = strict_json(read_regular(Path(spec['baseline']), 1024 * 1024))
    base = runtime.git.base_blob(runtime.p['base'], 'src/app/page.tsx')
    require(isinstance(base, bytes) and baseline.get('sourceSha256') == digest(base),
            'preview_baseline_mismatch')


def consume(runtime, spec, evidence, build_stdout, run, *, seconds):
    try:
        return _consume(runtime, spec, evidence, build_stdout, run, seconds=seconds)
    except OSError:
        raise Halt('preview_artifact_unavailable') from None


def _consume(runtime, spec, evidence, build_stdout, run, *, seconds):
    matches = re.findall(rb'^FLEET EXEC RUN: ([0-9a-f]{12})$', build_stdout, re.MULTILINE)
    require(len(matches) == 1, 'preview_build_identity_invalid')
    run_dir = Path(spec['build_root']) / matches[0].decode('ascii')
    expected = ('RUN DIR PRESERVED: ' + str(run_dir)).encode()
    preserved = re.findall(rb'^RUN DIR PRESERVED: (.+)$', build_stdout, re.MULTILINE)
    require(len(preserved) == 1 and b'RUN DIR PRESERVED: ' + preserved[0] == expected,
            'preview_build_identity_invalid')
    workspace = safe_path(run_dir / 'workspace', directory=True)
    require(workspace.stat().st_uid == os.getuid(), 'preview_build_identity_invalid')
    candidate = read_regular(safe_path(Path(runtime.p['workspace']) / 'src/app/page.tsx'), 1024 * 1024)
    require(read_regular(safe_path(workspace / 'src/app/page.tsx'), 1024 * 1024) == candidate,
            'preview_source_mismatch')
    spec_path = evidence / 'preview-spec.json'
    spec_path.write_bytes(canonical(spec))
    output = evidence / 'preview'
    require(seconds > 40, 'preview_time_budget_exhausted')
    preflight(runtime, spec)
    result = run([sys.executable, '-I', spec['runner'], '--workspace', str(workspace), '--evidence', str(output),
                  '--spec', str(spec_path), '--docker', runtime.docker,
                  '--seconds', str(min(240, seconds - 40))],
                 cwd='/', env=runtime.env, seconds=seconds)
    (evidence / 'preview-stdout.log').write_bytes(result.stdout)
    (evidence / 'preview-stderr.log').write_bytes(result.stderr)
    require(result.returncode == 0, 'preview_failed')
    receipt = strict_json(read_regular(safe_path(output / 'receipt.json'), 1024 * 1024))
    require(receipt.get('status') == 'COMPONENT_PREVIEW_VERIFIED'
            and receipt.get('kind') == spec['kind'] and receipt.get('network') == 'none'
            and receipt.get('baseline_sha256') == spec['baseline_sha256']
            and receipt.get('source_sha256') == digest(candidate), 'preview_receipt_invalid')
    screenshots = receipt.get('screenshots')
    require(isinstance(screenshots, list) and len(screenshots) == 8, 'preview_receipt_invalid')
    expected_pairs = {(scheme, state) for scheme in SCHEMES for state in STATES}
    observed = set()
    for shot in screenshots:
        require(isinstance(shot, dict) and set(shot) == {'scheme', 'state', 'file', 'sha256', 'bytes'},
                'preview_receipt_invalid')
        pair = (shot['scheme'], shot['state'])
        require(pair in expected_pairs and pair not in observed, 'preview_receipt_invalid')
        observed.add(pair)
        require(shot['file'] == '-'.join(pair) + '.png', 'preview_receipt_invalid')
        raw = read_regular(safe_path(output / shot['file']), 2 * 1024 * 1024)
        require(raw.startswith(b'\x89PNG\r\n\x1a\n') and len(raw) == shot['bytes']
                and digest(raw) == shot['sha256'], 'preview_image_invalid')
    require(observed == expected_pairs, 'preview_receipt_invalid')
    preflight(runtime, spec)
    return {**receipt, 'evidence_directory': str(output), 'receipt_sha256': digest(canonical(receipt))}
