import contextlib
import copy
import io
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import enroll  # noqa: E402
import projects  # noqa: E402
from github import ApiError  # noqa: E402
from test_projects import entry, listing  # noqa: E402

ORG = "org"
ALICE = {"login": "alice", "id": 1, "type": "User"}
MALLORY = {"login": "mallory", "id": 2, "type": "User"}
BOB = {"login": "bob", "id": 11, "type": "User"}
BOT = {"login": "enroll-bot[bot]", "id": 900}
TEMPLATE_SHA = "template"
BEIJING = projects.BEIJING
WARMUP_TIME = datetime(2026, 11, 28, 12, tzinfo=BEIJING)
CONTEST_TIME = datetime(2026, 12, 2, 12, tzinfo=BEIJING)


def github_time(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    def __init__(self):
        self.users = {"alice": dict(ALICE), "mallory": dict(MALLORY), "bob": dict(BOB)}
        self.now = CONTEST_TIME
        self.activity = {}
        self.repos = {f"{ORG}/app-template": self.repo("app-template", template=None, is_template=True)}
        self.variables = {}
        self.rulesets = {}
        self.collaborators = {}
        self.invitations = {}
        self.calls = []
        self.next_id = 100

    def repo(self, name, template=f"{ORG}/app-template", is_template=False, description=None):
        full = f"{ORG}/{name}"
        return {"name": name, "full_name": full, "html_url": "https://github.com/" + full, "description": description,
                "default_branch": "main", "created_at": github_time(self.now),
                "is_template": is_template, "private": False, "archived": False,
                "template_repository": {"full_name": template} if template else None}

    def missing(self, method, path):
        raise ApiError(method, path, 404, "Not Found")

    def writable(self, method, path, full):
        if self.repos[full]["archived"]:
            raise ApiError(method, path, 403, "Repository was archived so is read-only.")

    def get(self, path, missing_ok=False):
        self.calls.append(("GET", path))
        parts = path.split("?")[0].split("/")
        result = None
        if parts[0] == "users":
            result = self.users.get(parts[1])
        elif parts[0] == "orgs":
            result = [repo for full, repo in self.repos.items() if full.startswith(parts[1] + "/")]
        elif parts[0] == "repos":
            full = parts[1] + "/" + parts[2]
            rest = parts[3:]
            if full not in self.repos:
                result = None
            elif not rest:
                result = self.repos[full]
            elif rest[0] == "branches":
                result = {"name": rest[1]}
            elif rest == ["actions", "variables"]:
                found = list(self.variables.get(full, {}).values())
                result = {"total_count": len(found), "variables": found}
            elif rest[:2] == ["actions", "variables"]:
                result = self.variables.get(full, {}).get(rest[2])
            elif rest[0] == "rulesets":
                result = self.rulesets.get(full, [])
            elif rest[0] == "collaborators" and len(rest) == 1:
                result = [{"login": login} for login in self.collaborators.get(full, {})]
            elif rest[0] == "collaborators":
                user = self.users[rest[1]]
                result = {"user": user, "permission": self.collaborators.get(full, {}).get(rest[1], "read")}
            elif rest[0] == "invitations":
                result = list(self.invitations.get(full, {}).values())
            elif rest[0] == "activity":
                result = self.activity.get(full, [])[:100]
        if result is None:
            if missing_ok:
                return None
            self.missing("GET", path)
        return result

    def pages(self, path):
        yield from self.get(path)

    def cursor_pages(self, path):
        self.calls.append(("GET", path))
        parts = path.split("?")[0].split("/")
        yield from self.activity.get(parts[1] + "/" + parts[2], [])

    def post(self, path, body):
        self.calls.append(("POST", path))
        parts = path.split("/")
        full = parts[1] + "/" + parts[2]
        if parts[3] == "generate":
            target = body["owner"] + "/" + body["name"]
            if target in self.repos:
                raise ApiError("POST", path, 422, "name already exists")
            self.repos[target] = self.repo(body["name"], template=full, description=body["description"])
            self.activity[target] = [{"activity_type": "branch_creation", "ref": "refs/heads/main", "after": TEMPLATE_SHA,
                                      "actor": dict(BOT), "timestamp": github_time(self.now)}]
        elif parts[3:] == ["actions", "variables"]:
            self.writable("POST", path, full)
            self.variables.setdefault(full, {})[body["name"]] = dict(body)
        elif parts[3] == "rulesets":
            self.rulesets.setdefault(full, []).append(body)
        return {}

    def patch(self, path, body):
        self.calls.append(("PATCH", path))
        parts = path.split("/")
        full = parts[1] + "/" + parts[2]
        if len(parts) == 3:
            self.repos[full].update(body)
            return
        self.writable("PATCH", path, full)
        self.variables[full][parts[5]] = dict(body)

    def put(self, path, body=None):
        self.calls.append(("PUT", path))
        parts = path.split("/")
        full, login = parts[1] + "/" + parts[2], parts[4]
        self.next_id += 1
        self.invitations.setdefault(full, {})[self.next_id] = {
            "id": self.next_id, "invitee": {"id": self.users[login]["id"]}, "expired": False, "permissions": "write"}
        return {"id": self.next_id}

    def delete(self, path):
        self.calls.append(("DELETE", path))
        parts = path.split("/")
        self.invitations[parts[1] + "/" + parts[2]].pop(int(parts[4]))


class FakeIssues:
    def __init__(self, issue=None, gone=()):
        self.issue = issue
        self.gone = set(gone)
        self.comments = {}

    def get(self, path):
        return self.issue

    def post(self, path, body):
        number = int(path.split("/")[4])
        if number in self.gone:
            raise ApiError("POST", path, 404, "Not Found")
        self.comments.setdefault(number, []).append(body["body"])

    def patch(self, path, body):
        self.issue.update(body)


def run(api, applicant=ALICE, app="notes"):
    return enroll.enroll(api, ORG, applicant, app, sleep=lambda _: None)


CATALOG = projects.parse(listing(entry("hello", warmup=True), entry("excalidraw"), entry("obsidian"), entry("foo")))
EMPTY = projects.parse(listing())


def claim(api, project, applicant=ALICE, now=CONTEST_TIME, **options):
    return enroll.enroll(api, ORG, applicant, project, sleep=lambda _: None, catalog=CATALOG, now=now, clock=lambda: api.now, **options)


def warm_up(api, app, applicant=ALICE, issue=None):
    """A repository the player made in free mode during the warm-up, before the list had projects."""
    api.now = WARMUP_TIME
    enroll.enroll(api, ORG, applicant, app, sleep=lambda _: None, issue=issue)
    api.now = CONTEST_TIME


def form(app):
    return f"### 应用名\n\n{app}\n\n### 确认\n\n- [X] 我用的是自己的个人 GitHub 账号, 并同意作品以公开仓库的形式提交"


def give_up_form(app):
    return f"### 应用名\n\n{app}\n\n### 确认\n\n- [X] 我知道放弃以后不能再往这个仓库推送, 也不能再领这个项目"


def open_issue(api, body, title=enroll.TITLE, user=ALICE, opened=CONTEST_TIME, catalog=EMPTY, issues=None, **env):
    issues = issues or FakeIssues()
    issues.issue = {"number": 7, "title": title, "body": body, "user": user, "state": "open", "created_at": github_time(opened)}
    environ = {"GITHUB_REPOSITORY": f"{ORG}/enroll", "GITHUB_REPOSITORY_OWNER": ORG,
               "GH_TOKEN": "app", "ISSUE_TOKEN": "issues", "ISSUE_NUMBER": "7", **env}
    with mock.patch.dict(os.environ, environ, clear=True), \
            mock.patch.object(enroll, "GitHub", {"app": api, "issues": issues}.get), \
            mock.patch.object(projects, "load", lambda: catalog), \
            contextlib.redirect_stdout(io.StringIO()):
        enroll.main()
    return issues


def state(api):
    return copy.deepcopy((api.repos, api.variables, api.rulesets, api.collaborators, api.invitations))


class EnrollTest(unittest.TestCase):
    def test_new_applicant_gets_a_bound_protected_repository_and_an_invitation(self):
        api = FakeGitHub()
        url, access = run(api)
        self.assertEqual(url, "https://github.com/org/app-notes-alice")
        self.assertEqual(access, "invited")
        self.assertEqual(api.variables["org/app-notes-alice"]["STUDENT_ID"]["value"], "1")
        self.assertEqual(api.variables["org/app-notes-alice"]["STUDENT_LOGIN"]["value"], "alice")
        self.assertEqual(api.variables["org/app-notes-alice"]["APP_NAME"]["value"], "notes")
        [ruleset] = api.rulesets["org/app-notes-alice"]
        self.assertEqual({rule["type"] for rule in ruleset["rules"]}, {"deletion", "non_fast_forward"})
        self.assertEqual(ruleset["conditions"]["ref_name"]["include"], ["refs/heads/main"])

    def test_retry_is_idempotent(self):
        api = FakeGitHub()
        run(api)
        run(api)
        self.assertEqual(len(api.rulesets["org/app-notes-alice"]), 1)
        self.assertEqual(len(api.invitations["org/app-notes-alice"]), 1)
        self.assertEqual(sum(call == ("POST", "repos/org/app-template/generate") for call in api.calls), 1)

    def test_collaborator_with_write_is_not_invited_again(self):
        api = FakeGitHub()
        run(api)
        api.invitations["org/app-notes-alice"].clear()
        api.collaborators["org/app-notes-alice"] = {"alice": "write"}
        self.assertEqual(run(api)[1], "write")
        self.assertFalse(api.invitations["org/app-notes-alice"])

    def test_expired_invitation_is_replaced(self):
        api = FakeGitHub()
        run(api)
        [old] = api.invitations["org/app-notes-alice"]
        api.invitations["org/app-notes-alice"][old]["expired"] = True
        self.assertEqual(run(api)[1], "invited")
        self.assertNotIn(old, api.invitations["org/app-notes-alice"])
        self.assertEqual(len(api.invitations["org/app-notes-alice"]), 1)

    def test_repository_bound_to_another_account_is_refused(self):
        api = FakeGitHub()
        run(api)
        api.users["alice"] = {"login": "alice", "id": 3, "type": "User"}
        with self.assertRaises(enroll.Refusal):
            run(api, api.users["alice"])
        self.assertEqual(api.variables["org/app-notes-alice"]["STUDENT_ID"]["value"], "1")

    def test_renamed_applicant_is_refused(self):
        api = FakeGitHub()
        api.users["alice"] = {"login": "alice", "id": 9, "type": "User"}
        with self.assertRaises(enroll.Refusal):
            run(api)
        self.assertNotIn("org/app-notes-alice", api.repos)

    def test_organization_account_is_refused(self):
        api = FakeGitHub()
        api.users["alice"]["type"] = "Organization"
        with self.assertRaises(enroll.Refusal):
            run(api)

    def test_foreign_repository_with_the_same_name_is_left_alone(self):
        api = FakeGitHub()
        api.repos["org/app-notes-alice"] = api.repo("app-notes-alice", template=None)
        with self.assertRaises(RuntimeError):
            run(api)
        self.assertNotIn("org/app-notes-alice", api.variables)

    def test_unbound_repository_with_outside_access_is_left_alone(self):
        api = FakeGitHub()
        api.repos["org/app-notes-alice"] = api.repo("app-notes-alice")
        api.collaborators["org/app-notes-alice"] = {"mallory": "write"}
        with self.assertRaises(RuntimeError):
            run(api)
        self.assertNotIn("org/app-notes-alice", api.variables)

    def test_unbound_repository_from_an_interrupted_run_is_resumed(self):
        api = FakeGitHub()
        api.repos["org/app-notes-alice"] = api.repo("app-notes-alice")
        self.assertEqual(run(api)[1], "invited")
        self.assertEqual(api.variables["org/app-notes-alice"]["STUDENT_ID"]["value"], "1")

    def test_template_must_be_a_template(self):
        api = FakeGitHub()
        api.repos["org/app-template"]["is_template"] = False
        with self.assertRaises(RuntimeError):
            run(api)
        self.assertNotIn("org/app-notes-alice", api.repos)

    def test_each_app_gets_its_own_repository(self):
        api = FakeGitHub()
        run(api, app="notes")
        url, _ = run(api, app="markdown-editor")
        self.assertEqual(url, "https://github.com/org/app-markdown-editor-alice")
        self.assertIn("org/app-notes-alice", api.repos)

    def test_name_taken_by_another_account_is_refused(self):
        api = FakeGitHub()
        api.users["bar-alice"] = {"login": "bar-alice", "id": 4, "type": "User"}
        run(api, api.users["bar-alice"], app="foo")
        api.users["alice"] = dict(ALICE)
        with self.assertRaises(enroll.Refusal):
            run(api, app="foo-bar")
        self.assertEqual(api.variables["org/app-foo-bar-alice"]["STUDENT_ID"]["value"], "4")

    def test_app_name_is_read_from_the_issue_form(self):
        body = "### 应用名\n\nMarkdown-Editor\n\n### 确认\n\n- [X] 我用的是自己的个人 GitHub 账号"
        self.assertEqual(enroll.app_name(body), "markdown-editor")
        self.assertEqual(enroll.app_name("### 应用名\n\nnotes"), "notes")

    def test_bad_app_names_are_refused(self):
        for value in ["_No response_", "-x", "x-", "a--b", "a b", "名字", "a" * 41, ""]:
            with self.subTest(value=value), self.assertRaises(enroll.Refusal):
                enroll.app_name(f"### 应用名\n\n{value}\n\n### 确认\n\n- [X] ok")
        with self.assertRaises(enroll.Refusal):
            enroll.app_name(None)

    def test_success_message_skips_the_invitation_step_for_writers(self):
        self.assertNotIn("invitations", enroll.success_message("alice", "https://github.com/org/app-notes-alice", "write"))
        self.assertIn("/invitations", enroll.success_message("alice", "https://github.com/org/app-notes-alice", "invited"))



class ListModeTest(unittest.TestCase):
    def test_listed_project_claimed_inside_its_window_gets_a_repository(self):
        api = FakeGitHub()
        self.assertEqual(claim(api, "excalidraw", issue=7), ("https://github.com/org/app-excalidraw-alice", "invited"))
        variables = {name: variable["value"] for name, variable in api.variables["org/app-excalidraw-alice"].items()}
        self.assertEqual(projects.moment(variables.pop("CLAIMED_AT")), CONTEST_TIME)
        self.assertEqual(variables, {"STUDENT_ID": "1", "STUDENT_LOGIN": "alice", "PROJECT": "excalidraw", "APP_NAME": "excalidraw", "CLAIM_ISSUE": "7"})

    def test_id_not_in_the_list_is_refused(self):
        api = FakeGitHub()
        with self.assertRaises(enroll.Refusal):
            claim(api, "notes")
        self.assertEqual(set(api.repos), {"org/app-template"})

    def test_each_project_is_claimed_only_inside_its_window(self):
        moments = {
            "before the warm-up": datetime(2026, 11, 26, 23, 59, 59, tzinfo=BEIJING),
            "warm-up": WARMUP_TIME,
            "last second of the warm-up": datetime(2026, 11, 29, 23, 59, 59, tzinfo=BEIJING),
            "between": datetime(2026, 11, 30, 12, tzinfo=BEIJING),
            "contest": CONTEST_TIME,
            "after the contest": datetime(2026, 12, 8, tzinfo=BEIJING),
        }
        open_at = {"hello": {"warm-up", "last second of the warm-up"}, "excalidraw": {"contest"}}
        for project, windows in open_at.items():
            for label, now in moments.items():
                with self.subTest(project=project, at=label):
                    api = FakeGitHub()
                    if label in windows:
                        claim(api, project, now=now)
                    else:
                        with self.assertRaises(enroll.Refusal):
                            claim(api, project, now=now)
                    self.assertEqual(f"org/app-{project}-alice" in api.repos, label in windows)

    def test_archived_repository_is_not_claimed_again(self):
        for mode, start in [("free", lambda api: run(api, app="excalidraw")), ("list", lambda api: claim(api, "excalidraw"))]:
            with self.subTest(mode=mode):
                api = FakeGitHub()
                start(api)
                api.repos["org/app-excalidraw-alice"]["archived"] = True
                before = state(api)
                with self.assertRaises(enroll.Refusal):
                    start(api)
                self.assertEqual(state(api), before)


class ExclusiveTest(unittest.TestCase):
    def test_contest_project_held_by_another_player_is_refused(self):
        api = FakeGitHub()
        claim(api, "excalidraw", BOB)
        before = state(api)
        with self.assertRaises(enroll.Refusal):
            claim(api, "excalidraw")
        self.assertEqual(state(api), before)

    def test_archived_claim_no_longer_holds_the_project(self):
        api = FakeGitHub()
        claim(api, "excalidraw", BOB)
        api.repos["org/app-excalidraw-bob"]["archived"] = True
        self.assertEqual(claim(api, "excalidraw")[0], "https://github.com/org/app-excalidraw-alice")

    def test_rerunning_your_own_claim_changes_nothing(self):
        api = FakeGitHub()
        claim(api, "excalidraw", issue=7)
        before = state(api)
        claim(api, "excalidraw", issue=7)
        self.assertEqual(state(api), before)

    def test_renamed_player_cannot_hold_a_project_twice(self):
        api = FakeGitHub()
        claim(api, "excalidraw", BOB)
        bobby = api.users["bobby"] = dict(api.users.pop("bob"), login="bobby")
        before = state(api)
        with self.assertRaises(enroll.Refusal) as refused:
            claim(api, "excalidraw", bobby)
        self.assertIn("https://github.com/org/app-excalidraw-bob", str(refused.exception))
        self.assertEqual(state(api), before)

    def test_warm_up_project_is_open_to_everyone(self):
        api = FakeGitHub()
        claim(api, "hello", BOB, now=WARMUP_TIME)
        claim(api, "hello", now=WARMUP_TIME)
        self.assertLessEqual({"org/app-hello-bob", "org/app-hello-alice"}, set(api.repos))

    def test_project_is_not_held_by_a_repository_for_a_longer_app_name(self):
        api = FakeGitHub()
        run(api, BOB, app="foo-bar")
        claim(api, "foo")
        self.assertIn("org/app-foo-alice", api.repos)

    def test_claim_that_stopped_before_recording_its_project_does_not_hold_it(self):
        api = FakeGitHub()
        api.repos["org/app-excalidraw-bob"] = api.repo("app-excalidraw-bob", description="@bob")
        claim(api, "excalidraw")
        before = state(api)
        with self.assertRaises(enroll.Refusal):
            claim(api, "excalidraw", BOB)
        self.assertEqual(state(api), before)

    def test_warm_up_repository_does_not_hold_a_contest_project(self):
        api = FakeGitHub()
        warm_up(api, "excalidraw")
        self.assertEqual(claim(api, "excalidraw", BOB)[0], "https://github.com/org/app-excalidraw-bob")

    def test_warm_up_repository_is_checked_like_a_new_claim(self):
        api = FakeGitHub()
        claim(api, "excalidraw", BOB)
        warm_up(api, "excalidraw")
        before = state(api)
        with self.assertRaises(enroll.Refusal):
            claim(api, "excalidraw")
        self.assertEqual(state(api), before)

    def test_owner_claims_their_warm_up_repository_for_the_contest(self):
        api = FakeGitHub()
        warm_up(api, "excalidraw", issue=3)
        self.assertEqual(claim(api, "excalidraw", issue=7)[0], "https://github.com/org/app-excalidraw-alice")
        variables = api.variables["org/app-excalidraw-alice"]
        self.assertEqual(variables["PROJECT"]["value"], "excalidraw")
        self.assertEqual(projects.moment(variables["CLAIMED_AT"]["value"]), CONTEST_TIME)
        self.assertEqual(sum(call == ("POST", "repos/org/app-template/generate") for call in api.calls), 1)
        with self.assertRaises(enroll.Refusal):
            claim(api, "excalidraw", BOB)

    def test_max_repos_counts_contest_claims_but_not_warm_up_repositories(self):
        api = FakeGitHub()
        warm_up(api, "excalidraw")
        warm_up(api, "obsidian")
        claim(api, "foo", max_repos=1)
        before = state(api)
        with self.assertRaises(enroll.Refusal):
            claim(api, "excalidraw", max_repos=1)
        self.assertEqual(state(api), before)

    def test_max_repos_limits_the_contest_projects_held_at_once(self):
        api = FakeGitHub()
        api.users["bar-alice"] = {"login": "bar-alice", "id": 4, "type": "User"}
        claim(api, "foo", api.users["bar-alice"])
        claim(api, "hello", now=WARMUP_TIME)
        claim(api, "excalidraw", max_repos=1)
        before = state(api)
        with self.assertRaises(enroll.Refusal):
            claim(api, "obsidian", max_repos=1)
        self.assertEqual(state(api), before)
        claim(api, "excalidraw", max_repos=1)
        self.assertEqual(state(api), before)
        api.repos["org/app-excalidraw-alice"]["archived"] = True
        claim(api, "obsidian", max_repos=1)
        self.assertIn("org/app-obsidian-alice", api.repos)

    def test_without_max_repos_a_player_holds_any_number_of_projects(self):
        api = FakeGitHub()
        for project in ("excalidraw", "obsidian", "foo"):
            claim(api, project)
        self.assertLessEqual({"org/app-excalidraw-alice", "org/app-obsidian-alice", "org/app-foo-alice"}, set(api.repos))


class GiveUpTest(unittest.TestCase):
    def test_owner_gives_up_and_the_project_returns_to_the_list(self):
        api = FakeGitHub()
        claim(api, "excalidraw")
        self.assertEqual(enroll.give_up(api, ORG, ALICE, "excalidraw", CONTEST_TIME), ("https://github.com/org/app-excalidraw-alice", True))
        self.assertTrue(api.repos["org/app-excalidraw-alice"]["archived"])
        variables = api.variables["org/app-excalidraw-alice"]
        self.assertEqual(variables["RELEASE_REASON"]["value"], "gave_up")
        self.assertEqual(projects.moment(variables["RELEASED_AT"]["value"]), CONTEST_TIME)
        claim(api, "excalidraw", BOB)
        self.assertIn("org/app-excalidraw-bob", api.repos)
        with self.assertRaises(enroll.Refusal):
            claim(api, "excalidraw")

    def test_only_the_owner_can_give_a_project_up(self):
        api = FakeGitHub()
        api.users["bar-baz"] = {"login": "bar-baz", "id": 5, "type": "User"}
        claim(api, "excalidraw")
        claim(api, "foo", api.users["bar-baz"])
        before = state(api)
        new_alice = {"login": "alice", "id": 99, "type": "User"}
        for opener, app in [(MALLORY, "excalidraw"), (new_alice, "excalidraw"), ({"login": "baz", "id": 6, "type": "User"}, "foo-bar")]:
            with self.subTest(opener=opener["login"], id=opener["id"]), self.assertRaises(enroll.Refusal):
                enroll.give_up(api, ORG, opener, app, CONTEST_TIME)
        self.assertEqual(state(api), before)

    def test_giving_up_twice_changes_nothing(self):
        api = FakeGitHub()
        claim(api, "excalidraw")
        enroll.give_up(api, ORG, ALICE, "excalidraw", CONTEST_TIME)
        before = state(api)
        later = datetime(2026, 12, 3, tzinfo=BEIJING)
        self.assertEqual(enroll.give_up(api, ORG, ALICE, "excalidraw", later), ("https://github.com/org/app-excalidraw-alice", False))
        self.assertEqual(state(api), before)

    def test_give_up_that_stopped_before_archiving_finishes_on_retry(self):
        api = FakeGitHub()
        claim(api, "excalidraw")
        patch = api.patch

        def patch_without_archiving(path, body):
            if "archived" in body:
                raise ApiError("PATCH", path, 500, "stopped")
            return patch(path, body)

        api.patch = patch_without_archiving
        with self.assertRaises(ApiError):
            enroll.give_up(api, ORG, ALICE, "excalidraw", CONTEST_TIME)
        api.patch = patch
        self.assertEqual(enroll.give_up(api, ORG, ALICE, "excalidraw", CONTEST_TIME)[1], True)
        self.assertTrue(api.repos["org/app-excalidraw-alice"]["archived"])
        self.assertEqual(api.variables["org/app-excalidraw-alice"]["RELEASE_REASON"]["value"], "gave_up")

    def test_contest_entry_cannot_be_given_up_after_the_deadline(self):
        late = CATALOG.contest.closes + timedelta(seconds=1)
        for mode, catalog, app in [("list", CATALOG, "excalidraw"), ("free", EMPTY, "notes")]:
            with self.subTest(mode=mode):
                api = FakeGitHub()
                open_issue(api, form(app), catalog=catalog)
                before = state(api)
                issues = open_issue(api, give_up_form(app), title=enroll.GIVE_UP_TITLE, opened=late, catalog=catalog)
                [reply] = issues.comments[7]
                self.assertIn("正式赛已经截止", reply)
                self.assertTrue(reply.startswith("@alice 放弃没有成功: "))
                self.assertEqual(state(api), before)

    def test_give_ups_the_freeze_does_not_need_are_accepted_after_the_deadline(self):
        closes = CATALOG.contest.closes
        late = closes + timedelta(seconds=1)
        cases = [("contest project in the deadline second", CATALOG, "excalidraw", CONTEST_TIME, closes),
                 ("warm-up project", CATALOG, "hello", WARMUP_TIME, late),
                 ("free repository made after the deadline", EMPTY, "notes", late, late)]
        for case, catalog, app, made, given_up in cases:
            with self.subTest(case=case):
                api = FakeGitHub()
                api.now = made
                open_issue(api, form(app), opened=made, catalog=catalog)
                issues = open_issue(api, give_up_form(app), title=enroll.GIVE_UP_TITLE, opened=given_up, catalog=catalog)
                self.assertEqual(issues.issue["state_reason"], "completed")
                self.assertTrue(api.repos[f"org/app-{app}-alice"]["archived"])


class IssueTest(unittest.TestCase):
    def test_free_name_is_accepted_while_the_list_is_empty(self):
        api = FakeGitHub()
        issues = open_issue(api, form("notes"))
        self.assertEqual(issues.issue["state_reason"], "completed")
        self.assertTrue(issues.comments[7][0].startswith("@alice 你的仓库已就绪: https://github.com/org/app-notes-alice"))
        self.assertEqual(api.variables["org/app-notes-alice"]["CLAIM_ISSUE"]["value"], "7")

    def test_claim_before_its_window_is_refused_on_the_issue_with_the_window(self):
        api = FakeGitHub()
        issues = open_issue(api, form("excalidraw"), opened=WARMUP_TIME, catalog=CATALOG)
        [reply] = issues.comments[7]
        self.assertTrue(reply.startswith("@alice 没有创建仓库: "))
        self.assertIn("2026-12-01 00:00 到 2026-12-07 23:59 (北京时间)", reply)
        self.assertEqual(issues.issue["state_reason"], "not_planned")
        self.assertNotIn("org/app-excalidraw-alice", api.repos)

    def test_give_up_issue_archives_the_repository_and_replies(self):
        api = FakeGitHub()
        open_issue(api, form("notes"))
        issues = open_issue(api, give_up_form("notes"), title=enroll.GIVE_UP_TITLE)
        self.assertTrue(api.repos["org/app-notes-alice"]["archived"])
        self.assertEqual(issues.issue["state_reason"], "completed")
        self.assertIn("https://github.com/org/app-notes-alice", issues.comments[7][0])

    def test_give_up_by_someone_else_is_refused_on_the_issue(self):
        api = FakeGitHub()
        open_issue(api, form("notes"))
        issues = open_issue(api, give_up_form("notes"), title=enroll.GIVE_UP_TITLE, user=MALLORY)
        self.assertTrue(issues.comments[7][0].startswith("@mallory 放弃没有成功: "))
        self.assertEqual(issues.issue["state_reason"], "not_planned")
        self.assertFalse(api.repos["org/app-notes-alice"]["archived"])

    def test_issues_with_other_titles_are_ignored(self):
        api = FakeGitHub()
        open_issue(api, form("notes"))
        before = state(api)
        for title, body in [("提名项目: excalidraw", form("todo")), ("提名项目", give_up_form("notes")),
                            ("领取仓库 2", form("todo")), ("放弃", give_up_form("notes"))]:
            with self.subTest(title=title):
                issues = open_issue(api, body, title=title)
                self.assertEqual((issues.comments, issues.issue["state"]), ({}, "open"))
                self.assertEqual(state(api), before)

    def test_issue_forms_carry_the_titles_and_the_field_the_bot_reads(self):
        forms = Path(__file__).resolve().parents[1] / ".github" / "ISSUE_TEMPLATE"
        for name, title in [("enroll.yml", enroll.TITLE), ("give-up.yml", enroll.GIVE_UP_TITLE)]:
            with self.subTest(form=name):
                text = (forms / name).read_text(encoding="utf-8")
                self.assertIn(f"\ntitle: {title}\n", text)
                self.assertIn(f"\n      label: {enroll.APP_LABEL}\n", text)
        nomination = re.search(r'^title: "(.*)"$', (forms / "nominate.yml").read_text(encoding="utf-8"), re.M)[1]
        self.assertNotIn(nomination.strip(), enroll.ROUTES)

    def test_contest_claim_reply_explains_giving_up_and_idle_release(self):
        cases = [("contest project", "excalidraw", CONTEST_TIME, CATALOG, True),
                 ("warm-up project", "hello", WARMUP_TIME, CATALOG, False),
                 ("free name", "notes", CONTEST_TIME, EMPTY, False)]
        for case, app, opened, catalog, explained in cases:
            with self.subTest(case=case):
                reply = open_issue(FakeGitHub(), form(app), opened=opened, catalog=catalog, IDLE_DAYS="2").comments[7][0]
                self.assertEqual("「放弃项目」" in reply and "领取后 2 天内一直没有 push" in reply, explained)

    def test_max_repos_is_read_from_the_environment(self):
        api = FakeGitHub()
        open_issue(api, form("excalidraw"), catalog=CATALOG, MAX_REPOS="1")
        self.assertEqual(open_issue(api, form("obsidian"), catalog=CATALOG, MAX_REPOS="1").issue["state_reason"], "not_planned")
        self.assertNotIn("org/app-obsidian-alice", api.repos)
        self.assertEqual(open_issue(api, form("obsidian"), catalog=CATALOG, MAX_REPOS="").issue["state_reason"], "completed")
        self.assertIn("org/app-obsidian-alice", api.repos)

    def test_warm_up_claims_stay_free_while_the_list_names_only_contest_projects(self):
        catalog = projects.parse(listing(entry("excalidraw"), entry("obsidian")))
        api = FakeGitHub()
        for app in ("my-notes", "excalidraw"):
            with self.subTest(app=app):
                issues = open_issue(api, form(app), opened=WARMUP_TIME, catalog=catalog, MAX_REPOS="lots", IDLE_DAYS="2")
                self.assertEqual(issues.issue["state_reason"], "completed")
                self.assertNotIn("PROJECT", api.variables[f"org/app-{app}-alice"])
                self.assertNotIn("只归你", issues.comments[7][0])
        gap = catalog.warmup.closes + timedelta(hours=1)
        self.assertEqual(open_issue(api, form("notes"), opened=gap, catalog=catalog).issue["state_reason"], "not_planned")
        open_issue(api, form("excalidraw"), user=BOB, catalog=catalog)
        self.assertEqual(api.variables["org/app-excalidraw-bob"]["PROJECT"]["value"], "excalidraw")

    def test_free_mode_claims_ignore_max_repos(self):
        api = FakeGitHub()
        self.assertEqual(open_issue(api, form("notes"), MAX_REPOS="lots").issue["state_reason"], "completed")

    def test_list_mode_claims_fail_while_max_repos_is_not_a_positive_whole_number(self):
        for value in ("0", "-1", "1.5", "lots"):
            with self.subTest(MAX_REPOS=value):
                api = FakeGitHub()
                before = state(api)
                issues = FakeIssues()
                with self.assertRaises(ValueError):
                    open_issue(api, form("excalidraw"), catalog=CATALOG, issues=issues, MAX_REPOS=value)
                [reply] = issues.comments[7]
                self.assertIn("处理时出错", reply)
                self.assertEqual(issues.issue["state"], "open")
                self.assertEqual(state(api), before)

    def test_claim_reply_promises_idle_release_only_for_idle_days_that_parse(self):
        cases = {"0.5": "0.5", "1e1": "10", "0": None, "-2": None, "inf": None, "nan": None, "two": None, "": None}
        for value, days in cases.items():
            with self.subTest(IDLE_DAYS=value):
                issues = open_issue(FakeGitHub(), form("excalidraw"), catalog=CATALOG, IDLE_DAYS=value)
                [reply] = issues.comments[7]
                self.assertEqual(issues.issue["state_reason"], "completed")
                self.assertIn("「放弃项目」", reply)
                self.assertEqual(re.findall(r"领取后 (\S+) 天内一直没有 push", reply), [days] if days else [])


if __name__ == "__main__":
    unittest.main()
