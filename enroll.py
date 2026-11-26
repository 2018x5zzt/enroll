"""Create an applicant's repository from the template when they open an enrollment issue, and archive it when they give it up."""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import projects
from github import ApiError, GitHub, eventually
from projects import APP_NAME, APP_NAME_MAX

TITLE = "领取仓库"
GIVE_UP_TITLE = "放弃项目"
TEMPLATE = "app-template"
PREFIX = "app-"
RULESET = "history"
APP_LABEL = "应用名"
WRITE = {"write", "maintain", "admin"}


class Refusal(Exception):
    """The application cannot be accepted; the message is shown to the applicant."""


def app_name(body):
    """Read the app name field of the issue form."""
    match = re.search(rf"^###\s*{APP_LABEL}\s*$(.*?)(?=^###|\Z)", body or "", re.M | re.S)
    value = match[1].strip().lower() if match else ""
    if not APP_NAME.fullmatch(value) or len(value) > APP_NAME_MAX:
        raise Refusal(f"应用名要是 1 到 {APP_NAME_MAX} 个小写字母、数字或 `-` (不以 `-` 开头或结尾, 不连用), 比如 `markdown-editor`. "
                      "请从领取页面重新开一个 issue.")
    return value


def find_project(catalog, app, now):
    """The listed project the claim takes, or None for a free claim: one made inside the window of a phase the list names
    no projects for, so that contest projects can be published while the warm-up is still claimed freely."""
    for warmup, window in ((True, catalog.warmup), (False, catalog.contest)):
        if now in window and not catalog.lists(warmup):
            return None
    project = catalog.projects.get(app)
    if project is None:
        raise Refusal(f"`{app}` 不在项目列表 (本仓库的 `PROJECTS.md`) 里, 应用名要填列表里的项目 id. 请从领取页面重新开一个 issue.")
    window = catalog.window(project)
    if now not in window:
        raise Refusal(f"`{app}` 是{'热身赛' if project.warmup else '正式赛'}项目, 只能在 {window} 领.")
    return project


def variables(api, full):
    found = api.get(f"repos/{full}/actions/variables?per_page=30")
    return {variable["name"]: variable["value"] for variable in found["variables"]}


def active_repos(api, org):
    """The organization's unarchived repositories. The list returns 100 per request with names, so it indexes
    the claims; variables cost a request per repository and only confirm the few candidates."""
    return [repo for repo in api.pages(f"orgs/{org}/repos?type=all") if not repo["archived"]]


def candidates(api, repos, prefix, ids, keep=lambda repo: True):
    """Yield the repositories whose names could claim one of the projects, with their variables."""
    starts = tuple(f"{prefix}{project}-".lower() for project in ids)
    for repo in repos:
        if repo["name"].lower().startswith(starts) and keep(repo):
            yield repo, variables(api, repo["full_name"])


def check_available(api, org, prefix, catalog, student, project, max_repos):
    """Refuse a contest claim whose project has a holder or that would pass MAX_REPOS; the claimed repository does not hold it yet."""
    repos = active_repos(api, org)
    for repo, values in candidates(api, repos, prefix, [project.id]):
        if values.get("PROJECT") != project.id:
            continue
        if values.get("STUDENT_ID") != str(student["id"]):
            raise Refusal(f"正式赛项目 `{project.id}` 现在归别人 ({repo['html_url']}), 同一时间一个项目只归一个人. "
                          "他放弃或者项目被收回以后, 项目会回到列表. 请换一个项目, 从领取页面重新开一个 issue.")
        raise Refusal(f"你已经领着正式赛项目 `{project.id}`: {repo['html_url']} (你改 GitHub 用户名之前领的), 同一个项目不能领两次. "
                      "请接着在这个仓库里做; 有疑问请联系维护者.")
    if max_repos is None:
        return
    contest = {listed.id for listed in catalog.projects.values() if not listed.warmup}
    suffix = "-" + student["login"].lower()
    mine = [repo["html_url"] for repo, values in candidates(api, repos, prefix, contest, lambda repo: repo["name"].lower().endswith(suffix))
            if values.get("STUDENT_ID") == str(student["id"]) and values.get("PROJECT") in contest]
    if len(mine) >= max_repos:
        raise Refusal(f"每人同一时间最多做 {max_repos} 个正式赛项目, 你在做: {' '.join(mine)}. "
                      "要换项目, 先开「放弃项目」issue 交还一个, 再从领取页面重新开一个 issue.")


