"""What the access database records about sign-ins, and what it reports back.

The developer area's user-activity page is only as honest as this history, so
these cover what is written (and deliberately not written) on each kind of
attempt, what a window counts, and what happens when the file reaches its cap.
"""

import hashlib
import sqlite3
import time

import pytest

from services import access_control as access_control_module
from services.access_control import (
    ACTIVITY_MAX_DAYS,
    LOGIN_FAILURE_LIMIT,
    MAX_PIN_CHARS,
    MAX_USERNAME_CHARS,
    AccessControl,
)


def _control(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")
    return control


def _events(control):
    with control._connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM login_events ORDER BY id")]


def test_a_successful_login_is_recorded_against_the_account(tmp_path):
    control = _control(tmp_path)
    control.authenticate_limited("root", "secret", "10.0.0.5")

    events = _events(control)
    assert [(event["username"], event["outcome"]) for event in events] == [("root", "success")]
    assert events[0]["user_id"] == control.authenticate("root", "secret")["id"]
    # The origin is kept as a digest, never as an address: it is here to count
    # distinct sources, not to say where anybody was sitting.
    assert events[0]["client_digest"] != "10.0.0.5"
    assert len(events[0]["client_digest"]) == 64


def test_a_wrong_pin_is_recorded_against_the_account_it_was_aimed_at(tmp_path):
    control = _control(tmp_path)
    control.authenticate_limited("root", "wrong", "10.0.0.5")

    events = _events(control)
    assert [(event["username"], event["outcome"]) for event in events] == [("root", "failure")]


def test_a_name_matching_no_account_is_recorded_without_storing_what_was_typed(tmp_path):
    """A PIN typed into the username box must not end up in the clear.

    That is the commonest way to produce an unrecognised name, and this file
    otherwise holds only hashes. The attempt is still counted -- the outcome and
    the origin digest are what the activity page reports -- but the text is not
    kept.
    """

    control = _control(tmp_path)
    control.authenticate_limited("8675309-mistyped-pin", "whatever", "10.0.0.5")

    events = _events(control)
    assert [(event["user_id"], event["username"], event["outcome"]) for event in events] == [
        (None, None, "failure")
    ]
    assert b"8675309-mistyped-pin" not in (tmp_path / "accesscontrol.db").read_bytes()


def test_an_attempt_refused_by_the_lockout_is_recorded_as_blocked(tmp_path):
    control = _control(tmp_path)
    for _ in range(LOGIN_FAILURE_LIMIT):
        control.authenticate_limited("root", "wrong", "10.0.0.5")
    # Refused before the PIN is even considered -- and still attributed, so the
    # page can name the account somebody is being kept out of.
    user, retry_after = control.authenticate_limited("root", "secret", "10.0.0.5")
    assert user is None and retry_after > 0

    outcomes = [event["outcome"] for event in _events(control)]
    assert outcomes == ["failure"] * LOGIN_FAILURE_LIMIT + ["blocked"]
    assert _events(control)[-1]["username"] == "root"


def test_an_oversized_pin_still_names_the_account_it_was_aimed_at(tmp_path):
    """The two input bounds answer different questions.

    An overlong PIN is refused before any hash is verified, but the account it
    was aimed at exists and is worth naming -- recording it as an attempt on an
    unrecognised name would hide somebody being worked on.
    """

    control = _control(tmp_path)
    assert control.authenticate_limited("root", "p" * (MAX_PIN_CHARS + 1), "10.0.0.5") == (None, 0)
    # An overlong *username* is still unattributed: it cannot match an account,
    # and looking it up is the search the bound exists to prevent.
    control.authenticate_limited("u" * (MAX_USERNAME_CHARS + 1), "1234", "10.0.0.5")

    events = _events(control)
    assert [(event["username"], event["outcome"]) for event in events] == [
        ("root", "failure"),
        (None, "failure"),
    ]


def test_origins_are_keyed_so_a_stored_digest_cannot_be_looked_up(tmp_path):
    """An unkeyed digest of an address is the address, for a small enough space."""

    control = _control(tmp_path)
    control.authenticate_limited("root", "secret", "10.0.0.5")
    stored = _events(control)[0]["client_digest"]

    assert stored != hashlib.sha256(b"10.0.0.5").hexdigest()
    assert stored == control._origin_digest("10.0.0.5")
    # The same address digests differently under another deployment's secret, so
    # exported history cannot be correlated between installations either.
    other = AccessControl(tmp_path / "other.db")
    other.ensure_schema(initial_username="root", initial_pin="secret")
    assert other._origin_digest("10.0.0.5") != stored


def test_the_report_counts_each_account_and_still_lists_the_quiet_ones(tmp_path):
    control = _control(tmp_path)
    control.save_user(None, "operator", "2468", "editor")
    control.save_user(None, "quiet", "1357", "viewer")
    control.authenticate_limited("root", "secret", "10.0.0.5")
    control.authenticate_limited("operator", "2468", "10.0.0.6")
    control.authenticate_limited("operator", "2468", "10.0.0.7")
    control.authenticate_limited("operator", "nope", "10.0.0.7")
    control.record_change({"id": 1, "username": "root"}, "POST /developer/api/sync")

    report = control.activity_report(30)
    users = {user["username"]: user for user in report["users"]}

    assert users["operator"]["logins"] == 2
    assert users["operator"]["failed_logins"] == 1
    assert users["operator"]["distinct_sources"] == 2
    assert users["operator"]["active_days"] == 1
    # Two sign-ins on one day is a different fact from two sign-ins on two days.
    assert users["operator"]["logins_per_active_day"] == 2.0
    assert users["root"]["changes"] == 1

    # An account nobody uses is the reason to open this page, so it is present
    # rather than absent for having no rows to join against.
    assert users["quiet"]["logins"] == 0
    assert users["quiet"]["last_login"] is None

    summary = report["summary"]
    assert summary["successful_logins"] == 3
    assert summary["failed_logins"] == 1
    assert summary["users_seen"] == 2
    assert summary["accounts"] == 3
    assert summary["never_signed_in"] == 1
    assert summary["changes"] == 1
    assert report["top_actions"][0]["action"] == "POST /developer/api/sync"
    assert [event["outcome"] for event in report["recent_logins"]][0] == "failure"


def test_the_window_bounds_what_is_counted(tmp_path):
    control = _control(tmp_path)
    control.authenticate_limited("root", "secret", "10.0.0.5")
    with control._connect() as conn:
        conn.execute("UPDATE login_events SET occurred_at = datetime('now', '-40 days')")

    assert control.activity_report(30)["summary"]["successful_logins"] == 0
    assert control.activity_report(90)["summary"]["successful_logins"] == 1
    # The account is still listed, with its last sign-in from outside the window:
    # "nothing this month, last seen in June" is the useful reading.
    older = {user["username"]: user for user in control.activity_report(30)["users"]}["root"]
    assert older["logins"] == 0 and older["last_login"] is not None


def test_the_daily_series_covers_every_day_including_the_quiet_ones(tmp_path):
    control = _control(tmp_path)
    control.authenticate_limited("root", "secret", "10.0.0.5")

    report = control.activity_report(7)
    days = report["daily"]
    assert len(days) == 7
    assert [day["day"] for day in days] == sorted(day["day"] for day in days)
    assert days[-1]["day"] == report["window"]["last_day"]
    assert days[-1]["logins"] == 1
    assert [day["logins"] for day in days[:-1]] == [0] * 6
    # Hour buckets keep their own date, so the page can convert each one at the
    # offset that was in force that day rather than at today's.
    assert [bucket["day"] for bucket in report["hourly"]] == [report["window"]["last_day"]]
    assert sum(bucket["logins"] for bucket in report["hourly"]) == 1
    assert 0 <= report["hourly"][0]["hour"] < 24
    # Quarter-hour buckets, so a zone offset by part of an hour still converts
    # into the local hour the sign-in actually happened in.
    assert report["hourly"][0]["minute"] in (0, 15, 30, 45)


@pytest.mark.parametrize("days", [0, -1, ACTIVITY_MAX_DAYS + 1, "soon", None])
def test_an_unusable_window_is_refused(tmp_path, days):
    control = _control(tmp_path)
    with pytest.raises(ValueError):
        control.activity_report(days)


def test_history_outlives_the_account_it_belongs_to(tmp_path):
    """Removing a user must not quietly rewrite what the deployment recorded."""

    control = _control(tmp_path)
    control.save_user(None, "leaver", "2468", "editor")
    control.authenticate_limited("leaver", "2468", "10.0.0.6")
    leaver = control.authenticate("leaver", "2468")
    control.delete_user(leaver["id"], current_user_id=999)

    report = control.activity_report(30)
    assert [user["username"] for user in report["users"]] == ["root"]
    assert report["summary"]["logins_by_removed_accounts"] == 1
    assert report["summary"]["successful_logins"] == 1
    # Counted against the accounts that exist, so the page cannot end up saying
    # "1 of 0 accounts used" -- the departed account's sign-in is reported above
    # instead.
    assert report["summary"]["users_seen"] == 0
    assert report["summary"]["users_seen"] <= report["summary"]["accounts"]


def test_the_history_is_capped_and_sheds_unrecognised_attempts_first(tmp_path, monkeypatch):
    """A flood of invented names must not push real sign-ins out of the file."""

    monkeypatch.setattr(access_control_module, "LOGIN_EVENT_ROW_CAP", 3)
    monkeypatch.setattr(access_control_module, "LOGIN_EVENT_PRUNE_SLACK", 0)
    control = _control(tmp_path)
    control.authenticate_limited("root", "secret", "10.0.0.5")
    control.authenticate_limited("root", "secret", "10.0.0.5")
    # Each attempt uses a fresh name and origin so the limiter's own lockouts do
    # not stop the flood before the cap is reached.
    for index in range(8):
        control.authenticate_limited(f"ghost-{index}", "x", f"10.9.9.{index}")

    events = _events(control)
    assert len(events) == 3
    assert sum(1 for event in events if event["username"] == "root") == 2


def test_a_flood_against_a_real_account_cannot_evict_its_sign_ins(tmp_path, monkeypatch):
    """Attributed attempts are cheap to manufacture too.

    Aiming at a name that exists puts a real user_id on every wrong PIN, and a
    locked-out attempt is refused before any hash is checked -- so an outsider
    can produce attributed rows about as fast as anonymous ones. Sign-ins are
    what "last signed in" and "never signed in" are read from, so they outrank
    both.
    """

    monkeypatch.setattr(access_control_module, "LOGIN_EVENT_ROW_CAP", 3)
    monkeypatch.setattr(access_control_module, "LOGIN_EVENT_PRUNE_SLACK", 0)
    control = _control(tmp_path)
    control.authenticate_limited("root", "secret", "10.0.0.5")
    for index in range(20):
        control.authenticate_limited("root", "wrong", f"10.9.9.{index}")

    events = _events(control)
    assert len(events) == 3
    successes = [event for event in events if event["outcome"] == "success"]
    assert len(successes) == 1
    # And the account is still reported as having signed in, which is the fact
    # the flood was able to erase before.
    assert control.activity_report(30)["users"][0]["last_login"] == successes[0]["occurred_at"]


def test_an_existing_access_database_gains_the_history_table(tmp_path):
    """Deployments upgrade in place: the file predates this feature."""

    path = tmp_path / "legacy-accesscontrol.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, role TEXT,
            credential_version INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT)""")
        conn.execute("""CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY, user_id INTEGER, username TEXT, action TEXT, changed_at TEXT)""")

    control = AccessControl(path)
    control.ensure_schema()
    control.save_user(None, "root", "secret", "admin")
    control.authenticate_limited("root", "secret", "10.0.0.5")

    assert control.activity_report(30)["summary"]["successful_logins"] == 1


def test_an_account_older_than_the_history_is_not_reported_as_never_used(tmp_path):
    """Upgrading starts the history empty; that is not evidence of disuse.

    Nothing can reconstruct last year's sign-ins, so on the first day after this
    feature ships every existing account has none recorded -- including the ones
    in use every shift. Reporting those as "never signed in" would be false
    about real people, and an administrator might act on it by removing the
    account.
    """

    control = _control(tmp_path)
    control.save_user(None, "veteran", "2468", "editor")
    with control._connect() as conn:
        conn.execute("UPDATE users SET created_at = datetime('now','-400 days') WHERE username='veteran'")
    # The history begins here, after the veteran account already existed.
    control.authenticate_limited("root", "secret", "10.0.0.5")
    control.save_user(None, "newcomer", "1357", "viewer")

    users = {user["username"]: user for user in control.activity_report(30)["users"]}
    assert users["veteran"]["last_login"] is None
    assert users["veteran"]["tracked_since_created"] is False
    # Created once the history was already running, so an absence of sign-ins in
    # it really does mean the account has never been used.
    assert users["newcomer"]["last_login"] is None
    assert users["newcomer"]["tracked_since_created"] is True


def test_with_no_history_at_all_no_account_claims_to_be_unused(tmp_path):
    control = _control(tmp_path)
    report = control.activity_report(30)
    assert report["retention"]["first_event"] is None
    assert [user["tracked_since_created"] for user in report["users"]] == [False]


def test_changes_by_a_removed_account_are_reported_separately(tmp_path):
    """The audit-trail twin of logins_by_removed_accounts.

    The per-account table is built from the accounts that exist, so an author
    whose account has since been deleted appears in no row. Counted among the
    authors as well, the page would report more authors than there are accounts
    and a Changes column that does not add up to the total above it.
    """

    control = _control(tmp_path)
    control.save_user(None, "leaver", "2468", "editor")
    leaver = control.authenticate("leaver", "2468")
    control.record_change(leaver, "POST /life-data-analysis/api/dispositions/save")
    control.record_change(control.authenticate("root", "secret"), "POST /developer/api/sync")
    control.delete_user(leaver["id"], current_user_id=999)

    report = control.activity_report(30)
    summary = report["summary"]
    assert summary["changes"] == 2
    assert summary["change_authors"] == 1
    assert summary["changes_by_removed_accounts"] == 1
    assert summary["change_authors"] <= summary["accounts"]
    # The table plus what is reported as departed accounts for the total.
    in_table = sum(user["changes"] for user in report["users"])
    assert in_table + summary["changes_by_removed_accounts"] == summary["changes"]


def test_an_existing_client_lockout_survives_the_keyed_digest_upgrade(tmp_path):
    """The limiter keeps the key it has always used.

    Its rows are filed under a plain digest of the client. Rekeying them when
    the history moved to a keyed digest would have handed every client already
    locked out for spraying guesses a fresh budget on the upgrade, and split one
    client's attempts across two keys for as long as a rolling deployment ran
    two versions at once.
    """

    control = _control(tmp_path)
    client = "10.9.9.9"
    legacy_scope = "client:" + hashlib.sha256(client.encode("utf-8")).hexdigest()
    with control._connect() as conn:
        conn.execute(
            """INSERT INTO login_attempts(scope_key,failure_count,window_started,locked_until)
               VALUES (?,?,?,?)""",
            (legacy_scope, LOGIN_FAILURE_LIMIT, time.time(), time.time() + 600),
        )

    user, retry_after = control.authenticate_limited("root", "secret", client)
    assert user is None and retry_after > 0
    # The history still records that attempt under the keyed digest, which is
    # what the two digests being separate is for.
    assert _events(control)[-1]["client_digest"] == control._origin_digest(client)


def test_retention_reports_where_each_kind_of_attempt_still_reaches_back_to(tmp_path):
    """The cap drops refusals before sign-ins, so one date cannot describe both.

    Saying only "history begins X" would tell an administrator that a window
    inside that range is counted in full, when the refusals in it may have been
    evicted to keep the sign-ins.
    """

    control = _control(tmp_path)
    control.authenticate_limited("root", "secret", "10.0.0.5")
    control.authenticate_limited("root", "wrong", "10.0.0.5")
    with control._connect() as conn:
        conn.execute("UPDATE login_events SET occurred_at = datetime('now','-300 days') WHERE outcome='success'")

    retention = control.activity_report(30)["retention"]
    tiers = {tier["kind"]: tier for tier in retention["tiers"]}
    assert tiers["success"]["first_kept"] < tiers["attributed_refusal"]["first_kept"]
    assert retention["first_event"] == tiers["success"]["first_kept"]
    assert retention["capped"] is False
    assert retention["pruned"] is False


def test_the_three_eviction_tiers_each_report_their_own_cutoff(tmp_path, monkeypatch):
    """Refusals are two tiers, not one, and they do not run out together.

    Attempts on unrecognised names are dropped before failures against a real
    account, so a single "refusals complete since" date would be the attributed
    one and would overstate how far back the unrecognised-name history reaches
    -- which is the count somebody watching for an attack is reading.
    """

    monkeypatch.setattr(access_control_module, "LOGIN_EVENT_ROW_CAP", 6)
    monkeypatch.setattr(access_control_module, "LOGIN_EVENT_PRUNE_SLACK", 0)
    control = _control(tmp_path)
    control.authenticate_limited("root", "secret", "10.0.0.5")
    # Attributed refusals, then a flood of unrecognised names to push the table
    # past the cap. The unattributed tier is evicted first, so it ends up with
    # the latest cutoff of the three.
    for index in range(3):
        control.authenticate_limited("root", "wrong", f"10.0.1.{index}")
    for index in range(9):
        control.authenticate_limited(f"ghost-{index}", "x", f"10.9.9.{index}")

    retention = control.activity_report(30)["retention"]
    tiers = {tier["kind"]: tier for tier in retention["tiers"]}
    assert retention["pruned"] is True
    assert retention["rows_dropped"] > 0
    assert retention["last_pruned_at"] is not None
    assert (
        tiers["success"]["first_kept"]
        <= tiers["attributed_refusal"]["first_kept"]
        <= tiers["unattributed_refusal"]["first_kept"]
    )
    # The sign-in survived the flood, and the unrecognised-name history is the
    # one that has been cut back.
    assert _events(control)[0]["outcome"] == "success"


def test_reaching_the_cap_is_not_reported_as_having_dropped_anything(tmp_path, monkeypatch):
    """Trimming starts a slack's worth of rows past the cap, not at it.

    In between, the history is full but complete. Saying attempts are being
    dropped there would tell an administrator their counts are short when every
    one of them is exact -- and if traffic stopped, it would say so forever.
    """

    monkeypatch.setattr(access_control_module, "LOGIN_EVENT_ROW_CAP", 3)
    monkeypatch.setattr(access_control_module, "LOGIN_EVENT_PRUNE_SLACK", 10)
    control = _control(tmp_path)
    for index in range(5):
        control.authenticate_limited("root", "secret", f"10.0.0.{index}")

    retention = control.activity_report(30)["retention"]
    assert retention["login_events"] == 5
    assert retention["capped"] is True
    assert retention["pruned"] is False
    assert retention["rows_dropped"] == 0


def test_a_tier_trimmed_away_entirely_still_reports_that_it_was(tmp_path, monkeypatch):
    """"None recorded" and "all of them dropped" must not look the same.

    Sign-ins are kept ahead of everything else, so a history full of them can
    evict every unrecognised-name attempt. With only the surviving rows to go
    on, the page would show that count as zero with nothing to say attempts had
    been made -- which reads as "nobody tried".
    """

    monkeypatch.setattr(access_control_module, "LOGIN_EVENT_ROW_CAP", 4)
    monkeypatch.setattr(access_control_module, "LOGIN_EVENT_PRUNE_SLACK", 0)
    control = _control(tmp_path)
    for index in range(3):
        control.authenticate_limited(f"ghost-{index}", "x", f"10.9.9.{index}")
    for index in range(6):
        control.authenticate_limited("root", "secret", f"10.0.0.{index}")

    tiers = {tier["kind"]: tier for tier in control.activity_report(30)["retention"]["tiers"]}
    assert tiers["unattributed_refusal"]["first_kept"] is None
    assert tiers["unattributed_refusal"]["dropped"] == 3
    assert tiers["success"]["first_kept"] is not None


def test_the_pruning_record_gains_its_per_tier_columns_in_place(tmp_path):
    """A database written by the version before these columns existed."""

    control = _control(tmp_path)
    with control._connect() as conn:
        conn.execute("DROP TABLE login_event_pruning")
        conn.execute("""CREATE TABLE login_event_pruning (
            id INTEGER PRIMARY KEY CHECK(id = 1), last_pruned_at TEXT NOT NULL,
            rows_dropped INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("INSERT INTO login_event_pruning(id, last_pruned_at, rows_dropped) VALUES (1, '2026-01-01 00:00:00', 7)")

    control.ensure_schema()
    retention = control.activity_report(30)["retention"]
    assert retention["pruned"] is True
    assert retention["rows_dropped"] == 7
    assert [tier["dropped"] for tier in retention["tiers"]] == [0, 0, 0]


def test_a_future_dated_row_is_outside_the_window_everywhere(tmp_path):
    """A clock put back leaves rows dated after today.

    Every section has to agree about them. With only a lower bound the summary
    and the tables counted such a row while the daily series -- which stops at
    today -- did not, so the page contradicted itself about days the window does
    not even claim to cover.
    """

    control = _control(tmp_path)
    control.authenticate_limited("root", "secret", "10.0.0.5")
    control.record_change(control.authenticate("root", "secret"), "POST /developer/api/sync")
    with control._connect() as conn:
        conn.execute("UPDATE login_events SET occurred_at = datetime('now', '+3 days')")
        conn.execute("UPDATE audit_log SET changed_at = datetime('now', '+3 days')")

    report = control.activity_report(30)
    summary = report["summary"]
    assert summary["successful_logins"] == 0
    assert summary["changes"] == 0
    assert summary["users_seen"] == 0
    assert report["recent_logins"] == []
    assert report["recent_changes"] == []
    assert report["hourly"] == []
    assert report["top_actions"] == []
    assert sum(day["logins"] for day in report["daily"]) == 0
    assert sum(user["logins"] + user["changes"] for user in report["users"]) == 0


def test_eviction_follows_the_clock_not_the_insertion_order(tmp_path, monkeypatch):
    """Under a clock correction, row order and time order disagree.

    Retention reports the oldest surviving timestamp as a clean cutoff, so
    eviction has to remove the oldest *timestamps* -- otherwise an attempt newer
    than the cutoff could be gone while the page called that period complete.
    """

    monkeypatch.setattr(access_control_module, "LOGIN_EVENT_ROW_CAP", 2)
    monkeypatch.setattr(access_control_module, "LOGIN_EVENT_PRUNE_SLACK", 0)
    control = _control(tmp_path)
    with control._connect() as conn:
        # Written in this order, but the middle one is the oldest by the clock.
        for stamp in ("2026-03-01 09:00:00", "2026-01-01 09:00:00", "2026-05-01 09:00:00"):
            conn.execute(
                """INSERT INTO login_events(user_id,username,outcome,client_digest,occurred_at)
                   VALUES (1,'root','success','d',?)""",
                (stamp,),
            )
    # One more attempt takes the table past the cap and triggers the trim.
    control.authenticate_limited("root", "secret", "10.0.0.5")

    kept = [event["occurred_at"] for event in _events(control)]
    assert "2026-01-01 09:00:00" not in kept
    tiers = {tier["kind"]: tier for tier in control.activity_report(365)["retention"]["tiers"]}
    # The reported cutoff is now the oldest thing actually kept, with nothing
    # newer than it missing.
    assert tiers["success"]["first_kept"] == min(kept)


def test_refusals_aimed_at_a_removed_account_are_reported_separately(tmp_path):
    control = _control(tmp_path)
    control.save_user(None, "leaver", "2468", "editor")
    leaver = control.authenticate("leaver", "2468")
    control.authenticate_limited("leaver", "wrong", "10.0.0.6")
    control.delete_user(leaver["id"], current_user_id=999)

    summary = control.activity_report(30)["summary"]
    assert summary["failed_logins"] == 1
    assert summary["refusals_by_removed_accounts"] == 1
    # Nothing in the table accounts for it, which is why it is named above.
    assert sum(user["failed_logins"] for user in control.activity_report(30)["users"]) == 0


def test_drops_from_before_per_tier_counting_are_unclassified_not_zero(tmp_path):
    """Migrating cannot invent a classification for rows already gone."""

    control = _control(tmp_path)
    with control._connect() as conn:
        conn.execute("DROP TABLE login_event_pruning")
        conn.execute("""CREATE TABLE login_event_pruning (
            id INTEGER PRIMARY KEY CHECK(id = 1), last_pruned_at TEXT NOT NULL,
            rows_dropped INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("INSERT INTO login_event_pruning(id,last_pruned_at,rows_dropped) VALUES (1,'2026-01-01 00:00:00',12)")

    control.ensure_schema()
    retention = control.activity_report(30)["retention"]
    assert retention["rows_dropped"] == 12
    assert retention["dropped_unclassified"] == 12
    assert [tier["dropped"] for tier in retention["tiers"]] == [0, 0, 0]


def test_a_future_dated_sign_in_is_not_reported_as_the_last_one(tmp_path):
    """"Last signed in" reaches back forever, but not forward.

    The all-time lookup is deliberately unbounded at the start -- the column
    means "ever", not "in this window". Left unbounded at the end too, a row the
    clock produced by being put back showed up as a sign-in from the future.
    """

    control = _control(tmp_path)
    control.authenticate_limited("root", "secret", "10.0.0.5")
    real = _events(control)[0]["occurred_at"]
    with control._connect() as conn:
        conn.execute(
            """INSERT INTO login_events(user_id,username,outcome,client_digest,occurred_at)
               VALUES ((SELECT id FROM users WHERE username='root'),'root','success','d',
                       datetime('now','+5 days'))"""
        )

    user = control.activity_report(30)["users"][0]
    assert user["last_login"] == real
    assert user["total_logins"] == 1
