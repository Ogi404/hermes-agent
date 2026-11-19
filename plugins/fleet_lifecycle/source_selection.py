"""Read-only, allowlisted GitHub source selection for newly staged tasks only."""
from pathlib import Path
import re
import shlex
from urllib.parse import quote

from plugins.fleet_lifecycle.controller import Halt, require, strict_json
from plugins.fleet_lifecycle.runtime import Git, run


def reader_environment(runtime):
    # GitHub credentials stay in the existing native CLI store; never return a
    # token to the planner, source worker, model context or evidence packet.
    return {**runtime.env, 'HOME': str(Path.home()), 'GH_PROMPT_DISABLED': '1',
            'GH_NO_UPDATE_NOTIFIER': '1'}


def reader_binary():
    from hermes_platform.resolver import locate_command
    found = locate_command('gh')
    require(found is not None and found.found and len(found.command) == 1,
            'source_reader_unavailable')
    path = Path(found.command[0])
    require(path.is_absolute() and path.is_file(), 'source_reader_unavailable')
    return str(path)


def current_head(project, source, runtime):
    """Read a fixed GET endpoint, never a caller-provided URL or command."""
    repository = project['source_repository']
    require(source.call('remote', 'get-url', 'origin').decode().strip()
            == 'https://github.com/' + repository + '.git', 'source_repository_mismatch')
    try:
        result = run([reader_binary(), 'api', '--method', 'GET',
                      'repos/' + repository + '/git/ref/heads/' + quote(project['source_branch'], safe='')],
                     cwd='/', env=reader_environment(runtime), seconds=30, limit=16 * 1024)
    except OSError:
        raise Halt('source_reader_unavailable') from None
    require(result.returncode == 0, 'source_metadata_unavailable')
    try:
        data = strict_json(result.stdout)
    except Halt:
        raise Halt('source_metadata_invalid') from None
    require(isinstance(data, dict) and data.get('ref') == 'refs/heads/' + project['source_branch']
            and isinstance(data.get('object'), dict) and data['object'].get('type') == 'commit',
            'source_metadata_invalid')
    head = data['object'].get('sha')
    require(isinstance(head, str) and re.fullmatch('[0-9a-f]{40}', head) is not None,
            'source_metadata_invalid')
    return head


def source_checkout(source, branch, head):
    """Guard tracked source, not the preserved child-worktree namespace."""
    source.guard(branch, head)
    source.implementation_index(head)
    require(not source.call('-c', 'core.filemode=true', '-c', 'core.ignorestat=false',
                            'status', '--porcelain=v1', '--untracked-files=no', '--ignored=no'),
            'source_checkout_dirty')


def select_source(project, source, runtime):
    """Fetch only an immutable verified object; preserve every checkout/ref."""
    local_head = source.call('rev-parse', 'HEAD').decode().strip()
    source_checkout(source, project['source_branch'], local_head)
    config = source.call('config', '--local', '--null', '--list')
    require(not any(item.split(b'\n', 1)[0].lower().startswith(
        (b'url.', b'http.', b'credential.', b'protocol.')) for item in config.split(b'\0') if item),
        'source_configuration_unsupported')
    head = current_head(project, source, runtime)
    if source.call('cat-file', '-t', head, ok=False).strip() != b'commit':
        native = Git(source.executable, source.workspace, reader_environment(runtime))
        helper = '!' + shlex.quote(reader_binary()) + ' auth git-credential'
        try:
            native.call('-c', 'protocol.https.allow=always', '-c', 'credential.helper=',
                        '-c', 'credential.helper=' + helper, 'fetch', '--no-tags',
                        '--no-write-fetch-head', '--no-recurse-submodules',
                        'https://github.com/' + project['source_repository'] + '.git', head)
        except Halt:
            raise Halt('source_objects_unavailable') from None
    require(source.call('cat-file', '-t', head).strip() == b'commit', 'source_objects_unavailable')
    require(source.call('merge-base', local_head, head).decode().strip() == local_head,
            'source_checkout_not_ancestor')
    source_checkout(source, project['source_branch'], local_head)
    return head, local_head
