# Project 5 — Mixtape Bug Hunt · Submission

## AI Usage
_(Fill in at Milestone 4.)_ I used Claude Code during codebase orientation to summarize each service
file and trace call chains, and during debugging to explain suspicious functions once I had located
them myself

## Codebase Map

### Overview
Mixtape is a Flask app with a strict three-layer structure. Every request flows through the same shape:

```
HTTP request → routes/*.py (parse input, format JSON)
             → services/*.py (all business logic)
             → models.py (SQLAlchemy ORM tables)
             → SQLite (instance/mixtape.db)
```

The application is assembled by a **factory function** (`create_app` in `app.py`), which registers four
blueprints under URL prefixes and calls `db.create_all()`. Run it with
`FLASK_APP=app:create_app flask run` — never `python app.py` (double-import).

### Main files and their roles

**`app.py`** — Application factory. Creates the Flask app, configures SQLAlchemy (SQLite at
`instance/mixtape.db`), and registers four blueprints: `/songs`, `/playlists`, `/users`, `/feed`.

**`models.py`** — Defines the ORM. Seven entities plus three association tables:
- `User` — has `listening_streak` and `last_listened_at` columns (streak state lives on the user row).
- `Song` — `shared_by` FK points to the user who shared it (this is who gets notified about it).
- `ListeningEvent` — one row per play, with `listened_at`. Backs both streaks and the feed.
- `Rating` — score 1–5, unique per (user, song). Stored as its own row, not on the Song.
- `Playlist` — has `is_collaborative`.
- `Notification` — `user_id` (recipient), `notification_type`, `body`, `read`.
- `Tag` — song genres/labels.
- Association tables: `friendships` (symmetric, stored as two rows per pair), `song_tags`
  (many-to-many), and `playlist_entries` — a join table that adds a **`position`** column, so songs in a
  playlist have an explicit order, not just insertion order, plus `added_by` and `added_at`.

**`routes/`** — Thin HTTP layer, one blueprint per resource. Each handler parses the request, calls a
service function, and jsonifies the result. Notable endpoints:
- `songs.py` — `GET /songs/search?q=`, `GET /songs/<id>`, `POST /songs/<id>/rate`, `POST /songs/<id>/listen`
- `playlists.py` — `POST /playlists/`, `GET /playlists/<id>`, `GET /playlists/<id>/songs`, `POST /playlists/<id>/songs`
- `users.py` — `GET /users/<id>`, `GET /users/<id>/streak`, `GET /users/<id>/notifications`, `POST /users/notifications/<id>/read`
- `feed.py` — `GET /feed/<id>/listening-now`, `GET /feed/<id>/activity`

