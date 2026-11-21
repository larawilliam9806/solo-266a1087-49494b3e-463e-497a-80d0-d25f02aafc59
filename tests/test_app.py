import io
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from examapp import db, engine  # noqa: E402
from examapp.app import create_app, read_run  # noqa: E402


FORM_HEADER = "题号,选项字母,标准题码,标准选项\n"
ANSWER_HEADER = "卡号,题号,涂选字母,实扫题码\n"


def form_csv(rows):
    return FORM_HEADER + "".join(f"{q},{l},{c},{o}\n" for q, l, c, o in rows)


def answer_csv(rows):
    return ANSWER_HEADER + "".join(
        f"{card},{q},{letter},{code}\n" for card, q, letter, code in rows
    )


@pytest.fixture()
def app(tmp_path):
    os.environ["EXAM_SYNC_JOBS"] = "1"
    return create_app(str(tmp_path / "test.db"), sync_jobs=True)


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, path, csv_text, name=None, filename="upload.csv"):
    data = {"file": (io.BytesIO(csv_text.encode("utf-8")), filename)}
    if name is not None:
        data["name"] = name
    response = client.post(
        path, data=data, content_type="multipart/form-data", follow_redirects=True
    )
    assert response.status_code == 200
    return response


def current_run(app):
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            return read_run(conn, db.active_run_id(conn))
        finally:
            conn.close()


def stats_of(run):
    return {
        code: {
            "counted": s["counted"],
            "blank": s["blank"],
            "flagged": s["flagged"],
            "options": dict(s["options"]),
        }
        for code, s in run["stats"].items()
    }


A_FORM = form_csv([
    ("1", "A", "Q1", "A"), ("1", "B", "Q1", "B"),
    ("2", "A", "Q2", "A"), ("2", "B", "Q2", "B"),
])


# --------------------------------------------------------------------------
# Parsing / validation
# --------------------------------------------------------------------------

def test_form_mapping_requires_unique_standard_code():
    bad = form_csv([
        ("1", "A", "Q1", "A"), ("1", "B", "Q1", "B"),
        ("2", "A", "Q1", "A"), ("2", "B", "Q1", "B"),
    ])
    with pytest.raises(engine.ValidationError):
        engine.parse_form_csv(bad.encode())


def test_form_mapping_option_letters_must_be_distinct():
    bad = form_csv([("1", "A", "Q1", "A"), ("1", "B", "Q1", "A")])
    with pytest.raises(engine.ValidationError):
        engine.parse_form_csv(bad.encode())


def test_answer_csv_column_names_are_flexible():
    rows = engine.parse_answer_csv(
        "card_no,qno,letter,scanned\nC1,1,A,Q1\n".encode()
    )
    assert rows == [{"card_no": "C1", "q_no": "1", "letter": "A", "scanned_code": "Q1"}]


def test_duplicate_card_row_is_rejected():
    with pytest.raises(engine.ValidationError):
        engine.parse_answer_csv(
            (ANSWER_HEADER + "C1,1,A,Q1\nC1,1,B,Q1\n").encode()
        )


# --------------------------------------------------------------------------
# Core statistics
# --------------------------------------------------------------------------

def test_counts_options_and_blanks(client, app):
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2"),
        ("C2", "1", "B", ""), ("C2", "2", "", ""),
    ]))
    stats = stats_of(current_run(app))
    assert stats["Q1"] == {"counted": 2, "blank": 0, "flagged": 0,
                           "options": {"A": 1, "B": 1}}
    assert stats["Q2"] == {"counted": 1, "blank": 1, "flagged": 0,
                           "options": {"A": 0, "B": 1}}


def test_card_without_compatible_form_is_listed_separately(client, app):
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2"),
        # wrong scanned code -> incompatible
        ("C2", "1", "A", "Q9"), ("C2", "2", "B", "Q2"),
        # wrong question count -> incompatible
        ("C3", "1", "A", "Q1"),
    ]))
    run = current_run(app)
    assert [u["card_no"] for u in run["unmatched"]] == ["C2", "C3"]
    assert stats_of(run)["Q1"]["counted"] == 1


