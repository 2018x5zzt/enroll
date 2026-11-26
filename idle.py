"""Release the contest claims whose player has not pushed at all in the IDLE_DAYS days after claiming."""

import os
import sys
from datetime import datetime, timedelta, timezone

import projects
from enroll import PREFIX, active_repos, candidates, release
from github import ApiError, GitHub


def pushed(api, full, player, since):
    """Whether the player has pushed to any branch at or after `since`, by GitHub's own record of the repository's ref updates."""
    activity = api.get(f"repos/{full}/activity?direction=desc&per_page=100")
    recent = [entry for entry in activity if projects.moment(entry["timestamp"]) >= since]
    # Only the newest page is read, so a full page of recent activity may hide the player's push.
    return len(recent) >= 100 or any(str((entry.get("actor") or {}).get("id")) == player for entry in recent)


def release_idle(api, issues, hub, org, catalog, now, days, prefix=PREFIX):
    """Archive the contest claims recorded `days` days ago or more with no push by their player since; return their URLs."""
    contest = {project.id for project in catalog.projects.values() if not project.warmup}
    if days is None or not contest or now not in catalog.contest:
        return []
    due = timedelta(days=days)
    released = []
    broken = []
    # A claim is recorded after its repository is created, so a repository younger than `days` holds no due claim.
    for repo, values in candidates(api, active_repos(api, org), prefix, contest,
                                   lambda repo: projects.moment(repo["created_at"]) + due <= now):
        if values.get("PROJECT") not in contest:
            continue
        try:
            claimed_at = projects.moment(values["CLAIMED_AT"])
        except (KeyError, ValueError) as error:
            # A maintainer edits CLAIMED_AT by hand on a restore; one mistake must not hold back the other claims.
            print(f"Skipped {repo['html_url']}: CLAIMED_AT {values.get('CLAIMED_AT')!r} is not a time with a UTC offset ({error!r})")
            broken.append(repo["html_url"])
            continue
        if claimed_at + due > now or pushed(api, repo["full_name"], values["STUDENT_ID"], claimed_at):
            continue
        release(api, repo["full_name"], "idle", now)
        login = values.get("STUDENT_LOGIN")
        print(f"Released {repo['html_url']} of {login}: no push in {days:g} days")
        released.append(repo["html_url"])
        if values.get("CLAIM_ISSUE"):
            try:
                issues.post(f"repos/{hub}/issues/{values['CLAIM_ISSUE']}/comments", {"body": (
                    f"@{login} 你领的正式赛项目 `{values['PROJECT']}` 领取后 {days:g} 天内没有任何 push, 已经收回: "
                    f"仓库 {repo['html_url']} 已归档, 保持公开, 不能再推送. 项目回到了列表, 别人可以领. "
                    "想接着参加, 请换一个项目重新领; 有疑问请联系维护者.\n")})
            except ApiError as error:
                print(f"Could not tell {login} on issue #{values['CLAIM_ISSUE']}: {error}")
    if broken:
        raise ValueError("Set CLAIMED_AT like 2026-12-03T10:00:00+08:00 on these claims, which were not checked: " + " ".join(broken))
    return released


def main():
    days = projects.idle_days(os.environ.get("IDLE_DAYS"))
    released = release_idle(GitHub(os.environ.get("GH_TOKEN")), GitHub(os.environ.get("ISSUE_TOKEN")),
                            os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REPOSITORY_OWNER"], projects.load(),
                            datetime.now(timezone.utc), days, os.environ.get("PREFIX") or PREFIX)
    print(f"Released {len(released)} idle claims.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
