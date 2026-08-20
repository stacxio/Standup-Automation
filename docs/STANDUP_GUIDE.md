# Daily Stand-up — what each line means, and what you need to do

Every weekday the bot posts a summary of your day to your team's channel. Every
line in it is read from something **you** wrote — in Slack or in Jira. Nothing is
invented, and nothing is judged by a person.

This page explains where each line comes from and what to do so it says
something true about your work.

If you read only one thing, read **§5 — the comment template**. It fixes four
lines at once.

---

## 1. What the message looks like

```
*Raghul*

checkin:
status: done
Previous task: HIR-91, HIR-95, HIR-96
Task picked: HIR-98, HIR-97, HIR-96
Task done: no update
Task status: HIR-98-In Progress, HIR-97-To Do, HIR-96-In Progress

jira:
HIR-98: Add the scheduler similar to the DOB admin console
description: yes
branch: not linked
commit: 60e2a2b (from comments)
PR: not linked
PR merged: No update

Comments:
HIR-98: yes
```

The `checkin:` block is about **your day**. The `jira:` block repeats **per
ticket**. `Comments:` is one line per ticket.

---

## 2. `checkin:` — your day

| Line | What it means | Read from | What you do |
|------|---------------|-----------|-------------|
| `status:` | Whether you posted a stand-up today. `done` or `No update from developer`. | Your message in **#stacx-check-in** | Post your stand-up every working day. |
| `Previous task:` | The ticket ids from your **last** stand-up — whatever state they are in. | Your previous stand-up | Nothing. It shows what you carried over. |
| `Task picked:` | The ticket ids you named in **today's** stand-up. | Today's stand-up | **Always write the ticket id** — `HIR-98`, not "the scheduler work". |
| `Task done:` | Which of today's picked tickets Jira says are **Done**. | Jira status | Move a ticket to Done when it is done. |
| `Task status:` | Each picked ticket's live Jira status — `HIR-98-In Progress`. | Jira status | Keep the board matching reality. |

### Why `Task picked:` matters more than it looks

If your stand-up names no ticket id, **every ticket line below is empty and your
score is capped at 10 out of 100** — however much work you did. The bot cannot
find work it was never told about.

Write ids in any of these forms; all are understood:

```
HIR-98      HIR 98      HIR-98, HIR-97      hir-98
```

Only ids for real projects count: **SP, WS, HIR, BHA**. Something like `PHASE-2`
is not a Jira ticket, so it shows as `PHASE-2-unknown` and earns nothing.

---

## 3. `jira:` — one block per ticket

| Line | What it means | Read from | What you do |
|------|---------------|-----------|-------------|
| `description:` | `yes` if the ticket has a real description. A sub-task inherits its parent's. | Jira description | Write what the ticket is for. A few words like "fix" is not enough. |
| `branch:` | Your branch name. | Jira dev panel, else `branch:` in a comment | Put `branch: <name>` in a comment. |
| `commit:` | Your commit ids, exactly as you wrote them. | Jira dev panel, else commit ids in comments | Paste the commit id in a comment. |
| `PR:` | Your pull request. | Jira dev panel, else a PR link in a comment | Paste the PR link in a comment. |
| `PR merged:` | `Yes`, `No`, or `No update` if you have not said. | `PR merged:` in a comment | Say `Yes` or `No`. |

**`not linked` means nothing was found — not that you did nothing.** Right now
GitHub is not connected to Jira, so the bot reads these from your comments. Once
GitHub is connected they fill in automatically and you can stop typing them.

---

## 4. `Comments:` — did you update the ticket

| Value | Meaning |
|-------|---------|
| `yes` | You commented on that ticket today. |
| `yes (by others)` | Somebody else commented, not you. It does not count for you. |
| `no comments` | Nobody commented. |

A comment counts when it is **yours**, **posted today**, at least ~20
characters, and **not a copy** of your previous comment on that ticket. A
screenshot on its own counts — posting evidence is an update.

Pasting "working on it" into every ticket every morning does not count. That is
deliberate.

---

## 5. The comment template — do this one thing

Paste this into a Jira comment on each ticket you worked on, and fill it in:

```
branch: feature/HIR-98
commit: 60e2a2b
PR: https://github.com/your-org/your-repo/pull/42
PR merged: Yes
```

That single comment fills `branch`, `commit`, `PR` and `PR merged` — four lines
at once — and counts as your comment for the day.

**Leave nothing blank.** A template posted with empty fields:

```
branch: commit: PR: not raised yet PR merged: no PR exists
```

reads as nothing at all. If there is no PR yet, either omit the line or write
`PR merged: No`.

Pasting a link is fine — Jira turns it into a preview card and the bot still
reads the URL underneath.

---

## 6. The five mistakes that cost the most

| Mistake | What happens | Fix |
|---------|--------------|-----|
| Stand-up with no ticket id | Score capped at 10/100 | Write `HIR-98` in your stand-up |
| Ticket left `In Progress` after the work is finished | Counts as 25% done, not 100% | Move it to Review or Done |
| Comment says "done and merged" but the status never moved | The status is what counts, not the comment | Move the ticket |
| Template posted with blank fields | Reads as no branch, no commit, no PR | Fill it in or leave the line out |
| Made-up id like `PHASE-2` | Shows as `unknown`, earns nothing | Use a real SP / WS / HIR / BHA id |

---

## 7. How this becomes your score

You get one score a day out of 100, sent to you by DM. It is worked out by code,
the same way for everyone, from the lines above — no one grades you.

**Process — 40 points**

| | |
|---|---|
| Posted your check-in | 10 |
| Named a ticket id | 5 |
| Ticket has a description | 10 |
| Commit linked | 5 |
| Your own comment on the ticket | 10 |

**Delivery — 60 points**, per ticket:

| Ticket state | Credit |
|---|---|
| Done | 100% |
| In review **with** a commit id | 100% |
| In review | 50% |
| In progress | 25% |
| To Do / untouched | 0% |

Absent is 0 for the day. **Approved leave, weekends, and days where the data
could not be read are not scored at all** — they are left out of your average
rather than counted as zero.

Your DM lists every ticket and which checks passed, so any number can be traced
back to what produced it. If something looks wrong, that breakdown is where to
start.

Full rules: [SCORING.md](SCORING.md).
