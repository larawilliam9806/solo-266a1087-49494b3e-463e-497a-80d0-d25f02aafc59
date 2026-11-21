"""Form/answer parsing and the item-analysis engine.

The engine is deliberately pure: it takes the current forms, answer rows and
human review decisions, and returns a complete, deterministic snapshot.  The
caller persists that snapshot in one transaction together with the change that
triggered it, so forms, mapping corrections, review decisions and statistics
always become visible at the same moment.
"""
from __future__ import annotations

import csv
import io
import re
from collections import OrderedDict


class ValidationError(ValueError):
    """Raised for bad CSV input or an invalid form/mapping edit."""


# --------------------------------------------------------------------------
# CSV parsing
# --------------------------------------------------------------------------

FORM_HEADERS = {
    "q_no": {"题号", "卷式题号", "印刷题号", "q_no", "qno", "question", "题号\n"},
    "letter": {"选项字母", "字母", "涂选字母", "option", "letter", "选项"},
    "std_code": {"标准题码", "题码", "标准题号", "std_code", "code", "standard_code"},
    "std_option": {"标准选项", "标准答案选项", "std_option", "standard_option"},
}
ANSWER_HEADERS = {
    "card_no": {"卡号", "答题卡号", "卡编号", "card_no", "card", "card_id"},
    "q_no": {"题号", "卷式题号", "印刷题号", "q_no", "qno", "question"},
    "letter": {"涂选字母", "选项字母", "字母", "letter", "option", "作答"},
    "scanned_code": {"实扫题码", "扫描题码", "实扫题号", "scanned_code", "scan_code", "scanned"},
}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValidationError("无法识别文件编码，请使用 UTF-8 或 GBK 编码的 CSV")


def _norm(value) -> str:
    return (value or "").strip()


def _read_rows(raw: bytes, spec: dict, kind: str):
    """Return (data_rows, text) so callers can persist the decoded CSV."""
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    rows = [[_norm(c) for c in r] for r in reader if any(_norm(c) for c in r)]
    if not rows:
        raise ValidationError(f"{kind}为空")

    def locate(header):
        found = {}
        for idx, cell in enumerate(header):
            key = cell.lower().replace(" ", "")
            if not key:
                continue
            for field, aliases in spec.items():
                if key in {a.lower() for a in aliases} and field not in found:
                    found[field] = idx
        return found

    positions = locate(rows[0])
    if positions:
        missing = [f for f in spec if f not in positions]
        if missing:
            raise ValidationError(f"{kind}缺少列：{', '.join(missing)}")
        data_rows = rows[1:]
    else:
        # Headerless file: accept the canonical column order.
        positions = {field: i for i, field in enumerate(spec)}
        data_rows = rows

    out = []
    for r in data_rows:
        out.append(tuple(r[positions[f]] if positions[f] < len(r) else "" for f in spec))
    return out, text


def decode_csv(raw: bytes) -> str:
    """Decode an uploaded CSV to text so job payloads stay UTF-8 safe."""
    return _decode(raw)


def validate_form_rows(rows):
    """Validate one form's mapping rows. Returns the same rows on success."""
    seen = set()
    q_to_code: dict[str, str] = {}
    code_to_q: dict[str, str] = {}
    code_letters: dict[str, dict[str, str]] = {}
    for q_no, letter, std_code, std_option in rows:
        if not q_no or not letter:
            raise ValidationError(f"卷式行缺少题号或选项字母：{q_no or '?'}/{letter or '?'}")
        if not std_code or not std_option:
            raise ValidationError(f"卷式行缺少标准题码或标准选项：{q_no}/{letter}")
        if (q_no, letter) in seen:
            raise ValidationError(f"卷式内题号 {q_no} 的选项 {letter} 重复")
        seen.add((q_no, letter))
        if q_to_code.setdefault(q_no, std_code) != std_code:
            raise ValidationError(
                f"卷式内题号 {q_no} 映射到多个标准题码（{q_to_code[q_no]} / {std_code}）"
            )
        if code_to_q.setdefault(std_code, q_no) != q_no:
            raise ValidationError(
                f"标准题码 {std_code} 在本卷出现多次（题号 {code_to_q[std_code]} / {q_no}）"
            )
        letters = code_letters.setdefault(std_code, {})
        if std_option in letters:
            raise ValidationError(
                f"标准题码 {std_code} 的标准选项 {std_option} 重复"
            )
        letters[std_option] = letter
    return rows


