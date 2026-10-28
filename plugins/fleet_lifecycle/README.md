# Trusted fleet lifecycle controller (opt-in source, not activated)

Status and scope
----------------
This is a bounded operator CLI, not an inference tool, dispatcher, scheduler,
product implementer, admission bypass, or publication agent. Import/registration
has no lifecycle side effects. No commit, deployment, activation, live admission,
Slack transmission, ENS9 retry, or native review was performed by this source pass.
Unit fixtures are NOT native Reviewer approval or live pipeline evidence.

The MVP supports exactly ONE harmless NEW focused Python test (mode 100644).
The candidate list equals the test-file list. No existing tests, conftest, runner,
controller, or production source may be changed. Production templates are rejected.
The mutation targets a separate immutable production BASE blob, never the new test.
A generic prose contract cannot prove this scope: the validated schema-2 frozen
contract must have execution_mode=implement, task_lane=implementation and description
EXACTLY one of two plan-generated wire representations. Let J be the identical
canonical JSON of the same four frozen plan fields: base, candidate_paths,
test_files and mutation. The representations are these exact concatenations:

    legacy (default): "fleet-lifecycle-test-only-v1 " + J
    fenced (markdown=True): "fleet-lifecycle-test-only-v1\n\n```json\n" + J + "\n```"

Quotes and + above describe concatenation, not wire bytes; each \n denotes one LF.
The legacy prefix is followed by exactly one ASCII space. The fenced prefix is
followed by exactly two LFs, literal ```json, one LF, J, one LF and literal ```.
Neither representation has a trailing newline. A schematic legacy example is:

    fleet-lifecycle-test-only-v1 {"base":...,"candidate_paths":...,"mutation":...,"test_files":...}

Canonical means json.dumps(value, sort_keys=True, separators=(",", ":"),
ensure_ascii=True, allow_nan=False). Only exact full-description membership is
accepted: no alternate fences, language labels, whitespace, prose, noncanonical
JSON or repaired/unescaped exports. The native fleet guard still freezes and
hash-validates the actual description; the plan binds that contract_sha256
unchanged. The two wire forms do not share a contract hash. Do not edit an existing
frozen task to retrofit permission. Unsupported/narrower prose-only contracts fail.
The fenced form is a supported strict protocol representation, not proof that an
actual Linear UI roundtrip has passed. Source correction/capture, eligibility,
admission and activation still require separate authority. This explicit protocol
requires a separately reviewed producer/operator contract, not a claim that
arbitrary existing fleet cards are compatible.

In this checkout the native schema-2 snapshot has flat role-profile fields, not a
nested role_policy. Native fleet guard validates those fields and project registry.
Director must be an orchestration profile, Builder an implementation profile,
Reviewer a review profile; control_plane membership does not authorize Director.
Whole task body/title/provenance/project/workspace/branch/model-provider overrides
are independently frozen and rebound before/after staged actions. Project registry
identity, approved base and clean index/tree/modes are also rechecked.

Strict plan example
-------------------
This is a SCHEMA EXAMPLE, not an actual plan, receipt, or authorization. Zero hashes,
example IDs/paths and resolved executable destinations must be replaced from an
independently reviewed, aligned installation BEFORE requesting authorization. The
mutation hashes below describe only the illustrative bytes
"def guard():\n    raise ValueError('cycle')\n". They are not hashes of this product.
Every shown field is required; unknown/duplicate keys and nonfinite values fail.
No request_event_id is permitted in the plan. No field is excluded from its hash.