def test_blank_scanned_code_and_letter_still_match(client, app):
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "", ""), ("C1", "2", "", ""),
    ]))
    run = current_run(app)
    assert run["unmatched"] == []
    assert stats_of(run)["Q1"]["blank"] == 1


def test_two_compatible_forms_agreeing_are_counted(client, app):
    # Same numbering, different standard codes would break compatibility by
    # scanned code; keep codes identical but options reversed per form.
    form_b = form_csv([
        ("1", "A", "Q1", "B"), ("1", "B", "Q1", "A"),
        ("2", "A", "Q2", "A"), ("2", "B", "Q2", "B"),
    ])
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/forms", form_b, name="B卷")
    # letter A on form A -> Q1=A ; on form B -> Q1=B : divergence
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "A", "Q2"),
    ]))
    run = current_run(app)
    assert stats_of(run)["Q1"]["flagged"] == 1
    assert stats_of(run)["Q2"]["counted"] == 1


def test_forms_agreeing_after_normalisation_are_counted(client, app):
    form_b = form_csv([
        ("1", "A", "Q1", "A"), ("1", "B", "Q1", "B"),
        ("2", "A", "Q2", "A"), ("2", "B", "Q2", "B"),
    ])
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/forms", form_b, name="B卷-同构")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2"),
    ]))
    run = current_run(app)
    stats = stats_of(run)
    assert stats["Q1"]["counted"] == 1
    assert stats["Q1"]["flagged"] == 0
    assert stats["Q1"]["options"]["A"] == 1


def test_question_missing_from_one_compatible_form_is_flagged(client, app):
    # Card rows q1,q2.  Both forms are compatible (same numbering, blank
    # scanned codes), but form B maps q2 to a different standard code, so Q2
    # is only printed by form A and Q2B only by form B.
    form_b = form_csv([
        ("1", "A", "Q1", "A"), ("1", "B", "Q1", "B"),
        ("2", "A", "Q2B", "A"), ("2", "B", "Q2B", "B"),
    ])
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/forms", form_b, name="B卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "A", ""),
    ]))
    run = current_run(app)
    stats = stats_of(run)
    assert run["unmatched"] == []
    assert stats["Q1"]["counted"] == 1          # both forms print it and agree
    assert stats["Q2"]["flagged"] == 1          # only one form prints it
    assert stats["Q2B"]["flagged"] == 1


def test_question_absent_from_all_compatible_forms_is_skipped(client, app):
    # Form C defines Q3 globally, but the card is only compatible with A/B.
    form_c = form_csv([
        ("1", "A", "Q1", "A"), ("1", "B", "Q1", "B"),
        ("2", "A", "Q2", "A"), ("2", "B", "Q2", "B"),
        ("3", "A", "Q3", "A"), ("3", "B", "Q3", "B"),
    ])
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/forms", form_c, name="C卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "B", ""),
    ]))
    run = current_run(app)
    assert run["unmatched"] == []
    stats = stats_of(run)
    assert stats["Q3"] == {"counted": 0, "blank": 0, "flagged": 0,
                           "options": {"A": 0, "B": 0}}
    assert stats["Q1"]["counted"] == 1


# --------------------------------------------------------------------------
# Incremental corrections
# --------------------------------------------------------------------------

def test_new_form_rejudges_previously_unmatched_card(client, app):
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2"),
    ]))
    run = current_run(app)
    assert [u["card_no"] for u in run["unmatched"]] == ["C1"]

    upload(client, "/forms", A_FORM, name="A卷")
    run = current_run(app)
    assert run["unmatched"] == []
    assert stats_of(run)["Q1"]["counted"] == 1