def parse_form_csv(raw: bytes):
    """Return [(q_no, letter, std_code, std_option), ...] validated per form."""
    rows, _ = _read_rows(raw, FORM_HEADERS, "卷式 CSV")
    if not rows:
        raise ValidationError("卷式 CSV 没有数据行")
    return validate_form_rows(rows)


def parse_answer_csv(raw: bytes):
    """Return [{card_no, q_no, letter, scanned_code}, ...]."""
    rows, _ = _read_rows(raw, ANSWER_HEADERS, "答题 CSV")
    if not rows:
        raise ValidationError("答题 CSV 没有数据行")
    seen = set()
    out = []
    for card_no, q_no, letter, scanned in rows:
        if not card_no or not q_no:
            raise ValidationError("答题行缺少卡号或题号")
        if (card_no, q_no) in seen:
            raise ValidationError(f"卡号 {card_no} 的题号 {q_no} 出现多行")
        seen.add((card_no, q_no))
        out.append(
            {"card_no": card_no, "q_no": q_no, "letter": letter, "scanned_code": scanned}
        )
    return out


# --------------------------------------------------------------------------
# Same-mistake neighbour screening (CSV parsing)
# --------------------------------------------------------------------------

SEAT_HEADERS = {
    "seat_no": {"座号", "座位号", "座次", "seat", "seat_no", "seatno"},
    "card_no": {"卡号", "答题卡号", "卡编号", "card_no", "card", "card_id"},
}
KEY_HEADERS = {
    "std_code": {"标准题码", "题码", "标准题号", "std_code", "code", "standard_code"},
    "std_option": {"正确选项", "标准选项", "正确答案", "标准答案", "正确标准选项",
                   "正确标准答案", "答案", "std_option", "correct_option", "answer",
                   "key"},
}

SCREEN_MAX_SEATS = 30          # hard limits from the task
SCREEN_MAX_FORMS_PER_CARD = 12
MIN_NEIGHBOURS_MIN = 1
MIN_NEIGHBOURS_MAX = SCREEN_MAX_SEATS - 1


def parse_seat_csv(raw: bytes):
    """Return [(seat_no, card_no), ...]; both columns must be present and unique."""
    rows, _ = _read_rows(raw, SEAT_HEADERS, "座位 CSV")
    if not rows:
        raise ValidationError("座位 CSV 没有数据行")
    if len(rows) > SCREEN_MAX_SEATS:
        raise ValidationError(
            f"筛查一次最多 {SCREEN_MAX_SEATS} 个座位，当前 {len(rows)} 行"
        )
    seen_seats = set()
    seen_cards = set()
    out = []
    for seat_no, card_no in rows:
        if not seat_no or not card_no:
            raise ValidationError("座位行缺少座号或卡号")
        if seat_no in seen_seats:
            raise ValidationError(f"座号 {seat_no} 重复")
        if card_no in seen_cards:
            raise ValidationError(f"卡号 {card_no} 重复")
        seen_seats.add(seat_no)
        seen_cards.add(card_no)
        out.append((seat_no, card_no))
    return out


def parse_key_csv(raw: bytes):
    """Return OrderedDict{std_code: correct_option}.

    ``正确选项`` is what a card must have picked (after normalisation) for the
    question to count as a mistake on that card.
    """
    rows, _ = _read_rows(raw, KEY_HEADERS, "答案 CSV")
    if not rows:
        raise ValidationError("答案 CSV 没有数据行")
    key: "OrderedDict[str, str]" = OrderedDict()
    for std_code, std_option in rows:
        if not std_code or not std_option:
            raise ValidationError("答案行缺少标准题码或正确选项")
        if std_code in key:
            raise ValidationError(f"答案 CSV 中标准题码 {std_code} 重复")
        key[std_code] = std_option
    return key


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def _natural_key(text: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text)]


