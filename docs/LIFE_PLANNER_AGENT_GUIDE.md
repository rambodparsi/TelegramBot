# Life Planner: Guide for AI Agents

This file explains how to read and edit the owner's **Life Planner** Google Sheet. Read all of it before making any change. The same rules are summarized in the sheet's `README_AI` tab. If the two ever disagree, the sheet's `README_AI` tab wins.

---

## 1. What this sheet is

The sheet is a personal planner covering the owner's whole life: daily time blocks, to-dos, events, routines, habits, goals (year → quarter → month → week) and reflections.

It has two kinds of tabs:

| Kind | Tabs | Agent may write? |
|---|---|---|
| **Data** | `Inbox`, `Tasks`, `Schedule`, `Goals`, `Routines`, `Habits`, `Reviews`, `ChangeLog` | **Yes**, following the rules below |
| **Config** | `Settings` | Only when the owner asks |
| **Views** | `Dashboard`, `Daily`, `Weekly`, `Monthly`, `Yearly` | **Never**. They contain only formulas |
| **Helper** | `_GoalCalc` (hidden), `README_AI` | **Never** |

The views calculate everything from the data tabs. If you change data correctly, the views update by themselves. You never need to touch a view to make something appear.

---

## 2. Golden rules

1. **Never delete rows** in any tab. To remove something, change its status instead:
   - Tasks / Schedule → `Status = Cancelled`
   - Goals → `Status = Dropped`
   - Inbox → `Status = Discarded`
   - Routines → `Active = No`
2. **Never insert rows in the middle, sort, filter permanently, reorder or rename columns, or rename tabs.** The views depend on exact positions and names.
3. **Append new rows directly after the last used row.** Row 1 is always the header. Find the last row by reading column A.
4. **Every new row gets a new unique ID.** Take the highest existing number for that prefix and add 1. Never reuse an ID, even one that was cancelled.
5. **Use only allowed values** (section 5) for dropdown columns. Values are case-sensitive and must match exactly.
6. **Fixed items are protected.** Do not move, shorten or cancel a row with `Flexibility = Fixed` without the owner's explicit OK. `Flexible` items may be moved to resolve conflicts.
7. **Mark your work.** Set `CreatedBy = ai` on rows you create. Set `UpdatedAt` to the current date-time on rows you change.
8. **Log every change** in `ChangeLog`: one row per changed field (section 7).
9. **Never create overlapping time blocks** (section 8).
10. **When in doubt, ask the owner.** At the end of a session, summarize what you changed.

---

## 3. Formats

