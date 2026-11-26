import contextlib
import io
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import freeze  # noqa: E402
import projects  # noqa: E402
from github import ApiError  # noqa: E402
from test_enroll import (ALICE, BOB, CATALOG, CONTEST_TIME, EMPTY, MALLORY, ORG, TEMPLATE_SHA, WARMUP_TIME,  # noqa: E402
                         FakeGitHub, FakeIssues, claim, github_time, run, state, warm_up)
from test_idle import STAFF  # noqa: E402

DEADLINE = CATALOG.contest.closes
LATE = DEADLINE + timedelta(hours=1)
FULL = f"{ORG}/app-excalidraw-alice"


def update(api, full, actor, sha, when, ref="refs/heads/main"):
    api.activity[full].insert(0, {"activity_type": "push", "ref": ref, "after": sha,
                                  "actor": dict(actor), "timestamp": github_time(when)})


def sweep(api, now=LATE, catalog=CATALOG, issues=None):
    issues = issues or FakeIssues()
    with contextlib.redirect_stdout(io.StringIO()):
        freeze.freeze(api, issues, f"{ORG}/enroll", ORG, catalog, now)
    return issues


def workflow_run(api, now, event="schedule", issues=None, catalog=CATALOG, **env):
    issues = issues or FakeIssues()
    log = io.StringIO()
    environ = {"GITHUB_REPOSITORY": f"{ORG}/enroll", "GITHUB_REPOSITORY_OWNER": ORG, "GITHUB_EVENT_NAME": event,
               "GH_TOKEN": "app", "ISSUE_TOKEN": "issues", **env}
    with mock.patch.dict(os.environ, environ, clear=True), \
            mock.patch.object(freeze, "GitHub", {"app": api, "issues": issues}.get), \
            mock.patch.object(projects, "load", lambda: catalog), \
            mock.patch.object(freeze, "datetime") as clock, \
            contextlib.redirect_stdout(log):
        clock.now.return_value = now
        freeze.main()
    return issues, log.getvalue()


@contextlib.contextmanager
def stopped(api, method, when):
    """Make one kind of write fail, as a run that crashes there would."""
    original = getattr(api, method)

    def fail(path, body):
        if when(body):
            raise ApiError(method.upper(), path, 500, "stopped")
        return original(path, body)

    setattr(api, method, fail)
    try:
        yield
    finally:
        setattr(api, method, original)


def recorded(api, full=FULL):
    return {name: variable["value"] for name, variable in api.variables[full].items()
            if name in ("FROZEN_SHA", "FROZEN_PUSHED_AT", "SUBMITTED")}


def claimed():
    api = FakeGitHub()
    claim(api, "excalidraw", issue=7)
    return api


