# enroll

开发者在本仓库开 issue 领取仓库, 一个应用一个: 机器人从模板仓库 `app-template` 生成 `app-<应用名>-<GitHub 用户名>` (公开), 把它绑定到申请人的数字 GitHub ID, 保护默认分支, 再邀请申请人以 write 权限加入. 每个仓库只有领取人能推, 互相推不到对方的仓库. 作品人工审核, 这里没有自动评分.
项目列表 `projects.json` 里有项目时, 应用名只能填列表里的项目 id, 正式赛项目同一时间只归一个人, 见「项目列表」.
正式赛截止以后, 机器人记下每个参赛仓库截止时 `main` 上的提交, 再把仓库归档; 验收只用这个提交, 见「截止」.

领取链接: `https://github.com/<组织>/enroll/issues/new/choose`.

## 文件

- `.github/ISSUE_TEMPLATE/enroll.yml`: 领取表单, 标题固定为 `领取仓库`; 必填的「应用名」由 `enroll.py` 的 `app_name` 从 issue 正文的 `### 应用名` 一节读出, 转小写,
  只许小写字母、数字和单个 `-` 连接, 最多 40 个字符 (规则是 `projects.py` 的 `APP_NAME`). 改表单里这一项的 label 要同时改 `APP_LABEL`.
- `.github/ISSUE_TEMPLATE/give-up.yml`: 放弃表单, 标题固定为 `放弃项目`, 也用「应用名」这一项.
- `.github/ISSUE_TEMPLATE/nominate.yml`: 提名表单, 标题默认 `提名项目: `, 机器人不理.
  机器人只处理标题正好是 `领取仓库` 或 `放弃项目` 的 issue, 别的都不理; 表单的标题和 label 是否和代码一致、workflow 是否放进这两种标题, 由测试检查.
  领取表单和提名表单里 `PROJECTS.md` 的链接写死了组织 `0x00-Migration`, 换组织要一起改.
- `.github/workflows/enroll.yml`: 标题含 `领取仓库` 或 `放弃项目` 的 issue 打开时运行 `enroll.py` (机器人再按去掉首尾空格的完整标题分辨); 手动运行可填 issue 号重试.
  这些 job 排成一队, 一次只跑一个 (job 级 concurrency 组 `enroll`), 两个人不会同时领到同一个项目. 提名和别的 issue 不跑机器人.
- `.github/workflows/idle.yml` (`Release idle claims`): 每小时运行一次 `idle.py`, 也可以手动运行; 和领取在同一个 job 级 concurrency 组里排队.
- `.github/workflows/freeze.yml` (`Freeze at the deadline`): 每小时第 47 分钟运行一次 `freeze.py`; 手动运行默认只演练, 见「截止」. 也和领取在同一个 job 级 concurrency 组里排队.
- `enroll.py`: 领取和放弃, 可重复运行 (中途失败重跑会接着做, 不会重复建仓库、重复邀请或重复归档).
- `idle.py`: 收回长时间没有 push 的正式赛项目, 见「项目列表」.
- `freeze.py`: 正式赛截止以后冻结参赛仓库, 见「截止」.
- `projects.json`: 项目列表和两个领取时间段. `projects.py` 读它 (格式不对就报错), 也用它生成 `PROJECTS.md`.
- `PROJECTS.md`: 给开发者看的项目列表, 由 `python3 projects.py` 生成, 不要手改. 它和 `projects.json` 对不上时测试失败.
- `github.py`: 只用标准库的 REST 客户端 (重试、限流、刚创建的资源暂时 404).
- `tests/`: `python3 -m unittest discover -s tests`, 用内存里的假 GitHub.

## 项目列表

- `projects.json` 的 `warmup` 和 `contest` 是热身赛和正式赛的领取时间段: `opens` 到 `closes`, 两头都算, 写 ISO 8601 并带时区, 比如 `2026-12-01T00:00:00+08:00`.
  `projects` 里每个项目有 `id` (照应用名规则, 就是仓库名里的应用名)、`name`、`upstream` (原项目地址)、`tier` (档位, 现在只是文字)、`features` (核心功能, 字符串列表)、`warmup` (`true` 是热身赛项目).
  一个 id 不能是另一个 id 加 `-` 再加别的 (有 `foo` 就不能有 `foo-bar`), 否则 `app-foo-bar-baz` 既像 baz 领的 `foo-bar`, 又像 bar-baz 领的 `foo`; `projects.py` 遇到这种列表会报错.