def check_template(api, template):
    source = api.get("repos/" + template)
    if not source.get("is_template") or source.get("private") or source.get("archived"):
        raise RuntimeError(template + " must be a public, unarchived template repository.")


def create(api, org, template, name, description, sleep):
    full = f"{org}/{name}"
    try:
        api.post(f"repos/{template}/generate", {
            "owner": org, "name": name, "description": description,
            "private": False, "include_all_branches": False,
        })
    except ApiError as error:
        if error.status != 422:
            raise
        print(f"Generate returned 422, checking whether {full} already exists", flush=True)
    repo = eventually(lambda: api.get("repos/" + full), sleep)
    eventually(lambda: api.get(f"repos/{full}/branches/{repo['default_branch']}"), sleep)
    return repo


def outsiders(api, full):
    collaborators = list(api.pages(f"repos/{full}/collaborators?affiliation=outside"))
    invitations = list(api.pages(f"repos/{full}/invitations"))
    return collaborators or invitations


def set_variable(api, full, name, value):
    path = f"repos/{full}/actions/variables/{name}"
    if api.get(path, missing_ok=True) is None:
        api.post(f"repos/{full}/actions/variables", {"name": name, "value": value})
    else:
        api.patch(path, {"name": name, "value": value})


def bind(api, full, student, sleep):
    path = f"repos/{full}/actions/variables/STUDENT_ID"
    expected = str(student["id"])
    variable = api.get(path, missing_ok=True)
    if variable is None:
        if outsiders(api, full):
            raise RuntimeError(f"{full} has outside access but no STUDENT_ID; left for a maintainer.")
        try:
            api.post(f"repos/{full}/actions/variables", {"name": "STUDENT_ID", "value": expected})
        except ApiError as error:
            if error.status not in (409, 422):
                raise
        variable = eventually(lambda: api.get(path), sleep)
    if variable["value"] != expected:
        raise Refusal(f"仓库 {full} 已属于另一个 GitHub 账号. 请换一个应用名; 如果它是你改用户名之前领的, 请联系维护者.")
    set_variable(api, full, "STUDENT_LOGIN", student["login"])


def protect(api, full, branch):
    if any(ruleset["name"] == RULESET for ruleset in api.get(f"repos/{full}/rulesets")):
        return
    api.post(f"repos/{full}/rulesets", {
        "name": RULESET,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/" + branch], "exclude": []}},
        "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
    })


def grant(api, full, student):
    permission = api.get(f"repos/{full}/collaborators/{student['login']}/permission")
    if permission["user"]["id"] != student["id"]:
        raise RuntimeError("The login now names a different GitHub user; access was not changed.")
    if permission["permission"] in WRITE:
        return "write"
    for invitation in api.pages(f"repos/{full}/invitations"):
        if (invitation.get("invitee") or {}).get("id") != student["id"]:
            continue
        if not invitation.get("expired") and invitation["permissions"] in WRITE:
            return "invited"
        api.delete(f"repos/{full}/invitations/{invitation['id']}")
    result = api.put(f"repos/{full}/collaborators/{student['login']}", {"permission": "push"})
    return "invited" if result else "write"


