"""Freeze the contest entries once the contest closes: record the commit each one's default branch held at the deadline,
by GitHub's own record of the repository's ref updates, archive the repository and tell the player. A dry run rehearses
this for the contest or the warm-up and changes nothing."""

import os
import sys
from datetime import datetime, timezone

import projects
from enroll import PREFIX, active_repos, candidates, entered, set_variable, variables
from github import ApiError, GitHub


class Unfrozen(Exception):
    """The entry cannot be frozen; the message says why, for a maintainer."""


def entries(api, org, prefix, catalog, warmup):
    """The active entries of the contest, or of the warm-up, that are bound to a player, with their variables."""
    repos = active_repos(api, org)
    if catalog.lists(warmup):
        found = candidates(api, repos, prefix, [project.id for project in catalog.projects.values() if project.warmup == warmup])
    else:
        found = ((repo, variables(api, repo["full_name"])) for repo in repos if repo["name"].lower().startswith(prefix.lower()))
    return [(repo, values) for repo, values in found if "STUDENT_ID" in values and entered(catalog, repo, values, warmup)]


def last_update(api, repo, player, start, cutoff):
    """The newest update of the default branch at or before the cutoff, whoever made it, and whether the player made any
    update of that branch from `start` to the cutoff, by GitHub's record of the repository's ref updates."""
    ref = "refs/heads/" + repo["default_branch"]
    newest = None
    for update in api.cursor_pages(f"repos/{repo['full_name']}/activity?ref={ref}&direction=desc"):
        when = projects.moment(update["timestamp"])
        if update["ref"] != ref or when > cutoff:
            continue
        newest = newest or update
        if when < start:
            break
        if str((update.get("actor") or {}).get("id")) == player:
            return newest, True
    return newest, False


def record_of(api, catalog, repo, values, cutoff, warmup=False):
    """The entry's freeze variables in the order they are written: as recorded, or else worked out at the cutoff. The
    player's own updates count from the claim when the phase was claimed from the list, else from the repository's making."""
    if "FROZEN_SHA" in values:
        try:
            return {name: values[name] for name in ("FROZEN_PUSHED_AT", "SUBMITTED", "FROZEN_SHA")}
        except KeyError as missing:
            raise Unfrozen(f"FROZEN_SHA is set but {missing} is not; delete all three to freeze it again") from None
    try:
        start = projects.moment(values["CLAIMED_AT"] if catalog.lists(warmup) else repo["created_at"])
    except (KeyError, ValueError) as error:
        raise Unfrozen(f"CLAIMED_AT {values.get('CLAIMED_AT')!r} is not a time with a UTC offset ({error!r})") from None
    update, submitted = last_update(api, repo, values["STUDENT_ID"], start, cutoff)
    if update is None:
        raise Unfrozen(f"GitHub records no update of {repo['default_branch']} by {cutoff.isoformat()}")
    return {"FROZEN_PUSHED_AT": update["timestamp"], "SUBMITTED": "true" if submitted else "false", "FROZEN_SHA": update["after"]}


def notice(login, repo, record):
    branch = repo["default_branch"]
    missing = "" if record["SUBMITTED"] == "true" else f"领取以后到截止, 没有记录到你往 `{branch}` 的任何 push. "
    return (f"@{login} 正式赛已经截止. {missing}验收用截止时 `{branch}` 上的这个提交: {repo['html_url']}/commit/{record['FROZEN_SHA']}\n"
            f"仓库 {repo['html_url']} 已归档, 保持公开, 不能再推送. 有疑问请联系维护者.\n")


def freeze(api, issues, hub, org, catalog, now, prefix=PREFIX, rehearse=None, summary=None):
    """Once the contest has closed, record each active contest entry's default branch at the deadline, archive the
    repository and tell its player; return the URLs. With `rehearse` set to "contest" or "warmup", do none of that: cut
    off at that phase's deadline, or now if that comes first, and only print what would be recorded. Either way, print a
    line per entry and append a table to the `summary` file. Entries that cannot be frozen are left as they are, and the
    run fails after the others."""
    dry_run = rehearse is not None
    warmup = rehearse == "warmup"
    closes = (catalog.warmup if warmup else catalog.contest).closes
    if not dry_run and now <= closes:
        print(f"Nothing is frozen before the contest closes at {closes.isoformat()}.")
        return []
    cutoff = min(closes, now)
    frozen = []
    table = []
    skipped = []
    for repo, values in entries(api, org, prefix, catalog, warmup):
        url = repo["html_url"]
        try:
            record = record_of(api, catalog, repo, values, cutoff, warmup)
        except Unfrozen as reason:
            print(f"Skipped {url}: {reason}")
            skipped.append(url)
            continue
        if not dry_run:
            if "FROZEN_SHA" not in values:
                # FROZEN_SHA marks the freeze as recorded, so it goes last: a run that stops before it works the entry out again.
                for name, value in record.items():
                    set_variable(api, repo["full_name"], name, value)
            api.patch("repos/" + repo["full_name"], {"archived": True})
        login = values.get("STUDENT_LOGIN")
        name = values.get("PROJECT") or values.get("APP_NAME")
        sha, pushed_at, submitted = record["FROZEN_SHA"], record["FROZEN_PUSHED_AT"], record["SUBMITTED"]
        print(f"{'Would freeze' if dry_run else 'Froze'} {url} of {login} ({name}) at {sha}, pushed {pushed_at}, submitted: {submitted}")
        frozen.append(url)
        table.append(f"| [{repo['name']}]({url}) | {login} | {name} | [{sha}]({url}/commit/{sha}) | {pushed_at} | {submitted} |\n")
        if not dry_run and values.get("CLAIM_ISSUE"):
            try:
                issues.post(f"repos/{hub}/issues/{values['CLAIM_ISSUE']}/comments", {"body": notice(login, repo, record)})
            except ApiError as error:
                print(f"Could not tell {login} on issue #{values['CLAIM_ISSUE']}: {error}")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write(f"{'Dry run, nothing changed' if dry_run else 'Frozen'}: cut off at {cutoff.isoformat()}.\n\n"
                       "| Repository | Player | Project | Commit | Pushed at | Submitted |\n|-|-|-|-|-|-|\n")
            file.writelines(table)
            file.writelines(f"\nNot frozen: {url}\n" for url in skipped)
    if skipped:
        raise ValueError("These entries were not frozen; the log above says why: " + " ".join(skipped))
    return frozen


def main():
    scheduled = os.environ["GITHUB_EVENT_NAME"] == "schedule"
    dry_run = not scheduled and os.environ.get("DRY_RUN") != "false"
    window = "contest" if scheduled else os.environ.get("WINDOW") or "contest"
    if window == "warmup" and not dry_run:
        raise ValueError("Only the contest is frozen for real; rehearse the warm-up with dry_run on.")
    frozen = freeze(GitHub(os.environ.get("GH_TOKEN")), GitHub(os.environ.get("ISSUE_TOKEN")),
                    os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REPOSITORY_OWNER"], projects.load(),
                    datetime.now(timezone.utc), os.environ.get("PREFIX") or PREFIX, window if dry_run else None,
                    os.environ.get("GITHUB_STEP_SUMMARY"))
    print(f"{'Would freeze' if dry_run else 'Froze'} {len(frozen)} entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