- `projects` 为空是自由模式, 和以前一样: 应用名自己起, 不限时间, 不限人数. 有项目就是列表模式.
  列表里没有热身赛项目时, 热身赛时间段里照旧自由领 (只列了热身赛项目时, 正式赛时间段同理). 所以热身赛用自由模式时,
  正式赛项目在热身赛期间就能加进列表、提前公布, 热身赛照旧自由领, 正式赛时间段一到就只能领列表里的. 两个时间段之外只能按列表领. 列表模式:
  - 应用名必须是列表里的 id. 热身赛项目只能在热身赛时间段里领, 正式赛项目只能在正式赛时间段里领. 时间按 issue 的创建时间算, 维护者手动重试也按这个时间.
  - 正式赛项目同一时间只归一个人: 已经有别人领着, 就拒绝. 热身赛项目谁都能领. 重领自己领着的项目总是可以.
    改过 GitHub 用户名的人, 旧用户名的仓库还领着这个项目时, 用新用户名再领会被拒绝, 回复里给出旧仓库, 让他接着在旧仓库里做.
  - 可选变量 `MAX_REPOS`: 一个人同一时间最多领着几个正式赛项目, 填正整数, 不设就不限. 热身赛项目不算. 只在列表模式下读.
    填了别的 (比如 `0`、`1.5`) 时, 列表模式的领取都会失败: issue 里留言「处理时出错」, Actions 日志写明原因, 改好后手动重试这些 issue.
- 领取记录: 按列表领取项目, 检查通过、仓库绑定到领取人以后, 机器人在仓库上先写 `CLAIMED_AT` (领取时间), 再写 `PROJECT` (项目 id).
  「领着」一个项目, 就是有一个没有归档、`PROJECT` 是这个 id 的仓库, 领的人是它的 `STUDENT_ID`. 别人是否领着、`MAX_REPOS`、收回都只看 `PROJECT`, 不看 `APP_NAME`.
  机器人先读组织的仓库列表 (一次请求 100 个, 带名字和是否归档), 按名字 `<PREFIX><id>-` 找候选, 只对候选读变量确认. 领取中途失败、还没写 `PROJECT` 的仓库不占项目: 别人可以先领, 维护者重试时重新检查.
- 自由领取建的仓库 (自由模式下建的, 或者列表里没有热身赛项目时热身赛里建的) 没有 `PROJECT`, 不算领着任何项目, 也不会被收回, 不用归档.
  列表模式下领项目时, 如果 `<PREFIX><id>-<用户名>` 已经是自己在自由模式下建的仓库, 照样先检查 (别人是否领着、`MAX_REPOS`), 通过后在这个仓库上写 `CLAIMED_AT` 和 `PROJECT`, 接着用它.
- 放弃: 开「放弃项目」issue, 填应用名. 机器人找 `<PREFIX><应用名>-<开 issue 的人>`, `STUDENT_ID` 是开 issue 的人才放弃, 别人一律拒绝.
  先写 `RELEASED_AT` (时间) 和 `RELEASE_REASON=gave_up`, 再归档仓库. 归档的仓库只读, 仍然公开, 不再算领着, 正式赛项目就回到列表. 放弃的人不能再领这个项目. 自由模式下也能放弃.
- 收回: 设了变量 `IDLE_DAYS` (正的天数, 可以是小数) 时, `idle.py` 只在正式赛时间段里收回. 正式赛的领取从 `CLAIMED_AT` 起过了 `IDLE_DAYS` 天,
  如果 GitHub 记录的仓库活动 (`GET repos/<仓库>/activity`) 里没有领取人在 `CLAIMED_AT` 以后的任何 push (任何分支, 包括 force push 和新建分支), 就写 `RELEASED_AT` 和 `RELEASE_REASON=idle`, 归档仓库, 再在领取 issue (`CLAIM_ISSUE`) 里 @ 他.
  留言失败 (issue 删了、转走了或锁了) 只在日志里记一行, 接着处理下一个领取.
  生成仓库时机器人的 push、维护者的 push、`CLAIMED_AT` 以前的 push (比如热身赛时推到同一个仓库的) 都不算. 领取以后领取人 push 过一次, 就永远不会因为这条被收回. 热身赛项目和自由模式建的仓库不收回, 正式赛结束后不收回, 不设 `IDLE_DAYS` 不收回.
  `IDLE_DAYS` 填的不是正数时, `Release idle claims` 每次都失败, 不收回任何领取; 领取的回复里也不提收回, Actions 日志写明原因.
- 提名: 开发者开「提名项目」issue (原项目地址、star 数、许可证、推荐理由、核心功能草稿). 维护者审核, 合适的加进列表, 在 issue 里回复.
- 加项目: 在 `projects.json` 的 `projects` 里加一项, 运行 `python3 projects.py` 重新生成 `PROJECTS.md`, 两个文件一起提交. 例如:

  ```json
  {"id": "excalidraw", "name": "Excalidraw", "upstream": "https://github.com/excalidraw/excalidraw", "tier": "A", "features": ["画图", "导出 PNG"], "warmup": false}
  ```

  公布以后不要改 `id`: 它是仓库名的一部分, 改了已有的领取就对不上了.