def seat_order_key(seat_no: str):
    """Sort seats naturally, so 9 < 10 and A2 < A10."""
    return _natural_key(seat_no)


_SEAT_RE = re.compile(r"^([^0-9]*)([0-9]+)$")


def _seat_number(seat_no: str):
    match = _SEAT_RE.match((seat_no or "").strip())
    if not match:
        return None
    return match.group(1), int(match.group(2))


def seats_adjacent(seat_a: str, seat_b: str) -> bool:
    """True when two seats are next to each other in the row.

    A seat such as ``12`` (or ``A12``) is adjacent to ``13`` (``A13``) — the
    number differs by exactly 1 and the prefix is shared.  For labels without
    a number tail, consecutive positions in natural order count as adjacent.
    """
    na, nb = _seat_number(seat_a), _seat_number(seat_b)
    if na is not None and nb is not None:
        return na[0] == nb[0] and nb[1] - na[1] == 1
    return True


def build_forms(form_rows) -> "OrderedDict[int, dict]":
    """form_rows: iterable of (form_id, form_name, q_no, letter, std_code, std_option)."""
    forms: "OrderedDict[int, dict]" = OrderedDict()
    for form_id, form_name, q_no, letter, std_code, std_option in form_rows:
        form = forms.setdefault(
            form_id,
            {"id": form_id, "name": form_name, "qmap": {}, "codes": {},
             "options": OrderedDict()},
        )
        entry = form["qmap"].setdefault(
            q_no, {"std_code": std_code, "letters": {}, "rev": {}}
        )
        entry["letters"][letter] = std_option
        entry["rev"][std_option] = letter
        form["codes"][std_code] = q_no
        form["options"].setdefault(std_code, set()).add(std_option)
    for form in forms.values():
        form["q_set"] = set(form["qmap"].keys())
        form["code_set"] = set(form["codes"].keys())
    return forms


def form_is_compatible(form: dict, card_rows: dict) -> bool:
    """Full-card question numbers match and every non-empty code/letter fits."""
    if form["q_set"] != card_rows.keys():
        return False
    for q_no, row in card_rows.items():
        mapping = form["qmap"][q_no]
        if row["scanned_code"] and mapping["std_code"] != row["scanned_code"]:
            return False
        if row["letter"] and row["letter"] not in mapping["letters"]:
            return False
    return True


class Analysis:
    def __init__(self):
        self.question_codes: list[str] = []
        self.options: dict[str, list[str]] = {}
        self.stats: dict[str, dict] = {}
        self.card_results: list[dict] = []
        self.unmatched_cards: list[dict] = []
        self.flagged: list[dict] = []
        self.cards_total = 0
        self.cards_matched = 0
        # Card snapshots for screening: compatible forms and normalised answers
        # under each of them, captured at the moment the run is generated.
        self.card_forms: list[dict] = []
        self.card_form_answers: list[dict] = []

    def summary(self) -> dict:
        return {
            "cards_total": self.cards_total,
            "cards_matched": self.cards_matched,
            "cards_unmatched": len(self.unmatched_cards),
            "questions": len(self.question_codes),
            "flagged_cells": len(self.flagged),
        }