def test_code_correction_redistributes_counts(client, app):
    upload(client, "/forms", A_FORM, name="A卷")
    # No scanned codes, so remapping does not disturb compatibility.
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "B", ""),
    ]))
    assert stats_of(current_run(app))["Q1"]["counted"] == 1

    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        form_id = conn.execute("SELECT id FROM forms").fetchone()["id"]
        conn.close()
    client.post("/forms/correct", data={
        "form_id": str(form_id), "q_no": "1", "new_code": "Q1X",
    }, follow_redirects=True)
    stats = stats_of(current_run(app))
    assert "Q1" not in stats
    assert stats["Q1X"]["counted"] == 1
    assert stats["Q1X"]["options"] == {"A": 1, "B": 0}
    assert stats["Q2"]["counted"] == 1


def test_code_correction_unmatches_cards_with_old_scanned_code(client, app):
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2"),
    ]))
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        form_id = conn.execute("SELECT id FROM forms").fetchone()["id"]
        conn.close()
    client.post("/forms/correct", data={
        "form_id": str(form_id), "q_no": "1", "new_code": "Q1X",
    }, follow_redirects=True)
    run = current_run(app)
    # The physical scan still says Q1, so the card no longer fits any form.
    assert [u["card_no"] for u in run["unmatched"]] == ["C1"]
    assert stats_of(run)["Q1X"]["counted"] == 0


def test_stale_counts_withdrawn_when_form_removed_by_replacement(client, app):
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2"),
    ]))
    # Replace "A卷" with a form that has a different standard code: the card no
    # longer has a compatible form, so the previous counts must disappear.
    replacement = form_csv([
        ("1", "A", "X1", "A"), ("1", "B", "X1", "B"),
        ("2", "A", "X2", "A"), ("2", "B", "X2", "B"),
    ])
    upload(client, "/forms", replacement, name="A卷")
    run = current_run(app)
    assert [u["card_no"] for u in run["unmatched"]] == ["C1"]
    assert all(s["counted"] == 0 for s in stats_of(run).values())


def test_review_decision_overrides_flagged_cell(client, app):
    form_b = form_csv([
        ("1", "A", "Q1", "B"), ("1", "B", "Q1", "A"),
        ("2", "A", "Q2", "A"), ("2", "B", "Q2", "B"),
    ])
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/forms", form_b, name="B卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "A", "Q2"),
    ]))
    assert stats_of(current_run(app))["Q1"]["flagged"] == 1

    client.post("/review", data={
        "card_no": "C1", "std_code": "Q1", "resolution": "opt:A", "note": "以A卷为准",
    }, follow_redirects=True)
    stats = stats_of(current_run(app))["Q1"]
    assert stats == {"counted": 1, "blank": 0, "flagged": 0,
                     "options": {"A": 1, "B": 0}}


# --------------------------------------------------------------------------
# Transactionality & history
# --------------------------------------------------------------------------

def test_failed_save_keeps_previous_state(app):
    client = app.test_client()
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2"),
    ]))
    before = stats_of(current_run(app))

    # Force the next job to fail mid-transaction.
    from examapp import app as app_module
    original = app_module._do_upload_form

    def boom(conn, payload):
        original(conn, payload)
        raise RuntimeError("disk full")

    app_module._do_upload_form = boom
    try:
        bad_form = form_csv([("1", "A", "Z1", "A"), ("1", "B", "Z1", "B"),
                             ("2", "A", "Z2", "A"), ("2", "B", "Z2", "B")])
        resp = upload(client, "/forms", bad_form, name="Z卷")
        assert "失败" in resp.get_data(as_text=True)
    finally:
        app_module._do_upload_form = original

    assert stats_of(current_run(app)) == before
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        names = {r["name"] for r in conn.execute("SELECT name FROM forms")}
        conn.close()
    assert "Z卷" not in names
    assert "A卷" in names