## 截止

- 正式赛按截止时 `main` 上的版本验收. 截止以后开发者还能 push, 提交时间也能自己改, 所以 `freeze.py` 不看提交时间, 只看 GitHub 自己记录的分支更新 (`GET repos/<仓库>/activity`, 用 GitHub 服务器的时间):
  时间不晚于 `contest.closes` 的最后一次 `main` 更新 (push 和合并 PR 都算, 不管是谁做的), 更新后 `main` 指向的提交就是这个仓库验收用的提交. 别的分支的更新不算, 截止以后的更新不算, 冻结跑得再晚也一样.
- 参赛仓库: 正式赛截止以前建的、没有归档、名字以 `PREFIX` 开头、有 `STUDENT_ID` 的仓库; 列表模式下还要 `PROJECT` 是正式赛项目.
  截止前一刻开的领取 issue, 机器人截止以后才建好仓库的, 截止时仓库里还没有开发者的东西, 不算参赛, 不冻结.
- 记录: 机器人先在仓库上写 `FROZEN_PUSHED_AT` (那次更新的时间, GitHub 记的 UTC 时间, 比如 `2026-12-07T15:30:00Z`) 和 `SUBMITTED` (`true` 或 `false`),
  最后写 `FROZEN_SHA` (提交的 SHA, 写了它才算记好), 然后归档仓库 (只读, 仍然公开), 再在领取 issue (`CLAIM_ISSUE`) 里 @ 开发者, 给出这个提交的链接. 验收只构建 `FROZEN_SHA`.
  `SUBMITTED` 看开发者自己 (按 `STUDENT_ID`) 有没有更新过 `main`: 列表模式从 `CLAIMED_AT` 算起 (恢复过的领取从恢复的时间算), 自由模式从建仓库算起, 到截止为止.
  生成仓库时机器人的提交、维护者的 push、领取以前的 push (比如热身赛时推到同一个仓库的) 都不算. 是 `false` 的, 留言里写明领取以后没有记录到他往 `main` 的 push.
- `Freeze at the deadline` 在正式赛截止以前什么都不做; 截止以后第一次运行冻结所有参赛仓库, 以后每小时只处理还没归档的. 留言失败 (issue 删了、转走了或锁了) 只在日志里记一行, 接着处理下一个.
  中途失败了, 下一次运行接着做: 有 `FROZEN_SHA` 的仓库不重新算, 还没归档的补上归档和留言.
  `CLAIMED_AT` 填错或删了的, 和 GitHub 的记录里截止时 `main` 还没有任何更新的, 不冻结, 照样处理别的, 最后运行失败, 日志里列出这些仓库.
  维护者改好 `CLAIMED_AT` 等下一次运行; 别的情况查清以后, 不算参赛的在仓库 Settings 里手动归档.
- 放弃: 截止以后 (按 issue 的创建时间) 开的「放弃项目」issue, 参赛仓库一律拒绝, 回复里说正式赛已经截止. 不然开发者可以在冻结之前自己把仓库归档, 躲过冻结. 热身赛项目和截止以后才建的仓库照样能放弃.
- 演练: 在 Actions 里手动运行 `Freeze at the deadline`, `dry_run` 默认勾着: 不写变量、不归档、不留言, 只是每个仓库打印一行 (仓库、开发者、项目或应用名、提交、推送时间、`SUBMITTED`),
  运行页面的 Summary 里有同样的表. 真的冻结也打印这些行和这张表.
  `window` 选 `contest` 演练正式赛; 选 `warmup` 演练热身赛, 列表模式下看 `PROJECT` 是热身赛项目的仓库, 自由模式下看热身赛截止以前建的仓库.
  截止时间取所选时间段的 `closes`; 还没到就取运行的时刻, 表里是现在的 `main`. 热身赛结束以后选 `warmup` 跑一次, 对着表核对几个仓库的提交和 `SUBMITTED`.
  去掉 `dry_run`、选 `contest` 就是真的冻结, 和定时运行一样, 截止以前什么都不做. 选 `warmup` 又去掉 `dry_run` 会直接失败: 热身赛只演练, 不冻结.
- 冻结错了: 在仓库 Settings 里取消归档, 删掉变量 `FROZEN_SHA`、`FROZEN_PUSHED_AT` 和 `SUBMITTED`, 再手动运行 `Freeze at the deadline` (去掉 `dry_run`) 或等下一个小时, 机器人重新按截止时间算, 归档, 再留一次言.
  只取消归档、留着 `FROZEN_SHA` 的, 下一个小时会按原来的记录再归档, 也会再留一次言. 要让仓库保持可写, 先在 Actions 里禁用 `Freeze at the deadline`.