```json
{
  "schema": 1,
  "board": "default",
  "database": "/home/walle/.hermes/kanban.db",
  "task_id": "t_12345678",
  "project_id": "project-example",
  "workspace": "/srv/project/.worktrees/t_12345678",
  "branch": "hermes-agent/focused-cycle-test",
  "base": "0000000000000000000000000000000000000000",
  "contract_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "core": "/srv/fleet/controller-source",
  "source_sha": "0000000000000000000000000000000000000000",
  "candidate_paths": [
    "tests/test_cycle.py"
  ],
  "test_files": [
    "tests/test_cycle.py"
  ],
  "mutation": {
    "path": "production_guard.py",
    "base_sha256": "b85b323a8bdcf9d1cce65b0a592750e4e2053be08587444e2600ade793f2ff9f",
    "mutant_sha256": "4daaacf81d28ea0b5c3516b365a79ef3a13290657b692e3673ff17738f5d5237",
    "before": "raise ValueError('cycle')",
    "after": "pass # mutation",
    "before_sha256": "e8d5f53e799325f87d875f878b5bba74aca2141cd3e78c2d8e5dc684830a2bf7",
    "after_sha256": "29dcd013a4be40acf6b742bcdcce52f4aedf80556326350e524e02b9930e4407",
    "test_file": "tests/test_cycle.py",
    "test_name": "test_regression"
  },
  "resources": {
    "image": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "seconds": 300,
    "cpus": 1,
    "memory_mb": 1024,
    "pids": 128,
    "scratch_mb": 512
  },
  "profiles": {
    "director": {
      "name": "director",
      "home": "/home/walle/.hermes/profiles/director",
      "config_sha256": "0000000000000000000000000000000000000000000000000000000000000000"
    },
    "builder": {
      "name": "builder",
      "home": "/home/walle/.hermes/profiles/builder",
      "config_sha256": "0000000000000000000000000000000000000000000000000000000000000000"
    },
    "reviewer": {
      "name": "reviewer",
      "home": "/home/walle/.hermes/profiles/reviewer",
      "config_sha256": "0000000000000000000000000000000000000000000000000000000000000000"
    }
  },
  "native": {
    "launcher_entry": "/home/walle/.local/bin/hermes",
    "launcher": "/opt/approved/hermes-launcher",
    "launcher_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "runtime_command_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "client": "/home/walle/.hermes/profiles/reviewer/native-client.py",
    "client_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "binary_entry": "/home/walle/.nvm/versions/node/v24.20.0/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    "binary": "/opt/approved/claude-resolved",
    "binary_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "tools_root": "/home/walle/.hermes/tools",
    "model": "claude-sonnet-5",
    "provider": "fleet-claude-cli"
  },
  "state_root": "/home/walle/.hermes/profiles/director/fleet-lifecycle",
  "slack": {
    "profile": "default",
    "channel": "CEXAMPLE",
    "thread": "",
    "team": "TEXAMPLE"
  },
  "gate": {
    "task_id": "t_87654321",
    "stage": "merge",
    "source_sha": "0000000000000000000000000000000000000000",
    "actor": "UOWNER"
  },
  "context": [
    {
      "path": "production_guard.py",
      "functions": [
        "guard"
      ]
    }
  ],
  "commit": {
    "name": "Fleet Operator",
    "email": "operator@example.invalid",
    "message": "Add focused cycle regression"
  }
}
```

All paths are absolute normalized nonsymlink paths except explicitly lexical
native.binary_entry and native.launcher_entry: their strict resolution MUST equal
the separately pinned regular binary/launcher whose whole bytes are hashed. A
known approved lexical symlink is not silently rewritten; redirects are rejected.
The installed client's _COMMAND must exactly equal binary_entry. The facade is
constructed with the approved resolved regular binary because its own lstat rejects
symlink commands. Client, profiles/config, controller source SHA, launcher and
native binary are pinned. Model/provider stay claude-sonnet-5/fleet-claude-cli.
HOME remains the owning OS user's subscription/credential root (approved operator:
/home/walle); only HERMES_HOME becomes Reviewer's profile. No credentials are copied,
printed, mounted into tests, or passed in an API request. No APIs/fallback/accounts
or core-provider patches exist here.

Resources: seconds 1..900, cpus 1..4, memory_mb 256..8192, pids 1..256,
scratch_mb 64..4096; all integers, no booleans. Image is a local immutable Docker
image ID sha256:<64 lowercase hex>, not a mutable tag, and --pull=never forbids
acquisition. Context has 1..12 baseline entries with optional top-level AST symbol
names, each bounded to 48 KiB. Candidate bytes are limited to 1 MiB/file and 2 MiB
total. Review envelope is limited to 192 KiB before outer serialization, and the
pinned facade's actual serializer enforces its 1 MiB message cap before auth/IPC.
Native seconds must also fit the pinned facade's _MAX_SECONDS (900 here).

Noncircular operational approval
--------------------------------
1. Prepare the reviewed frozen task contract and strict plan, with all actual scope,
   profile/source/tool hashes, BASE hashes and whole mutation-result hash present.