class FreezeTest(unittest.TestCase):
    def test_last_update_of_main_by_the_deadline_is_recorded_and_the_repository_archived(self):
        api = claimed()
        update(api, FULL, ALICE, "draft", CONTEST_TIME + timedelta(hours=1))
        update(api, FULL, ALICE, "final", DEADLINE)
        update(api, FULL, ALICE, "too-late", DEADLINE + timedelta(seconds=1))
        issues = sweep(api)
        self.assertEqual(recorded(api), {"FROZEN_SHA": "final", "FROZEN_PUSHED_AT": github_time(DEADLINE), "SUBMITTED": "true"})
        self.assertTrue(api.repos[FULL]["archived"])
        [comment] = issues.comments[7]
        self.assertTrue(comment.startswith("@alice "))
        self.assertIn("https://github.com/org/app-excalidraw-alice/commit/final", comment)
        self.assertNotIn("没有记录到", comment)

    def test_updates_of_other_branches_do_not_count(self):
        api = claimed()
        update(api, FULL, ALICE, "feature", CONTEST_TIME + timedelta(hours=1), ref="refs/heads/feature")
        sweep(api)
        self.assertEqual(recorded(api), {"FROZEN_SHA": TEMPLATE_SHA, "FROZEN_PUSHED_AT": github_time(CONTEST_TIME), "SUBMITTED": "false"})

    def test_main_updated_only_by_the_bot_or_staff_is_frozen_but_not_submitted(self):
        for staff_push in (False, True):
            with self.subTest(staff_push=staff_push):
                api = claimed()
                if staff_push:
                    update(api, FULL, STAFF, "staff-fix", CONTEST_TIME + timedelta(hours=1))
                issues = sweep(api)
                self.assertEqual(recorded(api)["FROZEN_SHA"], "staff-fix" if staff_push else TEMPLATE_SHA)
                self.assertEqual(recorded(api)["SUBMITTED"], "false")
                [comment] = issues.comments[7]
                self.assertIn("没有记录到", comment)

    def test_pushes_to_main_before_the_claim_are_frozen_but_not_submitted(self):
        api = FakeGitHub()
        warm_up(api, "excalidraw", issue=3)
        update(api, FULL, ALICE, "warm-up", WARMUP_TIME + timedelta(hours=1))
        claim(api, "excalidraw", issue=7)
        sweep(api)
        self.assertEqual(recorded(api), {"FROZEN_SHA": "warm-up", "FROZEN_PUSHED_AT": github_time(WARMUP_TIME + timedelta(hours=1)),
                                         "SUBMITTED": "false"})

    def test_frozen_commit_is_found_behind_a_full_page_of_later_updates(self):
        api = claimed()
        update(api, FULL, ALICE, "final", CONTEST_TIME + timedelta(hours=1))
        for number in range(100):
            update(api, FULL, ALICE, f"late-{number}", DEADLINE + timedelta(minutes=1))
        sweep(api, DEADLINE + timedelta(hours=2))
        self.assertEqual(recorded(api)["FROZEN_SHA"], "final")

    def test_nothing_is_frozen_until_the_contest_closes(self):
        api = claimed()
        before = state(api)
        for now in (CONTEST_TIME, DEADLINE):
            with self.subTest(now=now):
                issues = sweep(api, now)
                self.assertEqual((state(api), issues.comments), (before, {}))

    def test_second_run_changes_nothing(self):
        api = claimed()
        sweep(api)
        before = state(api)
        issues = sweep(api, LATE + timedelta(hours=1))
        self.assertEqual((state(api), issues.comments), (before, {}))

    def test_rerun_archives_a_recorded_entry_without_computing_it_again(self):
        api = claimed()
        update(api, FULL, ALICE, "final", CONTEST_TIME + timedelta(hours=1))
        with stopped(api, "patch", lambda body: "archived" in body), self.assertRaises(ApiError):
            sweep(api)
        record = recorded(api)
        api.activity[FULL].clear()
        issues = sweep(api, LATE + timedelta(hours=1))
        self.assertEqual(record, {"FROZEN_SHA": "final", "FROZEN_PUSHED_AT": github_time(CONTEST_TIME + timedelta(hours=1)),
                                  "SUBMITTED": "true"})
        self.assertEqual(recorded(api), record)
        self.assertTrue(api.repos[FULL]["archived"])
        [comment] = issues.comments[7]
        self.assertIn("/commit/final", comment)

    def test_rerun_works_out_an_entry_whose_record_stopped_before_the_sha(self):
        for stop in ("FROZEN_PUSHED_AT", "SUBMITTED", "FROZEN_SHA"):
            with self.subTest(stopped_at=stop):
                api = claimed()
                update(api, FULL, ALICE, "final", CONTEST_TIME + timedelta(hours=1))
                with stopped(api, "post", lambda body: body.get("name") == stop), self.assertRaises(ApiError):
                    sweep(api)
                self.assertFalse(api.repos[FULL]["archived"])
                sweep(api, LATE + timedelta(hours=1))
                self.assertEqual(recorded(api), {"FROZEN_SHA": "final", "FROZEN_PUSHED_AT": github_time(CONTEST_TIME + timedelta(hours=1)),
                                                 "SUBMITTED": "true"})
                self.assertTrue(api.repos[FULL]["archived"])

    def test_entry_whose_issue_is_gone_is_frozen_and_the_sweep_goes_on(self):
        api = claimed()
        claim(api, "obsidian", BOB, issue=8)
        issues = FakeIssues(gone={7})
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            freeze.freeze(api, issues, f"{ORG}/enroll", ORG, CATALOG, LATE)
        self.assertTrue(api.repos[FULL]["archived"])
        self.assertTrue(api.repos["org/app-obsidian-bob"]["archived"])
        self.assertEqual(list(issues.comments), [8])
        self.assertIn("#7", log.getvalue())

    def test_list_mode_freezes_only_the_active_contest_claims(self):
        api = FakeGitHub()
        warm_up(api, "excalidraw", MALLORY, issue=2)
        claim(api, "hello", BOB, now=WARMUP_TIME, issue=3)
        claim(api, "obsidian", BOB, issue=4)
        api.repos["org/app-obsidian-bob"]["archived"] = True
        claim(api, "excalidraw", issue=7)
        issues = sweep(api)
        self.assertEqual({full for full, values in api.variables.items() if "FROZEN_SHA" in values}, {FULL})
        self.assertEqual({full for full, repo in api.repos.items() if repo["archived"]}, {FULL, "org/app-obsidian-bob"})
        self.assertEqual(list(issues.comments), [7])

    def test_free_mode_freezes_every_bound_repository_made_by_the_deadline(self):
        api = FakeGitHub()
        warm_up(api, "draft", issue=3)
        run(api, BOB, app="notes")
        update(api, "org/app-notes-bob", BOB, "notes", CONTEST_TIME + timedelta(hours=1))
        api.repos["org/app-unbound-bob"] = api.repo("app-unbound-bob")
        api.now = DEADLINE + timedelta(seconds=1)
        run(api, MALLORY, app="late")
        issues = sweep(api, catalog=EMPTY)
        self.assertEqual(recorded(api, "org/app-draft-alice"), {"FROZEN_SHA": TEMPLATE_SHA, "FROZEN_PUSHED_AT": github_time(WARMUP_TIME),
                                                                 "SUBMITTED": "false"})
        self.assertEqual(recorded(api, "org/app-notes-bob")["SUBMITTED"], "true")
        self.assertEqual({full for full, repo in api.repos.items() if repo["archived"]}, {"org/app-draft-alice", "org/app-notes-bob"})
        self.assertEqual(list(issues.comments), [3])

    def test_entries_that_cannot_be_frozen_are_left_and_the_run_fails_after_the_others(self):
        def broken_claimed_at(api):
            claim(api, "excalidraw", issue=7)
            api.variables[FULL]["CLAIMED_AT"]["value"] = "2026-12-03 10:00"

        def claimed_after_the_deadline(api):
            api.now = DEADLINE + timedelta(seconds=30)
            claim(api, "excalidraw", now=DEADLINE - timedelta(seconds=30), issue=7)

        for start in (broken_claimed_at, claimed_after_the_deadline):
            with self.subTest(case=start.__name__):
                api = FakeGitHub()
                start(api)
                api.now = CONTEST_TIME
                claim(api, "obsidian", BOB, issue=8)
                issues = FakeIssues()
                with self.assertRaises(ValueError) as failure:
                    sweep(api, issues=issues)
                self.assertFalse(api.repos[FULL]["archived"])
                self.assertEqual(recorded(api), {})
                self.assertTrue(api.repos["org/app-obsidian-bob"]["archived"])
                self.assertEqual(list(issues.comments), [8])
                self.assertIn("https://github.com/org/app-excalidraw-alice", str(failure.exception))

    def test_scheduled_run_freezes_once_the_clock_passes_the_deadline(self):
        for now, archived in [(DEADLINE, False), (LATE, True)]:
            with self.subTest(now=now):
                api = claimed()
                workflow_run(api, now)
                self.assertEqual(api.repos[FULL]["archived"], archived)


if __name__ == "__main__":
    unittest.main()