def enroll(api, org, applicant, app, template=TEMPLATE, prefix=PREFIX, sleep=time.sleep,
           catalog=None, now=None, max_repos=None, issue=None, clock=lambda: datetime.now(timezone.utc)):
    project = None if catalog is None else find_project(catalog, app, now)
    student = api.get("users/" + applicant["login"], missing_ok=True)
    if student is None or student["id"] != applicant["id"]:
        raise Refusal("提交 issue 之后 GitHub 用户名变了. 请用现在的账号重新开一个 issue.")
    if student["type"] != "User":
        raise Refusal("只接受个人 GitHub 账号.")

    template = f"{org}/{template}"
    name = f"{prefix}{app}-{student['login']}"
    full = f"{org}/{name}"
    repo = api.get("repos/" + full, missing_ok=True)
    recorded = project is not None and repo is not None and variables(api, full).get("PROJECT") == project.id
    if project is not None and not project.warmup and not recorded:
        check_available(api, org, prefix, catalog, student, project, max_repos)
    if repo is None:
        check_template(api, template)
        repo = create(api, org, template, name, "@" + student["login"], sleep)
    if repo["full_name"].lower() != full.lower():
        raise RuntimeError(f"{full} redirects to {repo['full_name']}; left for a maintainer.")
    if (repo.get("template_repository") or {}).get("full_name", "").lower() != template.lower():
        raise RuntimeError(f"{full} exists but was not generated from {template}; left for a maintainer.")
    if repo["archived"]:
        raise Refusal(f"仓库 {repo['html_url']} 已归档 (放弃了, 领取后一直没有 push 被收回, 或者正式赛截止后冻结了), 不能再领. 请换一个应用名; 要恢复这个仓库请联系维护者.")

    bind(api, full, student, sleep)
    if project is not None and not recorded:
        # PROJECT marks the claim as recorded, so it goes last: a run that stops before it is checked again on retry.
        set_variable(api, full, "CLAIMED_AT", clock().isoformat(timespec="seconds"))
        set_variable(api, full, "PROJECT", project.id)
    set_variable(api, full, "APP_NAME", app)
    if issue is not None:
        set_variable(api, full, "CLAIM_ISSUE", str(issue))
    protect(api, full, repo["default_branch"])
    return repo["html_url"], grant(api, full, student)


def release(api, full, reason, now):
    """Return a claim to the list: record when and why, then archive the repository, which stays public."""
    set_variable(api, full, "RELEASED_AT", now.isoformat())
    set_variable(api, full, "RELEASE_REASON", reason)
    api.patch("repos/" + full, {"archived": True})


def entered(catalog, repo, values):
    """Whether the repository is a contest entry, which freeze.py freezes at the deadline: in list mode its PROJECT is a
    contest project; in free mode it was made by the time the contest closed."""
    if catalog.projects:
        project = catalog.projects.get(values.get("PROJECT"))
        return project is not None and not project.warmup
    return projects.moment(repo["created_at"]) <= catalog.contest.closes


def give_up(api, org, opener, app, now, prefix=PREFIX, catalog=None):
    """Release the opener's claim on the app; return its URL and whether this run archived it. With the catalog, a contest
    entry is not released once the contest has closed, so that freeze.py freezes it."""
    repo = api.get(f"repos/{org}/{prefix}{app}-{opener['login']}", missing_ok=True)
    values = {} if repo is None else variables(api, repo["full_name"])
    if values.get("STUDENT_ID") != str(opener["id"]):
        raise Refusal(f"你没有领过 `{app}`: 只有领取人自己能放弃, 应用名要填领取时填的那个. 领取后改过 GitHub 用户名的, 请联系维护者.")
    if repo["archived"]:
        return repo["html_url"], False
    if catalog is not None and now > catalog.contest.closes and entered(catalog, repo, values):
        raise Refusal(f"正式赛已经截止, 正式赛的仓库不能再放弃. 验收用截止时 `{repo['default_branch']}` 上的提交, 机器人会把仓库归档. 有疑问请联系维护者.")
    release(api, repo["full_name"], "gave_up", now)
    return repo["html_url"], True