def analyse(forms, card_rows_by_card: dict, decisions: dict) -> Analysis:
    """Compute the whole analysis snapshot.

    forms: OrderedDict from build_forms
    card_rows_by_card: {card_no: {q_no: {"letter":..., "scanned_code":...}}}
    decisions: {(card_no, std_code): {"resolution":..., "note":...}}
    """
    result = Analysis()

    # Standard questions and their option universe are defined by live forms.
    codes = set()
    options: dict[str, set[str]] = {}
    for form in forms.values():
        for code, letters in form["options"].items():
            codes.add(code)
            options.setdefault(code, set()).update(letters)
    result.question_codes = sorted(codes, key=_natural_key)
    result.options = {c: sorted(options[c]) for c in result.question_codes}

    all_forms = list(forms.values())
    for code in result.question_codes:
        result.stats[code] = {
            "counted": 0, "blank": 0, "flagged": 0,
            "options": {o: 0 for o in result.options[code]},
        }

    result.cards_total = len(card_rows_by_card)
    for card_no in sorted(card_rows_by_card):
        rows = card_rows_by_card[card_no]
        compatible = [f for f in all_forms if form_is_compatible(f, rows)]
        if not compatible:
            detail = "无可相容卷式（整卡题号不一致或非空题码/涂选字母不吻合）"
            result.unmatched_cards.append({"card_no": card_no, "detail": detail})
            result.card_results.append(
                {"card_no": card_no, "std_code": "", "status": "unmatched",
                 "std_option": None, "detail": detail}
            )
            continue
        result.cards_matched += 1
        form_names = "/".join(f["name"] for f in compatible)

        # Freeze what screening needs, right now, for this card.  Scanning
        # every printed question (not just the live standard codes) keeps the
        # snapshot self-contained when a code is later corrected.
        for order, form in enumerate(compatible):
            result.card_forms.append({
                "card_no": card_no, "form_id": form["id"],
                "form_name": form["name"], "form_order": order,
            })
            for q_no in sorted(form["qmap"], key=_natural_key):
                mapping = form["qmap"][q_no]
                letter = ""
                # A printed question's letter comes from the card row with the
                # same printed number; incompatible numbering never gets here.
                row = rows.get(q_no)
                if row:
                    letter = row["letter"]
                std_option = mapping["letters"].get(letter, "") if letter else ""
                result.card_form_answers.append({
                    "card_no": card_no, "form_id": form["id"],
                    "std_code": mapping["std_code"], "q_no": q_no,
                    "letter": letter, "std_option": std_option,
                })

        for code in result.question_codes:
            stat = result.stats[code]
            decision = decisions.get((card_no, code))

            containing = [f for f in compatible if code in f["codes"]]
            if not containing:
                # No compatible form printed this question: skip entirely.
                continue

            answers = []
            for form in containing:
                q_no = form["codes"][code]
                letter = rows[q_no]["letter"]
                if letter:
                    std_option = form["qmap"][q_no]["letters"][letter]
                else:
                    std_option = ""
                answers.append((form["name"], letter, std_option))

            if len(containing) != len(compatible):
                missing = [f["name"] for f in compatible if code not in f["codes"]]
                status, option, detail = (
                    "flagged", None,
                    f"部分相容卷式未出该题：{'/'.join(missing)}",
                )
            else:
                distinct = {a[2] for a in answers}
                if len(distinct) == 1:
                    option = answers[0][2]
                    if option:
                        status, detail = "counted", f"相容卷式：{form_names}"
                    else:
                        status, detail = "blank", f"相容卷式：{form_names}（空答）"
                else:
                    shown = "；".join(
                        f"{name}: {letter or '空'}" for name, letter, _ in answers
                    )
                    status, option, detail = (
                        "flagged", None, f"卷式归一作答分歧：{shown}"
                    )

            if decision and status in ("flagged", "blank"):
                # A human conclusion is only meaningful where the automatic
                # verdict could not be trusted (disagreement / unprinted).
                # Once a correction makes the cell consistent, recalculated
                # statistics win and the stale conclusion stops applying.
                res = decision["resolution"]
                if res == "skip":
                    status, option, detail = "skipped", None, (
                        "人工核对：跳过（不计入）" +
                        (f"；{decision['note']}" if decision.get("note") else "")
                    )
                elif res == "blank":
                    status, option, detail = "blank", None, (
                        "人工核对：漏答" +
                        (f"；{decision['note']}" if decision.get("note") else "")
                    )
                elif res.startswith("opt:") and res[4:] in stat["options"]:
                    chosen = res[4:]
                    status, option, detail = "counted", chosen, (
                        "人工核对：" + chosen +
                        (f"；{decision['note']}" if decision.get("note") else "")
                    )

            if status == "skipped":
                continue
            if status == "counted":
                stat["counted"] += 1
                stat["options"][option] += 1
            elif status == "blank":
                stat["blank"] += 1
            else:
                stat["flagged"] += 1
                result.flagged.append(
                    {"card_no": card_no, "std_code": code, "detail": detail}
                )
            result.card_results.append(
                {"card_no": card_no, "std_code": code, "status": status,
                 "std_option": option, "detail": detail}
            )

    result.flagged.sort(key=lambda x: (_natural_key(x["card_no"]), _natural_key(x["std_code"])))
    return result