def test_previous_snapshot_remains_readable(client, app):
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2"),
    ]))
    first = current_run(app)["id"]
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", "Q1"), ("C1", "2", "B", "Q2"),
    ]))
    second = current_run(app)["id"]
    assert first != second
    resp = client.get(f"/runs/{first}")
    assert resp.status_code == 200
    assert "历史只读" in resp.get_data(as_text=True)
    assert stats_of(read_run_by_id(app, first))["Q1"]["options"]["A"] == 1


def test_gbk_upload_is_accepted(client, app):
    upload(client, "/forms", A_FORM, name="A卷")
    gbk = (ANSWER_HEADER + "C1,1,A,Q1\nC1,2,B,Q2\n").encode("gb18030")
    response = client.post(
        "/answers", data={"file": (io.BytesIO(gbk), "ans.csv")},
        content_type="multipart/form-data", follow_redirects=True,
    )
    assert response.status_code == 200
    assert stats_of(current_run(app))["Q1"]["counted"] == 1


def test_invalid_correction_rolls_back(client, app):
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "B", ""),
    ]))
    before = stats_of(current_run(app))
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        form_id = conn.execute("SELECT id FROM forms").fetchone()["id"]
        conn.close()
    # Q2 already exists in this form: the correction would duplicate a code.
    client.post("/forms/correct", data={
        "form_id": str(form_id), "q_no": "1", "new_code": "Q2",
    }, follow_redirects=True)
    assert stats_of(current_run(app)) == before


def test_stale_review_does_not_override_consistent_recount(client, app):
    form_b = form_csv([
        ("1", "A", "Q1", "B"), ("1", "B", "Q1", "A"),
        ("2", "A", "Q2", "A"), ("2", "B", "Q2", "B"),
    ])
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/forms", form_b, name="B卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "A", ""),
    ]))
    assert stats_of(current_run(app))["Q1"]["flagged"] == 1
    client.post("/review", data={
        "card_no": "C1", "std_code": "Q1", "resolution": "skip", "note": "",
    }, follow_redirects=True)
    assert stats_of(current_run(app))["Q1"]["counted"] == 0

    # Remove the divergent form; the cell now computes consistently, so the
    # old "skip" conclusion must stop suppressing it.
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        form_b_id = conn.execute(
            "SELECT id FROM forms WHERE name = 'B卷'"
        ).fetchone()["id"]
        conn.close()
    # There is no delete endpoint; replace B卷 with a copy of A卷 instead.
    upload(client, "/forms", A_FORM, name="B卷")
    stats = stats_of(current_run(app))["Q1"]
    assert stats["counted"] == 1


def test_old_stats_readable_while_recomputing(app, monkeypatch):
    import threading
    import time
    from examapp import app as app_module

    client = app.test_client()
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2"),
    ]))
    first = current_run(app)["id"]

    started = threading.Event()
    release = threading.Event()
    original = app_module._do_upload_answers

    def slow(conn, payload):
        started.set()
        release.wait(timeout=10)
        original(conn, payload)

    app.worker.sync = False
    monkeypatch.setattr(app_module, "_do_upload_answers", slow)
    try:
        app.worker.submit("upload_answers", {
            "csv": answer_csv([("C1", "1", "B", "Q1"), ("C1", "2", "B", "Q2")]),
        })
        assert started.wait(timeout=5)
        # While the worker is mid-recompute the previous snapshot is served.
        with app.app_context():
            conn = db.connect(app.config["DB_PATH"])
            assert db.active_run_id(conn) == first
            assert read_run(conn, first)["stats"]["Q1"]["options"]["A"] == 1
            conn.close()
        page = client.get(f"/runs/{first}")
        assert page.status_code == 200
        assert client.get("/api/status").get_json()["recomputing"] is True
    finally:
        release.set()
        for _ in range(100):
            with app.app_context():
                conn = db.connect(app.config["DB_PATH"])
                busy = conn.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()
                conn.close()
            if not busy:
                break
            time.sleep(0.05)


def read_run_by_id(app, run_id):
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            return read_run(conn, run_id)
        finally:
            conn.close()