2. Read-only: hermes -p director fleet-lifecycle hash --plan /absolute/plan.json
   This validates strict JSON/schema and prints plan_hash and exact approval_tests;
   it does not create a ledger, contact native inference, or create/decide a gate.
3. An independently authorized operator requests a native gate on the SEPARATE
   operational ledger card specified by gate.task_id, binding gate.source_sha to
   the controller source SHA and tests EXACTLY to the printed approval_tests:

       fleet-plan=<canonical hash> source=<controller SHA> task=<implementation task> contract=<frozen hash>

   The native producer supports push/ready/merge only; ready/merge additionally
   require a canonical GitHub PR URL. The example uses merge on the operational
   controller-source PR. Its stage is not the implementation's final PUSH gate.
   Native request reviewer_verdict=APPROVE must come from independent review;
   this controller never manufactures or decides it.
4. The authenticated authorized Slack actor must decide that latest native request
   approve at the exact SHA. CLI decisions, wrong actors, stale requests, other
   sources/plans/contracts, absent approvals and decisions for other requests fail.
   The actual returned request event ID is then durably reserved at admission.
   Do NOT edit the plan after approval to add a future request event ID.
5. Read-only prerequisite check:
       hermes -p director fleet-lifecycle check --plan /absolute/plan.json
   Then, ONLY under separate admission authorization:
       hermes -p director fleet-lifecycle admit --plan /absolute/plan.json
   Then, ONLY under separate controller/schedule activation authorization:
       hermes -p director fleet-lifecycle tick --plan /absolute/plan.json
   One tick advances at most one stage. These are documented future operator
   actions, not authorization to perform them in this pass.

Admission never unblocks, adopts old artifacts or runs a worker. It only installs
workflow/step identity on an already parked blocked/triage native card and emits
an admitted event. A fresh, owned Builder claim and request-review must follow
admission under independent release/dispatch authority. A claimed native run with
closed outcome=review_requested/status=review, current step waiting_source, and
exact Builder/Reviewer event provenance is required. Synthetic never-claimed runs
and pre-admission run IDs cannot hand off. All retained run PIDs are checked using
native host/fingerprint liveness helpers, including completed runs whose task row
has cleared its PID. A live or nonlocal/uncertain worker prevents verification;
controller never kills a Builder or impersonates it.

Durability, stages and publication boundary
------------------------------------------
Actual Director principal is established before Ledger writes. One private
FULL-synchronous SQLite ledger/lock serializes all plans for that Director.
Authorization reservation and admission intent are one transaction; native board
write is separate and deliberately never replayed after uncertainty. Existing
admission does not require source to remain pristine after Builder edits. Policy
or frozen identity drift halts; existing state cannot consume another allowance,
unblock, re-admit, adopt, review, or commit on replay. Durable event outbox retries
only identical audit emissions. Ambiguous action intent is cleaned and halted, not
rerun. No automatic restart/resumption gate is implied by tick/admit.

waiting_source -> verify -> precommit_review -> commit -> exact_review ->
request_push -> awaiting_push. Any failure/rejection/drift halts. Each native review
phase is single-attempt and durably counted before side effects (two total across
restarts). No agent/title/auxiliary/fallback calls are made. The installed launcher's
interpreter, flags and owning bootstrap are preserved; runtime_command_sha256 binds
the canonical JSON of the entire unmodified --print-runtime-command argv before
dispatch replacement. It is prepared read-only before authorization. The command
hash does not replace the independently required installed dependency-tree review; only a single terminal
runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True) is replaced
after AST/marker validation. Candidate modules are never host-imported for review.
A structured verdict has exact phase/base/head/diff_hash/manifest_hash bindings;
APPROVE requires empty findings and positive actual token usage. Explicit rejection
halts, and generic init/transport errors remain review_failed, never invented quota
or reset-time claims.

Positive baseline JUnit must collect >0 tests without errors, skips or failures and
exit 0. Only the disposable verification copy gets the approved one-occurrence
before/after replacement; pinned whole-base/snippet/result hashes are checked.
Mutant must exit exactly 1 with selected test/file AssertionError or narrowly
recognized Pytest Failed: DID NOT RAISE ValueError. Generic pytest.fail, wrong
exception expectation, unrelated failures, collection errors, skip and arbitrary
nonzero exit are not proof. Actual bounded stdout/stderr/JUnit hashes and resources
are persisted. scripts/run_tests.sh receives the test path and explicit -- followed
by --junitxml and selected -k, with the existing mounted PM test interpreter. No
provisioning/lazy install is permitted; interpreter must already import pytest.