| Kind | Format | Example |
|---|---|---|
| Date | `YYYY-MM-DD` | `2026-11-23` |
| Time | 24-hour `HH:MM` | `07:30`, `24:00` |
| Date-time | `YYYY-MM-DD HH:MM` (owner's timezone) | `2026-11-23 14:05` |
| Boolean (Habits) | `TRUE` / `FALSE` | `TRUE` |
| Number | Plain number, no units | `45` |

- Write through the Google Sheets API with `valueInputOption = USER_ENTERED` so dates and times are stored as real values, not text.
- Midnight at the **end** of a day is `24:00`. A block may not cross midnight: split it into two rows (for example 23:00–24:00 and 00:00–01:00 on the next date).
- Timezone: `Settings!B3` (default `America/Chicago`).

---

## 4. Tab schemas

Columns are listed in order. **Never change column order.**

### 4.1 `Tasks`: to-dos (views read rows 2–1000)

| Col | Field | Type | Required | Notes |
|---|---|---|---|---|
| A | TaskID | `T-0001` | yes | Unique |
| B | Title | text | yes | Short, starts with a verb |
| C | Area | list: Areas | yes | |
| D | GoalID | `G-###` | no | Links the task to a goal; counts toward goal progress |
| E | Priority | `P1`–`P4` | yes | P1 must · P2 should · P3 could · P4 someday |
| F | Status | list: TaskStatus | yes | New tasks start as `Not started` |
| G | DueDate | date | no | The deadline |
| H | PlannedDate | date | no | The day the owner intends to do it |
| I | EstMinutes | number | recommended | Estimate if the owner didn't give one |
| J | Flexibility | `Fixed` / `Flexible` | yes | |
| K | Notes | text | no | |
| L | CreatedBy | list: Actor | yes | `ai` for your rows |
| M | CreatedAt | date-time | yes | |
| N | UpdatedAt | date-time | yes | |
| O | CompletedDate | date | when Done | Set when Status becomes `Done` |

### 4.2 `Schedule`: time blocks and events (views read rows 2–3000)

| Col | Field | Type | Required | Notes |
|---|---|---|---|---|
| A | EventID | `E-0001` | yes | Unique |
| B | Date | date | yes | |
| C | Start | time | yes | |
| D | End | time | yes | Must be later than Start; `24:00` allowed |
| E | Title | text | yes | |
| F | Type | list: EventType | yes | `Event`, `Task block`, `Routine`, `Focus`, `Break`, `Travel` |
| G | Area | list: Areas | yes | Controls the color in the views |
| H | LinkedID | TaskID or RoutineID | for task blocks and routines | |
| I | Flexibility | `Fixed` / `Flexible` | yes | |
| J | Status | list: EventStatus | yes | `Planned`, `Done`, `Skipped`, `Cancelled` |
| K | Location | text | no | |
| L | Notes | text | no | |
| M | CalendarEventID | text | no | Reserved for future Google Calendar sync |
| N | CreatedBy | list: Actor | yes | |
| O | UpdatedAt | date-time | yes | |

### 4.3 `Goals`: views read rows 2–301

| Col | Field | Type | Required | Notes |
|---|---|---|---|---|
| A | GoalID | `G-001` | yes | Unique |
| B | Title | text | yes | |
| C | Level | `Year` / `Quarter` / `Month` / `Week` | yes | |
| D | Area | list: Areas | yes | |
| E | ParentGoalID | `G-###` | no | Links to the goal one level up |
| F | StartDate | date | yes | |
| G | EndDate | date | yes | The Year views use the **year of EndDate** |
| H | Metric | text | no | Unit name, e.g. `books`, `USD saved` |
| I | Target | number | no | If set, progress = Current / Target |
| J | Current | number | no | Update this when progress is reported |
| K | Status | list: GoalStatus | yes | `Active`, `Done`, `Paused`, `Dropped` |
| L | Why / Notes | text | no | |
| M | CreatedBy | list: Actor | yes | |
| N | UpdatedAt | date-time | yes | |

### 4.4 `Routines`: repeating templates

| Col | Field | Notes |
|---|---|---|
| A | RoutineID | `R-01` |
| B | Title | |
| C | Area | |
| D | Days | `Daily`, `Weekdays`, `Weekends`, or a list like `Mon,Wed,Fri` |
| E | WindowStart | Earliest allowed start time |
| F | WindowEnd | Latest allowed end time |
| G | DurationMin | Minutes |
| H | Flexibility | `Fixed` = always at WindowStart; `Flexible` = anywhere inside the window |
| I | Type | Usually `Routine`, `Focus` or `Break` |
| J | Active | `Yes` / `No` |
| K | Notes | |
| L | UpdatedAt | |

**Routines do not appear in any view on their own.** To place them on the calendar, copy them into `Schedule` as dated rows (section 6, "Weekly planning").

### 4.5 `Habits`: one row per date

- Column A = Date (pre-filled: 2026-11-01 through 2027-12-31, rows 2–427). Column B = weekday. Column K = Notes.
- Columns C–J = one habit each. The header in row 1 is the habit name, pulled from `Settings!N2:N9`. A header of `(unused)` means that slot is empty.
- To record a habit, find the row whose Date matches, then set that habit's cell to `TRUE` (done) or `FALSE` (not done). **Do not append rows here.**
- If you need dates after 2027-12-31, ask the owner first.

### 4.6 `Inbox`

| Col | Field | Notes |
|---|---|---|
| A | InboxID | `I-0001` |
| B | CapturedAt | date-time |
| C | Text | The raw idea |
| D | Status | `New`, `Processed`, `Discarded` |
| E | ConvertedToID | The ID of the task, event or goal it became |
| F | Notes | |
| G | CreatedBy | |

### 4.7 `Reviews`

| Col | Field | Notes |
|---|---|---|
| A | ReviewID | `RV-0001` |
| B | Type | `Daily`, `Weekly`, `Monthly`, `Yearly` |
| C | PeriodStart | First date of the day, week (Monday), month or year |
| D | Wins | |
| E | Challenges | |
| F | Lessons | |
| G | NextFocus | |
| H | Energy1to5 | 1–5 |
| I | Mood1to5 | 1–5 |
| J | CreatedBy | |
| K | CreatedAt | |

### 4.8 `ChangeLog`

| Col | Field | Notes |
|---|---|---|
| A | Timestamp | date-time |
| B | Actor | `ai` |
| C | Action | `create`, `update`, `move`, `complete`, `cancel` |
| D | Tab | e.g. `Tasks` |
| E | ItemID | e.g. `T-0012` |
| F | Field | Column name changed (blank for `create`) |
| G | OldValue | |
| H | NewValue | |
| I | Reason | Short, e.g. "Owner asked to move gym to Thursday" |

---

## 5. Settings and allowed values

### 5.1 Preferences (`Settings` column B)

| Cell | Setting | Default | How to use it |
|---|---|---|---|
| B2 | PlanningYear | 2027 | Year shown in Dashboard / Yearly goals |
| B3 | Timezone | America/Chicago | Use for all date-times |
| B4 | WeekStart | Monday | Weeks run Monday–Sunday |
| B5 | DayStart | 06:00 | Don't schedule before this |
| B6 | DayEnd | 24:00 | Don't schedule after this |
| B7 | SlotMinutes | 30 | Size of one row in the Daily and Weekly views; prefer starts on :00 or :30 |
| B8 | WorkHours | 09:00-17:00 | Put Work items here; keep personal items outside it when possible |
| B9 | MaxFocusHoursPerDay | 4 | Cap on `Focus` + `Task block` hours per day |
| B10 | BufferMinutes | 10 | Leave this gap between blocks you create |
| B11 | QuietHours | 23:00-06:00 | Only sleep and wind-down items here |
| B12 | AIMayMove | Flexible | You may move only Flexible items without asking |
| B13 | AIMayDelete | No | Never delete; cancel instead |

Always read the live values. The owner may change them.

### 5.2 Allowed values (`Settings`, row 2 downward)

| Column | List | Values |
|---|---|---|
| E | Areas | Work, Health, Money, Family, Personal growth, Home, Fun |
| G | TaskStatus | Not started, In progress, Waiting, Done, Cancelled |
| H | EventStatus | Planned, Done, Skipped, Cancelled |
| I | Priority | P1, P2, P3, P4 |
| J | EventType | Event, Task block, Routine, Focus, Break, Travel |
| K | Flexibility | Fixed, Flexible |
| L | GoalLevel | Year, Quarter, Month, Week |
| M | GoalStatus | Active, Done, Paused, Dropped |
| N | Habits | Up to 8 habit names (N2:N9) |
| O | ReviewType | Daily, Weekly, Monthly, Yearly |
| P | Actor | me, ai, sample |
| Q | InboxStatus | New, Processed, Discarded |
| R | YesNo | Yes, No |

The lists above are the defaults. Read the `Settings` tab for the current ones, since the owner may add areas or habits.

---

## 6. How to do common jobs

**Add a task**
1. Append a row to `Tasks` with the next TaskID, `Status = Not started`, `CreatedBy = ai`, and CreatedAt / UpdatedAt = now.
2. Set PlannedDate only if the owner chose a day or you are also scheduling it.
3. Log a `create` row.

**Schedule a task (time-block it)**
1. Read that date's non-cancelled `Schedule` rows.
2. Find a free gap of at least `EstMinutes` plus `BufferMinutes`. Respect WorkHours, DayStart/DayEnd, QuietHours and MaxFocusHoursPerDay.
3. Append a `Schedule` row: `Type = Task block`, `LinkedID = TaskID`, the task's Area, `Flexibility = Flexible`, `Status = Planned`.
4. Set the task's PlannedDate to that date.
5. Log both changes.

**Add an event (meeting, appointment, plan)**
- Append to `Schedule` with `Type = Event`. Use `Flexibility = Fixed` for anything involving other people or a booked time.

**Move something**
1. Only Flexible items, unless the owner says otherwise.
2. Change Date / Start / End.
3. If it is a task block, also update the task's PlannedDate.
4. Log each changed field with `Action = move`.

**Complete a task**
1. In `Tasks`: set Status to `Done` and CompletedDate to today.
2. Set its linked `Schedule` rows to `Status = Done`.
3. Log both changes with `Action = complete`.

**Cancel something**
- Set Status to `Cancelled` and log it. The freed time may be offered to Flexible items.

**Process the inbox**
1. For every `Inbox` row with `Status = New`, decide whether it is a task, an event, a goal or just an idea.
2. Create the item.
3. Set the Inbox row to `Status = Processed` and ConvertedToID to the new item's ID. Use `Discarded` if it needs no action.
4. Ask the owner if the intent is unclear.

**Weekly planning (usually Sunday)**
1. For each Routine with `Active = Yes`, append one `Schedule` row for each matching day of the coming Monday–Sunday. Use `Type` = the routine's Type and `LinkedID = RoutineID`. Fixed routines start at WindowStart. Flexible ones go anywhere inside the window that is free.
2. Roll unfinished tasks forward. For each task with Status not `Done` or `Cancelled` and a PlannedDate in the past, propose a new PlannedDate.
3. Propose 2–4 `Week` goals linked (via ParentGoalID) to the current `Month` goals.
4. Check that no day exceeds MaxFocusHoursPerDay and that nothing overlaps.
5. Show the owner the plan before or right after writing it.

**Record a habit**
- Set the matching `Habits` cell (date row × habit column) to `TRUE`.

**Update a goal**
- If the goal has a Target, update Current.
- Otherwise its progress is automatic (section 9), so mark the linked tasks Done.
- When a goal is achieved, set its Status to `Done`.

**Write a review**
- Append to `Reviews`. For a daily shutdown, ask the owner for one win and one thing to improve.

---

## 7. ChangeLog example

The owner asks to move Friday's workout to Saturday at 08:00:

| Timestamp | Actor | Action | Tab | ItemID | Field | OldValue | NewValue | Reason |
|---|---|---|---|---|---|---|---|---|
| 2026-11-25 19:02 | ai | move | Schedule | E-0041 | Date | 2026-11-27 | 2026-11-28 | Owner asked to move workout |
| 2026-11-25 19:02 | ai | move | Schedule | E-0041 | Start | 07:00 | 08:00 | Owner asked to move workout |
| 2026-11-25 19:02 | ai | move | Schedule | E-0041 | End | 08:00 | 09:00 | Owner asked to move workout |

---

## 8. Conflicts

Two non-cancelled `Schedule` rows on the same Date overlap when `StartA < EndB` **and** `StartB < EndA`.

- Never create an overlap.
- If a new Fixed item collides with a Flexible one, move the Flexible one to the next free slot (inside its routine window if it has one) and tell the owner.
- If two Fixed items collide, ask the owner.
- The Daily view shows only one item per time slot. If items overlap, it shows the one lower in the sheet, so an overlap hides an item from the owner.

---

## 9. How the views decide what to show

This section explains why something does or doesn't appear in a view.

| View | Shows |
|---|---|
| **Daily** time blocks | `Schedule` rows where Date = selected date and Status ≠ Cancelled, placed in 30-minute slots from 06:00 to 24:00 |
| **Daily** to-do list | `Tasks` where PlannedDate = selected date, **or** PlannedDate is empty and DueDate = selected date; Status ≠ Cancelled. Sorted with Done last, then by Priority (P1 first), then by row order. Up to 15 tasks; the top 3 are shown in bold |
| **Weekly** grid | Same as the Daily time blocks, for Monday–Sunday |
| **Weekly** objectives | Goals with `Level = Week` whose StartDate–EndDate overlaps the week, Status ≠ Dropped |
| **Weekly** workload | Tasks counted by PlannedDate; hours = sum of Schedule durations |
| **Monthly** calendar | First event of each day plus "+N more" and task counts. **Rows with Type = Routine or Break are left out** |
| **Monthly** goals | Goals with `Level = Month` overlapping the month |
| **Yearly** | Goals with `Level = Year` / `Quarter` whose EndDate falls in the selected year, plus month-by-month task, event, habit and review stats |
| **Dashboard** | Today's tasks (same rule as Daily); overdue = DueDate before today and Status not Done or Cancelled; next 6 events today that haven't ended; Year goals for PlanningYear; habit consistency this month; count of `New` inbox items |

**Goal progress** is calculated in the hidden `_GoalCalc` tab:
- `Status = Done` → 100%.
- Otherwise, if Target is set → Current ÷ Target (capped at 100%).
- Otherwise → linked tasks Done ÷ linked tasks not Cancelled.
- If the goal also has child goals, the children's average is blended in. A goal with no target and no tasks uses only the children's average.
- This works through 4 levels (Year → Quarter → Month → Week).

---

## 10. Limits

- **Row capacity:** Tasks rows 2–1000 · Schedule rows 2–3000 · Goals rows 2–301 · Habits rows 2–427 (views can read up to row 800). Warn the owner when a tab passes 90% of its limit.
- **No notifications:** the sheet cannot send reminders. If the owner wants reminders, create a Google Calendar event for the Fixed item and store its ID in `Schedule.CalendarEventID`.
- **Rows marked `CreatedBy = sample`** are demo data. Ignore them when reasoning about the owner's real life. Remove them only if the owner asks, by clearing values rather than deleting rows.

---

## 11. Quick checklist before saving changes

- [ ] Wrote only to data tabs (never to views or `_GoalCalc`)
- [ ] New IDs are unique and sequential
- [ ] All list values match `Settings` exactly
- [ ] Dates are `YYYY-MM-DD`, times are `HH:MM`, written with USER_ENTERED
- [ ] No overlaps; no Fixed item moved without permission
- [ ] `CreatedBy` / `UpdatedAt` set
- [ ] One `ChangeLog` row per changed field
- [ ] Told the owner what changed