def test_queued_job_replayed_after_restart(app):
    # Simulate a job accepted just before a crash: persisted 'queued', never run.
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        conn.execute(
            "INSERT INTO forms(name, created_at) VALUES ('A卷', '2026-01-01 00:00:00')"
        )
        rows = engine.parse_form_csv(A_FORM.encode())
        conn.executemany(
            "INSERT INTO form_mappings(form_id, q_no, letter, std_code, std_option) "
            "VALUES (1, ?, ?, ?, ?)", rows,
        )
        import json
        conn.execute(
            "INSERT INTO jobs(kind, payload, status, created_at) "
            "VALUES ('upload_answers', ?, 'queued', '2026-01-01 00:00:00')",
            (json.dumps({
                "csv": answer_csv([("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2")])
            }),),
        )
        conn.close()

    restarted = create_app(app.config["DB_PATH"], sync_jobs=False)
    deadline = 5.0
    import time
    start = time.time()
    result = None
    while time.time() - start < deadline:
        with restarted.app_context():
            conn = db.connect(restarted.config["DB_PATH"])
            try:
                job = conn.execute("SELECT status FROM jobs ORDER BY id DESC").fetchone()
                if job["status"] == "succeeded":
                    result = read_run(conn, db.active_run_id(conn))
                    break
            finally:
                conn.close()
        time.sleep(0.05)
    assert result is not None
    assert result["stats"]["Q1"]["counted"] == 1


# --------------------------------------------------------------------------
# Same-mistake neighbour screening
# --------------------------------------------------------------------------

def seat_csv(rows):
    return "座号,卡号\n" + "".join(f"{s},{c}\n" for s, c in rows)


def key_csv(rows):
    return "标准题码,正确标准选项\n" + "".join(f"{c},{o}\n" for c, o in rows)


def screen_post(client, run_id, seats, key, threshold):
    return client.post(
        f"/runs/{run_id}/screen",
        data={
            "seats": (io.BytesIO(seats.encode("utf-8")), "seats.csv"),
            "key": (io.BytesIO(key.encode("utf-8")), "key.csv"),
            "threshold": str(threshold),
        },
        content_type="multipart/form-data",
    )


def screen_engine(app, run_id, seats, key, threshold):
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            return engine.screen_snapshot(
                conn, run_id,
                engine.parse_seat_csv(seats.encode("utf-8")),
                engine.parse_key_csv(key.encode("utf-8")),
                threshold,
            )
        finally:
            conn.close()


# Two forms where the same printed letters mean different standard options,
# so the same physical card can have several compatible forms with different
# normalised answers.
POOL_FORM = form_csv([
    ("1", "A", "Q1", "A"), ("1", "B", "Q1", "B"),
    ("2", "A", "Q2", "A"), ("2", "B", "Q2", "B"),
])
SWAP_FORM = form_csv([
    ("1", "A", "Q1", "B"), ("1", "B", "Q1", "A"),
    ("2", "A", "Q2", "B"), ("2", "B", "Q2", "A"),
])
THREE_Q_KEY = key_csv([("Q1", "A"), ("Q2", "A")])


def test_snapshot_records_compatible_forms_and_normalised_answers(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/forms", SWAP_FORM, name="B卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "B", ""),
    ]))
    run = current_run(app)
    assert run["screenable"] is True
    assert [c["card_no"] for c in run["card_forms"]] == ["C1"]
    forms = run["card_forms"][0]["forms"]
    assert {f["form_name"] for f in forms} == {"A卷", "B卷"}
    by_name = {f["form_name"]: f for f in forms}
    assert by_name["A卷"]["answers"]["Q1"]["std_option"] == "A"
    assert by_name["B卷"]["answers"]["Q1"]["std_option"] == "B"
    assert by_name["A卷"]["answers"]["Q2"]["std_option"] == "B"