def success_message(login, url, access, note=None):
    steps = [] if access == "write" else [f"接受邀请: {url}/invitations (7 天内有效; 过期了重新开一个 issue 即可)"]
    steps += [
        f"克隆: `git clone {url}.git`",
        "开发, 提交, push. 只有你能推送这个仓库; `main` 不能 force push, 也不能删除.",
        "每次 push 会自动跑 `acdev check` 和 `acdev build`, 结果在仓库的 Actions 页面. 作品由审核员人工审核.",
    ]
    if note:
        steps.append(note)
    lines = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
    return f"@{login} 你的仓库已就绪: {url}\n\n{lines}\n"


def opened_at(issue):
    return projects.moment(issue["created_at"])


def load_issue(hub, issues):
    number = os.environ.get("ISSUE_NUMBER")
    if number:
        return issues.get(f"repos/{hub}/issues/{int(number)}")
    with open(os.environ["GITHUB_EVENT_PATH"]) as event:
        return json.load(event)["issue"]


def claim_issue(api, org, issue):
    app = app_name(issue.get("body"))
    login = issue["user"]["login"]
    catalog = projects.load()
    listed = catalog if catalog.projects else None
    now = opened_at(issue)
    project = None if listed is None else find_project(listed, app, now)
    contest = project is not None and not project.warmup
    limit = projects.max_repos(os.environ.get("MAX_REPOS")) if contest else None
    url, access = enroll(api, org, issue["user"], app, os.environ.get("TEMPLATE") or TEMPLATE, os.environ.get("PREFIX") or PREFIX,
                         catalog=listed, now=now, max_repos=limit, issue=issue["number"])
    print(f"Enrolled {login}: {url} ({access})")
    note = None
    if contest:
        try:
            days = projects.idle_days(os.environ.get("IDLE_DAYS"))
        except ValueError as error:
            print(f"{error} The reply leaves idle release out.")
            days = None
        note = "这个正式赛项目现在只归你. 不想做了, 开「放弃项目」issue 交还" + (f"; 领取后 {days:g} 天内一直没有 push, 项目会被收回." if days else ".")
    return success_message(login, url, access, note)


def give_up_issue(api, org, issue):
    app = app_name(issue.get("body"))
    login = issue["user"]["login"]
    url, released = give_up(api, org, issue["user"], app, opened_at(issue), os.environ.get("PREFIX") or PREFIX, catalog=projects.load())
    print(f"{login} gave up {url}" if released else f"{url} was archived already")
    if not released:
        return f"@{login} 仓库 {url} 已经归档了, 不用再放弃.\n"
    return f"@{login} 已放弃 `{app}`: 仓库 {url} 已归档, 保持公开, 谁都不能再推送. 想做别的, 从领取页面重新开一个 issue.\n"


ROUTES = {TITLE: (claim_issue, "没有创建仓库"), GIVE_UP_TITLE: (give_up_issue, "放弃没有成功")}


def main():
    hub = os.environ["GITHUB_REPOSITORY"]
    org = os.environ["GITHUB_REPOSITORY_OWNER"]
    api = GitHub(os.environ.get("GH_TOKEN"))
    issues = GitHub(os.environ.get("ISSUE_TOKEN"))
    issue = load_issue(hub, issues)
    if issue.get("pull_request"):
        return 0
    path = f"repos/{hub}/issues/{issue['number']}"
    login = issue["user"]["login"]
    route = ROUTES.get(issue["title"].strip())
    if route is None:
        print("Not an enrollment issue; ignored.")
        return 0
    handle, failure = route
    try:
        reply = handle(api, org, issue)
    except Refusal as refusal:
        issues.post(path + "/comments", {"body": f"@{login} {failure}: {refusal}"})
        issues.patch(path, {"state": "closed", "state_reason": "not_planned"})
        print("Refused: " + str(refusal))
        return 0
    except Exception:
        issues.post(path + "/comments", {"body": f"@{login} 处理时出错, 维护者会查看并重试, 请先不要重复提交."})
        raise
    issues.post(path + "/comments", {"body": reply})
    issues.patch(path, {"state": "closed", "state_reason": "completed"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