Git uses fixed argv without shell, global/system config, hooks, fsmonitor, external
diff/textconv, filters, lazy object fetch, network or submodule recursion. All
baseline archive blobs/modes are checked against Git objects before container-only
extraction; symlinks, gitlinks, export-ignore and export-subst transformations fail
closed. A frozen overlay captures only approved new regular mode-100644 test bytes.
Index, branch, base, manifest/diff/tree and live bytes must remain aligned. Commit
requires positive verification plus precommit APPROVE. Exact fresh committed SHA
then receives a distinct review. Last action requests native human PUSH gate on
that exact SHA with proof/contract binding. No push, merge, PR creation, deployment,
or decision of that gate is implemented/authorized.

Owned containment and interruption
---------------------------------
Candidate execution: nonroot, network none, read-only root, all capabilities dropped,
no-new-privileges, memory/swap/CPU/PID limits, immutable local image, no credential or
Docker socket mounts. Readonly PM generations/tools are the only host mounts.
Baseline archive/overlay stream as a bounded RAM packet into a size-limited private
/var/tmp tmpfs; /tmp is separately size-limited. No candidate source or aggregate
scratch bind is created on host. Archive packet <=132 MiB, output packet <=4 MiB,
and each decoded JUnit/stdout/stderr <=1 MiB. Evidence is bounded per action and
intentionally retained in private state_root; operators must separately manage
long-term ledger/evidence capacity, which is not a filesystem-wide quota service.

Container name and exact per-action label are persisted before Docker run. Cleanup
inspects the exact name/label, removes only the owned container and verifies absence;
daemon/permission failure is not silently treated as absence. SIGTERM is converted
to a controlled interruption; KeyboardInterrupt also unwinds cleanup. Abrupt death
leaves durable intent and owned identifiers: the next authorized tick cleans only
those identifiers, then halts without inference. The container's own runner timeout
bounds execution even if the controlling process is killed. Never manually clear
intent/attempts or delete a ledger to retry uncertain work.

Native helper timeout/group cleanup sends graceful SIGTERM and waits before SIGKILL
so finally: client.close() can run. Native CLI sessions also require a per-review
transient USER systemd service/cgroup because setsid children escape helper groups.
The random persisted hermes-fleet-review-<plan-prefix>-<uuid>.service has an exact
plan/UUID Description; cleanup refuses a mismatched unit. KillMode=mixed gives the
helper time to close, then terminates every remaining cgroup process; RuntimeMaxSec
is bounded. Missing/unavailable user bus, unsupported properties or failed cleanup
fail closed, not an uncontained fallback. No systemd/service operation was performed
in this pass. Future activation requires separately approved same-user transient
unit creation/inspection/stop authority, functioning user manager/bus, nonroot Docker
access, immutable test image and read-only PM mounts, and contained process fixtures
proving timeout/abrupt-death cleanup. No broad PID discovery or arbitrary kill exists.

Notifications and remaining qualification
----------------------------------------
Native verification_passed, review_approved (separate phases), commit_created and
fixed phase/reason failure events use an idempotent outbox. The existing Default
Slack subscription must be exact team/channel/thread with delivery_mode=notify;
controller events never automatically wake Default or imply an inference turn.
The native final gate notification remains a separate producer/consumer path.
Gate delivery reports delivered only when the subscription cursor matches that
actual request event; pending is not actual Slack delivery. Recording-adapter
fixtures prove only passive producer/consumer mechanics, not actual transmission.

Controller owns canonical tests, Git review and acceptance after worker exit. Added
fixtures cover real native SQLite/kernel/gate transitions with EXPLICIT fake external
side effects; throwaway Git is real when those tests run, Docker/systemd/native IPC
are not. No tests were executed by this worker. Installed owning bootstrap/dependency
closure alignment, user-manager/container interruption integration and the actual
subscription client installation still require independent contained verification.
These are operational/source-readiness blockers, not completed by AST parsing or
unit fixtures. No whole-hub or autonomous live-handoff readiness claim follows.
Source/profile alignment, deployment, scheduler activation, new admission, and any
new ENS9 resumption/retry each need their own explicit review/authorization. Keep
existing dispatcher hold, fleet guard and native reference unchanged.