# --------------------------------------------------------------------------
# DB loading + persistence
# --------------------------------------------------------------------------

def load_forms(conn):
    rows = conn.execute(
        "SELECT f.id, f.name, m.q_no, m.letter, m.std_code, m.std_option "
        "FROM forms f JOIN form_mappings m ON m.form_id = f.id "
        "ORDER BY f.id, m.q_no, m.letter"
    ).fetchall()
    return build_forms(
        [(r["id"], r["name"], r["q_no"], r["letter"], r["std_code"], r["std_option"])
         for r in rows]
    )


def load_card_rows(conn):
    cards: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT card_no, q_no, letter, scanned_code FROM answer_rows "
        "ORDER BY card_no, q_no"
    ):
        cards.setdefault(r["card_no"], {})[r["q_no"]] = {
            "letter": r["letter"], "scanned_code": r["scanned_code"],
        }
    return cards


def load_decisions(conn):
    return {
        (r["card_no"], r["std_code"]): {
            "resolution": r["resolution"], "note": r["note"],
        }
        for r in conn.execute("SELECT card_no, std_code, resolution, note FROM review_decisions")
    }


def compute(conn) -> Analysis:
    return analyse(load_forms(conn), load_card_rows(conn), load_decisions(conn))


def persist_run(conn, analysis: Analysis, created_at: str) -> int:
    """Insert a full snapshot and return its run id. Caller owns the transaction."""
    cur = conn.execute(
        "INSERT INTO runs(created_at, summary_json) VALUES (?, '{}')", (created_at,)
    )
    run_id = cur.lastrowid
    import json
    conn.execute(
        "UPDATE runs SET summary_json = ? WHERE id = ?",
        (json.dumps(analysis.summary(), ensure_ascii=False), run_id),
    )
    conn.executemany(
        "INSERT INTO run_question_stats(run_id, std_code, counted, blank, flagged) "
        "VALUES (?, ?, ?, ?, ?)",
        [(run_id, code, st["counted"], st["blank"], st["flagged"])
         for code, st in analysis.stats.items()],
    )
    conn.executemany(
        "INSERT INTO run_option_counts(run_id, std_code, std_option, cnt) VALUES (?, ?, ?, ?)",
        [(run_id, code, opt, cnt)
         for code, st in analysis.stats.items() for opt, cnt in st["options"].items()],
    )
    conn.executemany(
        "INSERT INTO run_card_results(run_id, card_no, std_code, status, std_option, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(run_id, r["card_no"], r["std_code"], r["status"], r["std_option"], r["detail"])
         for r in analysis.card_results],
    )
    conn.executemany(
        "INSERT INTO run_unmatched(run_id, card_no, detail) VALUES (?, ?, ?)",
        [(run_id, u["card_no"], u["detail"]) for u in analysis.unmatched_cards],
    )
    conn.executemany(
        "INSERT INTO run_card_forms(run_id, card_no, form_id, form_name, form_order) "
        "VALUES (?, ?, ?, ?, ?)",
        [(run_id, r["card_no"], r["form_id"], r["form_name"], r["form_order"])
         for r in analysis.card_forms],
    )
    conn.executemany(
        "INSERT INTO run_card_form_answers"
        "(run_id, card_no, form_id, std_code, q_no, letter, std_option) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(run_id, r["card_no"], r["form_id"], r["std_code"], r["q_no"],
          r["letter"], r["std_option"])
         for r in analysis.card_form_answers],
    )
    return run_id