def test_screen_counts_only_same_wrong_option(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),   # Q1 B(wrong), Q2 B(wrong)
        ("C2", "1", "B", ""), ("C2", "2", "A", ""),   # Q1 B(wrong), Q2 A(right)
        ("C3", "1", "", ""), ("C3", "2", "B", ""),    # blank is not a choice
    ]))
    run = current_run(app)
    result = screen_engine(app, run["id"], seat_csv([("1", "C1"), ("2", "C2")]),
                           THREE_Q_KEY, 2)
    assert result.best_count == 0
    assert result.matches == []

    result = screen_engine(app, run["id"], seat_csv([("1", "C1"), ("2", "C2")]),
                           THREE_Q_KEY, 1)
    assert [(m["card_a"], m["card_b"], m["codes"]) for m in result.matches] == [
        ("C1", "C2", ["Q1"]),
    ]


def test_screen_uses_normalised_answers_across_forms(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/forms", SWAP_FORM, name="B卷")
    # C1 letter B -> Q1=B under A卷, Q1=A under B卷.  C2 letter A -> A / B.
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
        ("C2", "1", "A", ""), ("C2", "2", "B", ""),
    ]))
    run = current_run(app)
    # Under different forms they disagree; same form gives Q1=A right + Q2=B
    # wrong => 1 shared mistake.  Threshold 2 is unreachable.
    assert screen_engine(app, run["id"],
                         seat_csv([("1", "C1"), ("2", "C2")]),
                         THREE_Q_KEY, 2).matches == []
    result = screen_engine(app, run["id"],
                           seat_csv([("1", "C1"), ("2", "C2")]),
                           THREE_Q_KEY, 1)
    assert result.matches and result.matches[0]["codes"] == ["Q2"]
    # One form per card, applied across the whole row.
    assert result.matches[0]["form_a"] == result.matches[0]["form_b"]


def test_screen_requires_seats_to_be_adjacent(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
        ("C2", "1", "B", ""), ("C2", "2", "B", ""),
    ]))
    run = current_run(app)
    result = screen_engine(app, run["id"],
                           seat_csv([("1", "C1"), ("3", "C2")]),
                           THREE_Q_KEY, 1)
    assert result.pairs and result.pairs[0]["adjacent"] is False
    assert result.matches == []


def test_screen_maximises_number_of_matching_pairs(client, app):
    # Non-adjacent forms would give C1/C2 1 shared mistake; choosing the swap
    # form for C1 gives 2 shared mistakes but the DP should prefer the count.
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
        ("C2", "1", "B", ""), ("C2", "2", "B", ""),
    ]))
    run = current_run(app)
    result = screen_engine(app, run["id"],
                           seat_csv([("1", "C1"), ("2", "C2")]),
                           THREE_Q_KEY, 2)
    assert [(m["codes"], m["count"]) for m in result.matches] == [(["Q1", "Q2"], 2)]


def test_screen_handles_cards_without_compatible_form(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
        ("C9", "1", "A", "WRONG"),
        ("C3", "1", "B", ""), ("C3", "2", "B", ""),
    ]))
    run = current_run(app)
    result = screen_engine(
        app, run["id"],
        seat_csv([("1", "C1"), ("2", "C9"), ("3", "C3")]),
        THREE_Q_KEY, 1,
    )
    assert result.best_count == 0
    assert all(not p["eligible"] for p in result.pairs)


def test_screen_rejects_more_than_30_seats(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
    ]))
    run = current_run(app)
    seats = seat_csv([(str(i), f"C{i}") for i in range(1, 32)])
    resp = screen_post(client, run["id"], seats, THREE_Q_KEY, 1)
    assert resp.status_code == 400
    assert "30" in resp.get_data(as_text=True)


def test_screen_rejects_duplicate_seat_or_card(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
    ]))
    run = current_run(app)
    for bad in (seat_csv([("1", "C1"), ("1", "C2")]),
                seat_csv([("1", "C1"), ("2", "C1")])):
        resp = screen_post(client, run["id"], bad, THREE_Q_KEY, 1)
        assert resp.status_code == 400