**`services/`** — All business logic. One module per domain:
- `streak_service.py` — records listening events and updates `listening_streak` (Issue #1 lives here).
- `feed_service.py` — "Friends Listening Now" and activity feed (Issue #2 lives here).
- `search_service.py` — song search by title/artist (Issue #3 lives here).
- `notification_service.py` — creates notifications; also owns `add_to_playlist` and `rate_song`
  (Issue #4 lives here).
- `playlist_service.py` — playlist creation/retrieval (Issue #5 lives here).

**`seed_data.py`** — Rebuilds the DB with 5 users, 25 songs (deliberately including songs with 3+ tags to
expose the search-duplicate bug), 3 playlists, friendships, listening events (recent + old), and one
working playlist-add notification (so the correct notification pattern is visible when investigating #4).

**`tests/`** — Pytest suites for playlists, search, and streaks.

### Data flow — user rates a song (traces the notification path)
1. `POST /songs/<song_id>/rate` with JSON `{user_id, score}` hits `rate()` in `routes/songs.py`.
2. The route validates presence of `user_id`/`score` and calls
   `notification_service.rate_song(user_id, song_id, int(score))`.
3. `rate_song` validates the score range, loads the `Song` and rater, then upserts a `Rating` row
   (updates the existing rating if the user already rated this song, otherwise inserts a new one) and
   commits.
4. It returns the `Rating`, which the route serializes with `to_dict()` and returns as `201`.

Compare this to the **working** path, `add_to_playlist` in the same file: after mutating data it calls
`create_notification(...)` for `song.shared_by`. That side-by-side comparison is the lens for Issue #4.

### Data flow — a song appears in "Friends Listening Now"
1. `POST /songs/<id>/listen` → `record_listening_event` inserts a `ListeningEvent` row with `listened_at=now`.
2. `GET /feed/<id>/listening-now` → `get_friends_listening_now` collects the user's friend IDs, queries
   `ListeningEvent` rows newer than a cutoff, orders by most-recent, and keeps only the latest event per
   friend before serializing.

### Patterns I noticed
- **Routes never contain business logic.** They parse input, call one service function, and format the
  response. All logic (and all five bugs) lives in `services/`.
- **Streak state is denormalized onto the `User` row** (`listening_streak`, `last_listened_at`) rather
  than derived from `ListeningEvent` rows on read.
- **Playlist ordering is explicit** via `playlist_entries.position`, not insertion order — worth
  remembering for the playlist bug.
- **Friendships are stored as two directed rows**, so friend lookups are symmetric.
- Times are stored as UTC (`datetime.now(timezone.utc)`), and comparisons re-attach `tzinfo` where a
  stored value came back naive.

---

## Bug Reproduction Notes
_(Milestone 2 — fill in as you reproduce each chosen bug.)_

## Root Cause Analysis

### Issue #5 — The last song in a playlist never shows up
**How I reproduced it.** With the seeded DB, the "Friday Energy" playlist has 7 rows in the
`playlist_entries` table, but `GET /playlists/<id>/songs` (→ `get_playlist_songs`) returned only 6.
I confirmed the true count by querying `playlist_entries` directly (7) and comparing it to the service
return value (6). The missing song was always the one with the highest `position` — i.e. the most
recently added — matching the report that adding a new song "frees" the previously missing one.

**How I found the root cause.** I started at the route `GET /playlists/<id>/songs` in
`routes/playlists.py`, which delegates to `get_playlist_songs` in `services/playlist_service.py`. The
query itself was correct — it joins `playlist_entries`, filters by playlist, and orders ascending by
`position`. The bug was on the return line: `return [song.to_dict() for song in songs[:-1]]`. The
moment I saw `[:-1]` I was confident: that slice drops the final element of an already-ordered list,
and the docstring explicitly promises "returns all songs," so the slice directly contradicts the
function's contract.

**The root cause.** The list comprehension sliced the ordered result with `songs[:-1]`, which excludes
the last element. Because the query orders songs ascending by `playlist_entries.position`, the last
element is always the song with the highest position — the most recently added one. So the newest song
was silently truncated on every read.

**My fix and side-effect check.** I changed `songs[:-1]` to `songs`, returning the complete ordered
list. I re-ran `tests/test_playlists.py` — `test_playlist_returns_all_songs`,
`test_playlist_returns_songs_in_order`, and `test_empty_playlist_returns_empty_list` all pass (the first
two previously failed). I verified an empty playlist still returns `[]` (slicing an empty list also gave
`[]`, so no behavior change there) and that ordering is unchanged. A live check confirmed "Friday
Energy" now returns all 7 songs.

### Issue #4 — Notified on playlist-add but not on rating
**How I reproduced it.** Using the seeded data, I checked simone's notification count (0), then called
`rate_song(user_id=kenji, song_id=<a song simone shared>, score=5)` and re-checked. The count stayed at
**0** — the rating was persisted (visible on the song) but no notification was created, exactly as
aaliya reported. For contrast, the playlist-add path in the same module *does* create a notification.

**How I found the root cause.** I traced `POST /songs/<id>/rate` (`routes/songs.py`) →
`rate_song` in `services/notification_service.py`. Following the hint, I read `rate_song` line-by-line
against the working `add_to_playlist` in the same file. `add_to_playlist` ends with a guarded call:
`if song.shared_by != added_by_user_id: create_notification(...)`. `rate_song` had no equivalent — it
committed the `Rating` and returned. That side-by-side comparison confirmed the cause was a missing
step, not a typo or a broken condition.

**The root cause.** `rate_song` never called `create_notification`. The notification-on-interaction
pattern that exists for playlist adds was simply never implemented for ratings, so no `Notification`
row was ever produced when a song was rated.

**My fix and side-effect check.** After the `db.session.commit()` that saves the rating, I added the
same guarded notification the playlist path uses: when `song.shared_by != user_id`, create a
`song_rated` notification for the sharer. I mirrored the existing pattern (guard so users aren't
notified about rating their own songs; reuse `create_notification`). I wrote a regression test
(`tests/test_notifications.py::test_rating_notifies_the_sharer`) asserting a `song_rated` notification
is created, plus `test_rating_own_song_does_not_notify` for the self-rating guard — both pass. I
confirmed the existing rate-then-update behavior (score changes for an already-rated song) still works
and that the playlist-add notification is unaffected.

### Issue #1 — Listening streak resets on Sundays
**How I reproduced it.** I couldn't rely on the real clock (today isn't a Sunday), so I called
`update_listening_streak(user, now)` directly with a controlled `now`. I set a user to
`listening_streak = 12` with `last_listened_at` on a Saturday, then called the function with a Sunday
`now` one calendar day later. The streak dropped from 12 to **1** instead of going to 13. As a control,
the same "consecutive day" scenario landing on a Monday correctly produced 13 — isolating the trigger
to Sunday specifically.

**How I found the root cause.** From `POST /songs/<id>/listen` (`routes/songs.py`) →
`record_listening_event` → `update_listening_streak` in `services/streak_service.py`. Reading the
branch that increments the streak, I saw the condition
`elif days_since_last == 1 and today.weekday() != 6:`. I confirmed with a one-liner that
`datetime.weekday()` returns `6` for Sunday, which meant the increment branch is skipped on Sundays and
control falls through to the `else` that resets the streak to 1. That was the specific cause, not just a
suspicious area.

**The root cause.** `datetime.weekday()` returns `6` for Sunday. The increment branch required
`today.weekday() != 6`, so any streak update that happened on a Sunday failed the condition and fell
into the reset branch — throwing away the streak even though the user listened on consecutive days.
There is no rule in the function's documented behavior that justifies treating Sunday differently; the
clause was simply wrong.

**My fix and side-effect check.** I removed the `and today.weekday() != 6` clause, so the branch is now
`elif days_since_last == 1:` — a consecutive calendar day always increments, on any weekday. I re-ran
`tests/test_streaks.py`: all 5 pass, including the previously-failing `test_streak_increments_on_sunday`.
I specifically checked `test_streak_resets_after_skipped_day` still passes, confirming that a genuine
gap (more than one day) still resets to 1, and `test_streak_does_not_double_count_same_day` confirms a
same-day repeat still makes no change.

### Issue #2 — "Friends Listening Now" shows people from yesterday
**How I reproduced it.** I inserted a `ListeningEvent` for a friend timestamped in the previous
evening and called `get_friends_listening_now`. The friend still appeared. Isolating it in a fresh
in-memory DB (so seed data couldn't add other same-day plays), a friend whose only play was yesterday
at 23:00 was included in the morning feed — matching nova's report about darius.

**How I found the root cause.** From `GET /feed/<id>/listening-now` (`routes/feed.py`) →
`get_friends_listening_now` in `services/feed_service.py`. The filter compared `listened_at` against
`cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD`, where `RECENT_THRESHOLD = timedelta(hours=24)`.
Seeing the cutoff was `now - 24h` made it clear: this is a rolling 24-hour window, not a "today"
boundary, which is why an 11pm play is still within range at 9am the next morning (only ~10 hours old).

**The root cause.** The feed defined "recent" as "within the last 24 hours" (a sliding window) rather
than "since the start of today." Any play from the previous evening stays inside a 24-hour window until
the same clock time the next day, so yesterday's activity lingers in a feed that is supposed to mean
"today."

**My fix and side-effect check.** I replaced the rolling window with a calendar-day boundary: the cutoff
is now the start of the current UTC day (`now.replace(hour=0, minute=0, second=0, microsecond=0)`), and
I removed the now-unused `RECENT_THRESHOLD` constant and `timedelta` import. I wrote
`tests/test_feed.py` with a boundary pair — a play at yesterday 23:00 is excluded, a play at today 00:01
is included — both pass. I checked `get_activity_feed` in the same module is untouched (it never used
the threshold and is intentionally not time-filtered), and that the per-friend dedup and ordering are
unchanged. Note: "today" is evaluated in UTC, consistent with how the rest of the app stores timestamps.

### Issue #3 — The same song shows up multiple times in search
**How I reproduced it.** This one is conditional and environment-sensitive. Running `search_songs`
directly against the seeded DB returned "Crown Heights Anthem" only **once**, not the reported three
times. To find out why, I executed the service's own query at the raw-SQL level
(`db.session.execute(q.statement).all()`) and it returned **3 rows** for that one song — one per tag.
So the buggy query genuinely multiplies rows; the duplicates just weren't surfacing through the ORM in
this environment (SQLAlchemy 2.0.51's legacy `Query.all()` de-duplicates ORM entities by primary key).
The reporter clearly hit a path/version where that masking didn't apply. The condition that triggers
the extra rows is a song having **more than one tag** — which is exactly why the seed data includes
songs with 3+ tags.

**How I found the root cause.** From `GET /songs/search?q=` (`routes/songs.py`) → `search_songs` in
`services/search_service.py`. The query did
`.outerjoin(song_tags, Song.id == song_tags.c.song_id)` and then filtered only on `Song.title`/
`Song.artist`. The join fans a song out to one row per tag, and nothing downstream uses `song_tags` —
the filter doesn't reference it and the output builds tags from the `Song.tags` relationship inside
`to_dict()`. The moment I confirmed the join was both the source of the row multiplication *and*
entirely unused, I knew it was the root cause rather than a symptom.

**The root cause.** The `outerjoin(song_tags, ...)` produced a Cartesian-style expansion: one result
row per (song, tag) pair. A song with N tags produced N identical rows. The join contributed nothing to
filtering or output, so correctness depended entirely on the ORM happening to de-duplicate the rows —
which is fragile and not guaranteed across query styles/versions.

**My fix and side-effect check.** I removed the `outerjoin(song_tags, ...)` entirely (and the now-unused
`Tag`/`song_tags` imports), leaving a plain `query(Song).filter(...)`. This fixes the duplication at its
source instead of masking it. I verified the raw SQL now returns 1 row (previously 3), and that tags
still appear in each result (`['rap', 'hip-hop', 'boom bap']`) since they load from the `Song.tags`
relationship, not the join. All 5 tests in `tests/test_search.py` pass — including
`test_search_no_duplicates_multi_tag_song` — and matching by artist (`test_search_returns_matching_songs`)
and the empty-result case still work.