## 规则

- 仓库归属以仓库变量 `STUDENT_ID` (数字 ID) 为准, 用户名会变; `STUDENT_LOGIN` 每次领取时更新, `APP_NAME` 记应用名, `CLAIM_ISSUE` 记最近一次领取的 issue 号.
  列表模式下 `PROJECT` 记领的项目, `CLAIMED_AT` 记领取时间, 见「项目列表」的领取记录.
  放弃或收回时写 `RELEASED_AT` 和 `RELEASE_REASON` (`gave_up` 或 `idle`). 正式赛截止以后冻结时写 `FROZEN_PUSHED_AT`、`SUBMITTED` 和 `FROZEN_SHA`, 见「截止」.
  开发者只有 write 权限, 改不了仓库变量, 也改不了仓库名.
- 应用名和用户名都能含 `-`, 名字可能撞上 (`foo` + `bar-baz` 和 `foo-bar` + `baz`): 后来的人因 `STUDENT_ID` 不符被拒, 换个应用名即可.
- 名字对上但 `STUDENT_ID` 是别人的: 拒绝 (多半是改名后用户名被别人注册). 名字对上但不是从模板生成的, 或没绑定却已有外部协作者 / 邀请: 不动, 报错给维护者.
- 名字对上但仓库已经归档: 拒绝, 不会重新领起来.
- 默认分支由仓库级 ruleset `history` 禁止删除和 force push (免费组织没有组织级 ruleset, 所以逐个仓库设).
- issue 的评论和关闭用 workflow 自带的 `GITHUB_TOKEN`; 建仓库、变量、ruleset、邀请、归档用 GitHub App 的令牌.

## 首次配置 (要组织 owner)

1. 在组织下建 GitHub App, 不要 webhook. Repository permissions: Administration 读写, Variables 读写, Contents 只读, Metadata 只读. 装到组织, 选 All repositories.
   如果从模板生成仓库时报和 workflow 有关的权限错误, 再加 Workflows 读写.
2. 本仓库 Settings → Secrets and variables → Actions: 变量 `ENROLL_APP_CLIENT_ID` (App 的 Client ID), secret `ENROLL_APP_KEY` (App 的私钥).
   没有 App 时可改用 owner 的 fine-grained PAT, 放进 secret `ENROLL_TOKEN`.
3. `app-template` 在 Settings 里勾上 Template repository, 保持公开.
4. 组织 Settings → Member privileges → Base permissions 设成 Read 或 No permission, 否则组织成员能推所有开发者的仓库.
5. 可选变量 `TEMPLATE`、`PREFIX` 覆盖默认的 `app-template` 和 `app-` (仓库名是 `<PREFIX><应用名>-<用户名>`).
6. 可选变量 `MAX_REPOS`、`IDLE_DAYS` 见「项目列表」.

## 运维

- 失败: 机器人在 issue 里留言并保持打开, 看 Actions 日志, 修好后手动运行 `Enroll` 并填 issue 号. `Release idle claims` 和 `Freeze at the deadline` 失败了, 下一个小时会重跑.
- 正式赛开始之前: 把正式赛项目加进 `projects.json`, 热身赛期间加也行 (见「项目列表」). 建议设好 `MAX_REPOS`, 不设的话一个人可以同时领任意多个正式赛项目; 设多少由总负责人定, 现在还没定.
- 演练冻结: 热身赛结束以后演练一次, 正式赛截止以前再演练一次, 见「截止」.
- 停止领取: 在 Actions 里禁用 `Enroll` workflow. 正式赛结束后可以禁用 `Release idle claims`. 冻结完成以前不要禁用 `Freeze at the deadline`.
- 恢复一个放弃或收回的领取: 在仓库 Settings 里取消归档, 删掉变量 `RELEASED_AT` 和 `RELEASE_REASON`; 有 `CLAIMED_AT` 的, 把它改成恢复的时间, 带时区, 比如 `2026-12-03T10:00:00+08:00`.
  收回从这个时间重新算 `IDLE_DAYS`, 恢复以前的 push 不算; 不改的话, 下一个小时又会被收回.
  填错 (没有时区、不是时间) 或删掉了, `Release idle claims` 跳过这个领取, 照样处理别的, 最后失败, 日志里列出这些仓库. 如果这期间正式赛项目已经被别人领了, 恢复后两个人同时领着, 先商量好.
- 模板里的 acdev 版本在 `app-template/.github/workflows/check.yml` 的 `ACDEV_VERSION` 和 `package.json` 的 `@acde/api` 两处, 一起改. 已生成的仓库不会跟着变.