def test_screen_uses_snapshot_after_answer_reupload(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
        ("C2", "1", "B", ""), ("C2", "2", "B", ""),
    ]))
    first = current_run(app)["id"]
    # Replace answers with all-correct ones.
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "A", ""),
        ("C2", "1", "A", ""), ("C2", "2", "A", ""),
    ]))
    assert current_run(app)["id"] != first
    old = screen_engine(app, first, seat_csv([("1", "C1"), ("2", "C2")]),
                        THREE_Q_KEY, 2)
    assert [(m["codes"], m["count"]) for m in old.matches] == [(["Q1", "Q2"], 2)]
    new = screen_engine(app, current_run(app)["id"],
                        seat_csv([("1", "C1"), ("2", "C2")]), THREE_Q_KEY, 1)
    assert new.matches == []


def test_screen_uses_snapshot_after_form_correction(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
        ("C2", "1", "B", ""), ("C2", "2", "B", ""),
    ]))
    first = current_run(app)["id"]
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        form_id = conn.execute("SELECT id FROM forms").fetchone()["id"]
        conn.close()
    client.post("/forms/correct", data={
        "form_id": str(form_id), "q_no": "1", "new_code": "Q1X",
    }, follow_redirects=True)
    # The old snapshot still answers with Q1, so screening is unchanged.
    old = screen_engine(app, first, seat_csv([("1", "C1"), ("2", "C2")]),
                        THREE_Q_KEY, 2)
    assert [(m["codes"], m["count"]) for m in old.matches] == [(["Q1", "Q2"], 2)]


def test_old_snapshot_without_screen_data_is_rejected(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
    ]))
    run_id = current_run(app)["id"]
    # Simulate a snapshot taken before screening data was recorded.
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        conn.execute("DELETE FROM run_card_forms WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM run_card_form_answers WHERE run_id = ?", (run_id,))
        conn.close()
    page = client.get(f"/runs/{run_id}/screen")
    assert page.status_code == 200
    assert "无法筛查" in page.get_data(as_text=True)
    resp = screen_post(client, run_id, seat_csv([("1", "C1")]), THREE_Q_KEY, 1)
    assert resp.status_code == 422
    assert "无法筛查" in resp.get_data(as_text=True)


def test_screen_page_lists_results(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
        ("C2", "1", "B", ""), ("C2", "2", "B", ""),
    ]))
    run = current_run(app)
    resp = screen_post(client, run["id"], seat_csv([("1", "C1"), ("2", "C2")]),
                       THREE_Q_KEY, 2)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Q1, Q2" in html
    assert "A卷" in html


def test_screen_does_not_touch_live_state(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
        ("C2", "1", "B", ""), ("C2", "2", "B", ""),
    ]))
    run = current_run(app)
    before = stats_of(run)
    screen_post(client, run["id"], seat_csv([("1", "C1"), ("2", "C2")]),
                THREE_Q_KEY, 2)
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            assert db.active_run_id(conn) == run["id"]
            assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 2
        finally:
            conn.close()
    assert stats_of(current_run(app)) == before


def test_screen_threshold_must_be_positive_integer(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
    ]))
    run = current_run(app)
    for bad in ("0", "-1", "abc", ""):
        resp = screen_post(client, run["id"], seat_csv([("1", "C1")]),
                           THREE_Q_KEY, bad)
        assert resp.status_code == 400, bad


def test_screen_requires_all_three_inputs(client, app):
    upload(client, "/forms", POOL_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "B", ""), ("C1", "2", "B", ""),
    ]))
    run = current_run(app)
    resp = client.post(
        f"/runs/{run['id']}/screen",
        data={"key": (io.BytesIO(THREE_Q_KEY.encode()), "key.csv"),
              "threshold": "1"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "座位 CSV" in resp.get_data(as_text=True)