def prune_runs(conn, keep: int = 4) -> None:
    rows = conn.execute(
        "SELECT id FROM runs ORDER BY id DESC LIMIT -1 OFFSET ?", (keep,)
    ).fetchall()
    for r in rows:
        conn.execute("DELETE FROM runs WHERE id = ?", (r["id"],))


# --------------------------------------------------------------------------
# Same-mistake neighbour screening
# --------------------------------------------------------------------------

class ScreenError(ValidationError):
    """Raised when a snapshot cannot be used for screening."""


class ScreenResult:
    def __init__(self):
        self.run_id = 0
        self.seat_order: list[str] = []        # seat labels, natural order
        self.cards: dict[str, str] = {}        # seat -> card ("" if unknown)
        self.pairs: list[dict] = []            # every adjacent seat pair
        self.matches: list[dict] = []          # pairs reaching the threshold
        self.assignments: dict[str, str] = {}  # seat -> chosen form name
        self.used_forms: list[str] = []
        self.threshold = 0
        self.best_count = 0
        self.available = True                  # snapshot carries screen data
        self.reason = ""                       # why unavailable / no result

    def summary(self) -> dict:
        return {
            "seats": len(self.seat_order),
            "threshold": self.threshold,
            "matched": len(self.matches),
        }


class _ScreenData:
    """Everything screening needs, read once from one immutable snapshot."""

    def __init__(self):
        self.card_forms: dict[str, list[dict]] = {}      # card -> candidate forms
        self.answers: dict[tuple[str, int], dict[str, str]] = {}
        self.form_names: dict[int, str] = {}


def load_screen_data(conn, run_id: int) -> _ScreenData | None:
    """Return frozen per-card data, or None if this snapshot predates it."""
    data = _ScreenData()
    rows = conn.execute(
        "SELECT card_no, form_id, form_name FROM run_card_forms "
        "WHERE run_id = ? ORDER BY card_no, form_order, form_id", (run_id,)
    ).fetchall()
    if not rows:
        return None
    for r in rows:
        data.card_forms.setdefault(r["card_no"], []).append(
            {"id": r["form_id"], "name": r["form_name"]}
        )
        data.form_names.setdefault(r["form_id"], r["form_name"])
    for r in conn.execute(
        "SELECT card_no, form_id, std_code, std_option FROM run_card_form_answers "
        "WHERE run_id = ?", (run_id,)
    ):
        data.answers.setdefault((r["card_no"], r["form_id"]), {})[
            r["std_code"]
        ] = r["std_option"]
    return data


def _adjacent_seats(seats_sorted: list[str]) -> list[tuple[str, str]]:
    return list(zip(seats_sorted, seats_sorted[1:]))


def screen_snapshot(conn, run_id: int, seat_rows, answer_key: dict,
                    threshold, card_form_limit: int = SCREEN_MAX_FORMS_PER_CARD
                    ) -> ScreenResult:
    """Screen a row of seats against one retained snapshot.

    ``seat_rows``   [(seat_no, card_no), ...] from the seat CSV.
    ``answer_key``  {std_code: correct_option} from the answer CSV.
    ``threshold``   minimum number of shared mistakes for a pair to count.

    Rules enforced here:
    * Only cards that had at least one compatible form *in this snapshot* take
      part; the whole card uses exactly one of those forms.
    * A pair is compared only when the two cards are adjacent in seat order
      and the chosen forms are both compatible with their own card.
    * A shared mistake is a question present in the answer CSV that both cards
      normalise (by their chosen forms) to the same wrong option.
    * Maximise the number of adjacent pairs reaching ``threshold``.

    Nothing is read from live forms/answer_rows, so later answer re-uploads or
    form corrections do not change the result for a retained snapshot.
    """
    result = ScreenResult()
    result.run_id = run_id

    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        raise ScreenError("同错题数下限必须是整数")
    if threshold < MIN_NEIGHBOURS_MIN or threshold > MIN_NEIGHBOURS_MAX:
        raise ScreenError(
            f"同错题数下限应在 {MIN_NEIGHBOURS_MIN}–{MIN_NEIGHBOURS_MAX} 之间"
        )
    result.threshold = threshold

    if not seat_rows:
        raise ScreenError("座位 CSV 没有数据行")
    if len(seat_rows) > SCREEN_MAX_SEATS:
        raise ScreenError(f"一次最多筛查 {SCREEN_MAX_SEATS} 个座位")
    if not answer_key:
        raise ScreenError("答案 CSV 没有数据行")

    # Identity of seats: both columns unique (also re-checked by the parser).
    seats_seen, cards_seen = set(), set()
    for seat_no, card_no in seat_rows:
        if seat_no in seats_seen or card_no in cards_seen:
            raise ScreenError("座号与卡号都必须各自唯一")
        seats_seen.add(seat_no)
        cards_seen.add(card_no)

    # Adjacency is defined by the seat number's natural order, not CSV order.
    ordered = sorted(seat_rows, key=lambda r: seat_order_key(r[0]))
    result.seat_order = [s for s, _ in ordered]
    result.cards = {s: c for s, c in ordered}

    data = load_screen_data(conn, run_id)
    if data is None:
        result.available = False
        result.reason = (
            "该快照生成于筛查功能上线之前，未记录各卡的相容卷式与归一作答，"
            "无法筛查。请重新导入答题数据生成新快照后再试。"
        )
        return result

    candidates: dict[str, list[dict]] = {}
    for _seat, card_no in ordered:
        forms = data.card_forms.get(card_no, [])
        if not forms:
            continue
        if len(forms) > card_form_limit:
            raise ScreenError(
                f"卡号 {card_no} 有 {len(forms)} 种相容卷式，超过每卡最多 "
                f"{card_form_limit} 种的上限"
            )
        candidates[card_no] = forms

    def normalised(card_no: str, form_id: int, code: str):
        return data.answers.get((card_no, form_id), {}).get(code)

    def shared_codes(card_a: str, fa: int, card_b: str, fb: int) -> list[str]:
        codes = []
        for code, key_opt in answer_key.items():
            a = normalised(card_a, fa, code)
            b = normalised(card_b, fb, code)
            if not a or not b:
                continue                      # missing / blank is not a choice
            if a == b and a != key_opt:
                codes.append(code)
        codes.sort(key=_natural_key)
        return codes

    # Score every adjacent pair over its form combinations.  The per-pair
    # maximum is also a safe upper bound for pruning the global search.
    pair_info: list[dict] = []
    for seat_a, seat_b in _adjacent_seats(result.seat_order):
        card_a, card_b = result.cards[seat_a], result.cards[seat_b]
        info = {
            "seat_a": seat_a, "seat_b": seat_b,
            "card_a": card_a, "card_b": card_b,
            "eligible": card_a in candidates and card_b in candidates,
            "adjacent": seats_adjacent(seat_a, seat_b),
            "reason": "", "scores": {}, "upper": 0,
            "matched": False, "codes": [], "form_a": None, "form_b": None,
        }
        if not info["eligible"]:
            missing = [c for c in (card_a, card_b) if c not in candidates]
            info["reason"] = ("快照中无相容卷式：" + "、".join(missing))
        elif not info["adjacent"]:
            info["reason"] = "座号不相邻"
        else:
            for fa in candidates[card_a]:
                for fb in candidates[card_b]:
                    codes = shared_codes(card_a, fa["id"], card_b, fb["id"])
                    info["scores"][(fa["id"], fb["id"])] = codes
                    info["upper"] = max(info["upper"], len(codes))
        pair_info.append(info)
    result.pairs = pair_info

    # --- global optimisation -------------------------------------------------
    # Seats form a path, so the joint score decomposes into adjacent pairs.
    # Dynamic programming walks the seat order carrying the chosen form of the
    # previous seat and the number of qualifying pairs so far.  Score vector is
    # (qualifying pairs, total shared mistakes), compared lexicographically.
    #
    # A seat whose card has no compatible form in this snapshot breaks the
    # chain: no pair touching it can qualify, but neither does it prevent the
    # pairs to its right from being evaluated.
    infos = pair_info  # infos[pos] is the pair (seat pos, seat pos+1)
    n = len(result.seat_order)
    seat_options = [candidates.get(result.cards[s], []) for s in result.seat_order]

    def pair_score(pos: int, fa: int, fb: int):
        info = infos[pos]
        if not info["eligible"] or not info["adjacent"]:
            return 0, 0
        codes = info["scores"].get((fa, fb), [])
        if len(codes) >= threshold:
            return 1, len(codes)
        return 0, 0

    # states[i]: {(form_id_or_None, matched): ((pairs, total, tie), prev_key)}
    states: list[dict] = []
    if n:
        first: dict[tuple, tuple] = {}
        for f in seat_options[0]:
            first[(f["id"], 0)] = ((0, 0, 0), None)
        if not first:
            first[(None, 0)] = ((0, 0, 0), None)
        states.append(first)
    for pos in range(1, n):
        prev_states = states[pos - 1]
        cur: dict[tuple, tuple] = {}
        for (prev_fid, prev_matched), (prev_score, _pk) in prev_states.items():
            for f in seat_options[pos] or [None]:
                fid = f["id"] if f else None
                if prev_fid is None:
                    # Chain already broken: nothing new can qualify here.
                    cand = prev_score
                    inc = 0
                else:
                    inc, total = pair_score(pos - 1, prev_fid, fid)
                    cand = (prev_score[0] + inc, prev_score[1] + total,
                            -(prev_fid * 1000003 + (fid or 0)))
                key = (fid, prev_matched + inc)
                if key not in cur or _better(cand, cur[key][0]):
                    cur[key] = (cand, (prev_fid, prev_matched))
        states.append(cur)

    if not states:
        return result

    final_key = max(states[-1], key=lambda k: states[-1][k][0])
    result.best_count = states[-1][final_key][0][0]

    chosen: dict[int, int | None] = {}
    key = final_key
    for pos in range(n - 1, -1, -1):
        chosen[pos] = key[0]
        prev_key = states[pos][key][1]
        if prev_key is None:
            break
        key = prev_key

    for pos, seat in enumerate(result.seat_order):
        fid = chosen.get(pos)
        if fid is not None:
            result.assignments[seat] = data.form_names.get(fid, str(fid))

    # Per-pair outcomes under the chosen assignment; collect the matches.
    for index, info in enumerate(pair_info):
        fa = chosen.get(index)
        fb = chosen.get(index + 1) if index + 1 < n else None
        if fa is None or fb is None:
            continue
        info["form_a"] = data.form_names.get(fa, str(fa))
        info["form_b"] = data.form_names.get(fb, str(fb))
        codes = info["scores"].get((fa, fb), [])
        info["codes"] = codes
        if info["adjacent"] and len(codes) >= threshold:
            info["matched"] = True
            result.matches.append({
                "seat_a": info["seat_a"], "seat_b": info["seat_b"],
                "card_a": info["card_a"], "card_b": info["card_b"],
                "form_a": info["form_a"], "form_b": info["form_b"],
                "count": len(codes), "codes": codes,
            })

    result.used_forms = sorted(set(result.assignments.values()), key=_natural_key)
    if result.best_count == 0:
        result.reason = (
            f"在整排每卡只用一种相容卷式的前提下，没有邻座对能达到同错 "
            f"{threshold} 题的下限。"
        )
    return result


def _better(a, b) -> bool:
    """Lexicographic comparison of (matched pairs, evidence, tie-break)."""
    return (a[0], a[1], a[2]) > (b[0], b[1], b[2])
