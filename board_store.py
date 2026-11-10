"""The board store: boards, leads, findings, the operator's notes and the evidence ledger.

A board is a search for a problem with a known origin, a testable goal and no
plannable route between them. What makes that hard for an agent is bookkeeping:
what has been tried, what it ruled out, what it opened up, and which open
direction is worth taking next. The store keeps that structure.

- A **lead** is a direction worth trying that nobody has tried yet. The open
  leads are the frontier.
- A **finding** is something an exploration actually showed. It cites evidence:
  tool results the plugin recorded as they happened, so a finding can never be
  something the model simply said.
- A **note** is the operator's judgment, left on the board at any time. Only the
  operator writes one; no tool can.
- The **goal test** is run by the plugin, never scored by the model. Nothing in
  the tool surface can set a board solved.

One SQLite file, WAL, a busy timeout, and every write in an IMMEDIATE
transaction, so the main agent and its subagents can work one board at once.
"""

import hashlib
import secrets
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

SCHEMA = "search-board.v1"

CLAIM_TTL = timedelta(minutes=30)
DEFAULT_STEP_BUDGET = 60
MAX_STEP_BUDGET = 1_000
MAX_FRONTIER = 40
MAX_LEADS_PER_CALL = 8
MAX_EVIDENCE_PER_FINDING = 20
SIMILARITY_WARN = 0.6

MAX_NAME = 64
MAX_TITLE = 200
MAX_ORIGIN = 4_000
MAX_GOAL = 2_000
MAX_GOAL_TEST = 4_000
MAX_DIRECTION = 1_000
MAX_RATIONALE = 2_000
MAX_STATEMENT = 2_000
MAX_NOTE = 2_000
MIN_TEXT = 8

# Evidence excerpts: enough of a tool result to judge it, not the whole thing.
EXCERPT_HEAD = 2_000
EXCERPT_TAIL = 2_000
ARGS_EXCERPT = 1_000
EVIDENCE_KEEP_DAYS = 14

STATUSES = (
    "open", "suspended", "proposed_solved", "proposed_exhausted",
    "solved", "exhausted", "abandoned",
)
LIVE = ("open", "suspended")
AWAITING_OPERATOR = ("proposed_solved", "proposed_exhausted")
TEST_KINDS = ("command", "observation", "judgment")
POLARITIES = ("supports", "rules_out")
BASES = ("observed", "inferred")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class Refused(Exception):
    """A request the store won't carry out, with the reason to hand back."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp(at: Optional[datetime] = None) -> str:
    return (at or now_utc()).isoformat(timespec="seconds")


def parse_stamp(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def ago(text: Optional[str], at: Optional[datetime] = None) -> str:
    then = parse_stamp(text)
    if then is None:
        return "unknown"
    seconds = int(((at or now_utc()) - then).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3_600:
        return f"{seconds // 60} min ago"
    if seconds < 86_400:
        return f"{seconds // 3_600} h ago"
    return f"{seconds // 86_400} d ago"


def bounded(value: object, low: int, high: int, what: str) -> str:
    text = str(value or "").strip()
    length = len(text)
    if length < low:
        raise Refused(f"{what} is too short (at least {low} characters)")
    if length > high:
        raise Refused(f"{what} is too long (at most {high} characters, got {length})")
    return text


def valid_name(value: object) -> str:
    name = str(value or "").strip()
    if not NAME_RE.match(name):
        raise Refused(
            "name must be lowercase letters, digits and hyphens, starting with a letter or "
            "digit, at most 64 characters (e.g. flaky-flush)"
        )
    return name


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) > 2}


def similarity(left: str, right: str) -> float:
    a, b = _words(left), _words(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def excerpt(text: str, head: int = EXCERPT_HEAD, tail: int = EXCERPT_TAIL) -> str:
    if len(text) <= head + tail:
        return text
    cut = len(text) - head - tail
    return f"{text[:head]}\n… [{cut} characters not kept] …\n{text[-tail:]}"


# ---------------------------------------------------------------- records


@dataclass
class Board:
    id: str
    name: str
    title: str
    origin: str
    goal: str
    goal_test: str
    goal_test_kind: str
    status: str
    step_budget: int
    steps_used: int
    created_at: str
    closed_at: Optional[str]
    outcome: Optional[str]

    @property
    def steps_left(self) -> int:
        return max(self.step_budget - self.steps_used, 0)

    @property
    def live(self) -> bool:
        return self.status in LIVE

    @property
    def accepts_work(self) -> bool:
        return self.status == "open"


@dataclass
class Lead:
    id: str
    board_id: str
    direction: str
    rationale: str
    from_findings: list[str]
    state: str  # open | claimed | explored | ruled_out
    priority: int
    claimed_by: Optional[str]
    claimed_at: Optional[str]
    prior_claims: list[str]
    closed_by: Optional[str]
    created_at: str

    def claim_expired(self, at: datetime) -> bool:
        if self.state != "claimed":
            return False
        claimed = parse_stamp(self.claimed_at)
        return claimed is None or at - claimed > CLAIM_TTL

    def on_frontier(self, at: datetime) -> bool:
        return self.state == "open" or self.claim_expired(at)


@dataclass
class Finding:
    id: str
    board_id: str
    statement: str
    polarity: str
    basis: str
    author: str  # model | plugin | operator
    from_lead: Optional[str]
    evidence: list[int]
    from_findings: list[str]
    created_at: str


@dataclass
class Note:
    id: str
    board_id: str
    text: str
    about: Optional[str]
    created_at: str
    read_at: Optional[str]


@dataclass
class Evidence:
    id: int
    created_at: str
    tool_name: str
    args_excerpt: str
    excerpt: str
    chars: int
    sha256: str
    conversation_id: str
    session_id: str
    tool_call_id: str


@dataclass
class Snapshot:
    board: Board
    leads: list[Lead] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)


# ---------------------------------------------------------------- store

_LOCAL_LOCK = threading.RLock()


class Store:
    """One board database. ``connect`` opens a fresh connection per operation."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        # ``connect`` may resolve a different file over time (Hermes profiles), so the
        # schema is checked per database file, not once per store.
        self._connect = connect
        self._ready: set[str] = set()

    @classmethod
    def at(cls, path: Path) -> "Store":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        def connect() -> sqlite3.Connection:
            db = sqlite3.connect(path, timeout=10, check_same_thread=False)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            return db

        return cls(connect)

    # -- plumbing

    @contextmanager
    def _db(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=10000")
            where = str(db.execute("PRAGMA database_list").fetchone()[2] or ":memory:")
            if where not in self._ready:
                self._create(db, where)
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.execute("COMMIT")
        except BaseException:
            if write and db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def _create(self, db: sqlite3.Connection, where: str) -> None:
        with _LOCAL_LOCK:
            if db.in_transaction:
                db.commit()
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS boards(
                    id TEXT PRIMARY KEY, seq INTEGER NOT NULL, name TEXT NOT NULL,
                    title TEXT NOT NULL, origin TEXT NOT NULL, goal TEXT NOT NULL,
                    goal_test TEXT NOT NULL, goal_test_kind TEXT NOT NULL, status TEXT NOT NULL,
                    step_budget INTEGER NOT NULL, steps_used INTEGER NOT NULL,
                    created_at TEXT NOT NULL, closed_at TEXT, outcome TEXT);
                -- One live board per name; a closed board's name may be reused.
                CREATE UNIQUE INDEX IF NOT EXISTS boards_live_name ON boards(name)
                    WHERE status IN ('open', 'suspended');
                CREATE TABLE IF NOT EXISTS leads(
                    id TEXT NOT NULL, board_id TEXT NOT NULL REFERENCES boards(id),
                    seq INTEGER NOT NULL, direction TEXT NOT NULL, rationale TEXT NOT NULL,
                    from_findings TEXT NOT NULL, state TEXT NOT NULL, priority INTEGER NOT NULL,
                    claimed_by TEXT, claimed_at TEXT, prior_claims TEXT NOT NULL,
                    closed_by TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY (board_id, id));
                CREATE TABLE IF NOT EXISTS findings(
                    id TEXT NOT NULL, board_id TEXT NOT NULL REFERENCES boards(id),
                    seq INTEGER NOT NULL, statement TEXT NOT NULL, polarity TEXT NOT NULL,
                    basis TEXT NOT NULL, author TEXT NOT NULL, from_lead TEXT,
                    evidence TEXT NOT NULL, from_findings TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (board_id, id));
                CREATE TABLE IF NOT EXISTS notes(
                    id TEXT NOT NULL, board_id TEXT NOT NULL REFERENCES boards(id),
                    seq INTEGER NOT NULL, text TEXT NOT NULL, about TEXT,
                    created_at TEXT NOT NULL, read_at TEXT, removed_at TEXT,
                    PRIMARY KEY (board_id, id));
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    tool_name TEXT NOT NULL, args_excerpt TEXT NOT NULL, excerpt TEXT NOT NULL,
                    chars INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL, cited INTEGER NOT NULL DEFAULT 0);
                -- What the agent is told: with its next turn, or (wake=1) in a turn of its own.
                CREATE TABLE IF NOT EXISTS notices(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    text TEXT NOT NULL, wake INTEGER NOT NULL DEFAULT 0, board_id TEXT,
                    attempted_at TEXT, delivered_at TEXT);
                -- The workflow: a finished board's write-up, the operator's review, the verification.
                CREATE TABLE IF NOT EXISTS flow(
                    board_id TEXT PRIMARY KEY REFERENCES boards(id), stage TEXT NOT NULL,
                    round INTEGER NOT NULL, report TEXT, report_round INTEGER NOT NULL DEFAULT 0,
                    report_at TEXT, entered_at TEXT NOT NULL,
                    verifier_state TEXT, verifier_token TEXT, verifier_verdict TEXT,
                    verifier_report TEXT, verifier_note TEXT, verifier_started_at TEXT,
                    verifier_told INTEGER NOT NULL DEFAULT 0,
                    own_verdict TEXT, own_report TEXT, verdict TEXT, verification TEXT,
                    verified_at TEXT, accepted_at TEXT, session_key TEXT);
                CREATE TABLE IF NOT EXISTS reports(
                    board_id TEXT NOT NULL REFERENCES boards(id), round INTEGER NOT NULL,
                    text TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (board_id, round));
                CREATE TABLE IF NOT EXISTS review_notes(
                    id TEXT NOT NULL, board_id TEXT NOT NULL REFERENCES boards(id), seq INTEGER NOT NULL,
                    round INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
                    answer TEXT, answered_at TEXT, removed_at TEXT, PRIMARY KEY (board_id, id));
                CREATE TABLE IF NOT EXISTS files(
                    board_id TEXT NOT NULL REFERENCES boards(id), round INTEGER NOT NULL,
                    path TEXT NOT NULL, content TEXT NOT NULL, chars INTEGER NOT NULL,
                    truncated INTEGER NOT NULL, error TEXT, PRIMARY KEY (board_id, round, path));
                """
            )
            found = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if found is not None and found[0] != SCHEMA:
                raise RuntimeError(f"board database {where} has schema {found[0]!r}, expected {SCHEMA!r}")
            db.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (SCHEMA,))
            db.commit()
            self._ready.add(where)

    @staticmethod
    def _board(row: sqlite3.Row) -> Board:
        return Board(
            id=row["id"], name=row["name"], title=row["title"], origin=row["origin"],
            goal=row["goal"], goal_test=row["goal_test"], goal_test_kind=row["goal_test_kind"],
            status=row["status"], step_budget=row["step_budget"], steps_used=row["steps_used"],
            created_at=row["created_at"], closed_at=row["closed_at"], outcome=row["outcome"],
        )

    @staticmethod
    def _lead(row: sqlite3.Row) -> Lead:
        return Lead(
            id=row["id"], board_id=row["board_id"], direction=row["direction"],
            rationale=row["rationale"], from_findings=json.loads(row["from_findings"]),
            state=row["state"], priority=row["priority"], claimed_by=row["claimed_by"],
            claimed_at=row["claimed_at"], prior_claims=json.loads(row["prior_claims"]),
            closed_by=row["closed_by"], created_at=row["created_at"],
        )

    @staticmethod
    def _finding(row: sqlite3.Row) -> Finding:
        return Finding(
            id=row["id"], board_id=row["board_id"], statement=row["statement"],
            polarity=row["polarity"], basis=row["basis"], author=row["author"],
            from_lead=row["from_lead"], evidence=json.loads(row["evidence"]),
            from_findings=json.loads(row["from_findings"]), created_at=row["created_at"],
        )

    @staticmethod
    def _note(row: sqlite3.Row) -> Note:
        return Note(
            id=row["id"], board_id=row["board_id"], text=row["text"], about=row["about"],
            created_at=row["created_at"], read_at=row["read_at"],
        )

    @staticmethod
    def _next(db: sqlite3.Connection, table: str, board_id: Optional[str] = None) -> int:
        if board_id is None:
            row = db.execute(f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table}").fetchone()
        else:
            row = db.execute(
                f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table} WHERE board_id=?", (board_id,)
            ).fetchone()
        return int(row[0])

    def _get_board(self, db: sqlite3.Connection, name: str) -> Optional[Board]:
        # A live board wins; otherwise the newest closed one, so old references resolve.
        row = db.execute(
            "SELECT * FROM boards WHERE name=? ORDER BY (status IN ('open','suspended')) DESC, "
            "seq DESC LIMIT 1",
            (name,),
        ).fetchone()
        return self._board(row) if row else None

    def _must_board(self, db: sqlite3.Connection, name: str) -> Board:
        board = self._get_board(db, str(name or "").strip())
        if board is None:
            raise Refused(f"no board named '{name}' (see search_board_list)")
        return board

    @staticmethod
    def _must_accept(board: Board, who: str = "the operator") -> None:
        if board.status == "suspended":
            raise Refused(
                f"'{board.name}' spent its {board.step_budget}-step budget and is suspended. "
                f"The graph is intact; ask {who} to extend it (/sboard extend {board.name} <steps>)."
            )
        if board.status != "open":
            raise Refused(f"'{board.name}' is closed ({board.status})")

    @staticmethod
    def _spend(db: sqlite3.Connection, board: Board) -> None:
        used = board.steps_used + 1
        status = "suspended" if used >= board.step_budget and board.status == "open" else board.status
        db.execute("UPDATE boards SET steps_used=?, status=? WHERE id=?", (used, status, board.id))

    # -- reads

    def board(self, name: str) -> Optional[Board]:
        with self._db() as db:
            return self._get_board(db, str(name or "").strip())

    def boards(self, include_closed: bool = False) -> list[Board]:
        with self._db() as db:
            if include_closed:
                rows = db.execute("SELECT * FROM boards ORDER BY seq DESC").fetchall()
            else:
                # Live boards, proposals, and finished boards still in the workflow (a write-up
                # due, in review, in verification).
                rows = db.execute(
                    "SELECT b.* FROM boards b LEFT JOIN flow f ON f.board_id=b.id "
                    "WHERE b.status IN (?,?,?,?) OR f.stage IN ('review','verification') "
                    "OR (b.status='solved' AND COALESCE(f.stage,'working')='working') ORDER BY b.seq DESC",
                    LIVE + AWAITING_OPERATOR,
                ).fetchall()
            return [self._board(row) for row in rows]

    def any_live(self) -> bool:
        with self._db() as db:
            row = db.execute("SELECT 1 FROM boards WHERE status='open' LIMIT 1").fetchone()
            return row is not None

    def snapshot(self, name: str) -> Snapshot:
        with self._db() as db:
            board = self._must_board(db, name)
            return self._snapshot(db, board)

    def _snapshot(self, db: sqlite3.Connection, board: Board) -> Snapshot:
        leads = [self._lead(r) for r in db.execute(
            "SELECT * FROM leads WHERE board_id=? ORDER BY seq", (board.id,))]
        findings = [self._finding(r) for r in db.execute(
            "SELECT * FROM findings WHERE board_id=? ORDER BY seq", (board.id,))]
        notes = [self._note(r) for r in db.execute(
            "SELECT * FROM notes WHERE board_id=? AND removed_at IS NULL ORDER BY seq", (board.id,))]
        return Snapshot(board=board, leads=leads, findings=findings, notes=notes)

    def frontier(self, board: Board, at: Optional[datetime] = None) -> list[Lead]:
        at = at or now_utc()
        with self._db() as db:
            leads = [self._lead(r) for r in db.execute(
                "SELECT * FROM leads WHERE board_id=? AND state IN ('open','claimed') "
                "ORDER BY priority DESC, seq", (board.id,))]
        return [lead for lead in leads if lead.on_frontier(at)]

    def unread_notes(self, board: Board) -> int:
        with self._db() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM notes WHERE board_id=? AND removed_at IS NULL AND read_at IS NULL",
                (board.id,),
            ).fetchone()
            return int(row[0])

    def counts(self, board: Board, at: Optional[datetime] = None) -> dict:
        at = at or now_utc()
        frontier = len(self.frontier(board, at))
        with self._db() as db:
            findings = db.execute(
                "SELECT COUNT(*) FROM findings WHERE board_id=?", (board.id,)).fetchone()[0]
            flow = self._flow(db, board)
        return {"frontier": frontier, "findings": int(findings), "unread_notes": self.unread_notes(board),
                "stage": flow.stage, "round": flow.round, "workflow": self.workflow_state(board, flow)}

    def workflow_state(self, board: "Board", flow: "Flow") -> Optional[str]:
        """One phrase for where a finished board stands, or None while it is plain search."""
        if flow.stage == "working":
            if self.write_up_due(board, flow):
                return "write-up due"
            return f"round {flow.round}" if flow.round > 1 and board.live else None
        if flow.stage == "review":
            return "in review"
        if flow.stage == "verification":
            if flow.verification is not None:
                return f"verified ({flow.verdict}): accept or send back"
            if flow.own_report is None:
                return "in verification: the agent's own check due"
            return f"in verification: verifier {flow.verifier_state or 'stopped'}"
        if flow.stage == "deliverable":
            return "complete"
        return flow.stage

    def evidence(self, ids: list[int]) -> list[Evidence]:
        if not ids:
            return []
        with self._db() as db:
            marks = ",".join("?" for _ in ids)
            rows = db.execute(f"SELECT * FROM evidence WHERE id IN ({marks})", list(ids)).fetchall()
            return [Evidence(
                id=r["id"], created_at=r["created_at"], tool_name=r["tool_name"],
                args_excerpt=r["args_excerpt"], excerpt=r["excerpt"], chars=r["chars"],
                sha256=r["sha256"], conversation_id=r["conversation_id"],
                session_id=r["session_id"], tool_call_id=r["tool_call_id"],
            ) for r in rows]

    # -- the model's writes

    def open_board(self, *, name: str, title: str, origin: str, goal: str, goal_test: str,
                   goal_test_kind: str, step_budget: Optional[int] = None) -> Board:
        name = valid_name(name)
        title = bounded(title, MIN_TEXT, MAX_TITLE, "title")
        origin = bounded(origin, MIN_TEXT, MAX_ORIGIN, "origin")
        goal = bounded(goal, MIN_TEXT, MAX_GOAL, "goal")
        goal_test = bounded(goal_test, 1, MAX_GOAL_TEST, "goal_test")
        if goal_test_kind not in TEST_KINDS:
            raise Refused(f"goal_test_kind must be one of {', '.join(TEST_KINDS)}")
        budget = DEFAULT_STEP_BUDGET if step_budget is None else int(step_budget)
        if not 1 <= budget <= MAX_STEP_BUDGET:
            raise Refused(f"step_budget must be between 1 and {MAX_STEP_BUDGET}")
        with self._db(write=True) as db:
            if db.execute("SELECT 1 FROM boards WHERE name=? AND status IN ('open','suspended')",
                          (name,)).fetchone():
                raise Refused(f"a board named '{name}' is already open")
            seq = self._next(db, "boards")
            board_id = f"b{seq}"
            db.execute(
                "INSERT INTO boards VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (board_id, seq, name, title, origin, goal, goal_test, goal_test_kind, "open",
                 budget, 0, stamp(), None, None),
            )
            return self._board(db.execute("SELECT * FROM boards WHERE id=?", (board_id,)).fetchone())

    def declare_leads(self, name: str, leads: list[dict], at: Optional[datetime] = None) -> tuple[list[Lead], list[str]]:
        at = at or now_utc()
        if not isinstance(leads, list) or not 1 <= len(leads) <= MAX_LEADS_PER_CALL:
            raise Refused(f"declare between 1 and {MAX_LEADS_PER_CALL} leads per call")
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            self._must_accept(board)
            existing = [self._lead(r) for r in db.execute(
                "SELECT * FROM leads WHERE board_id=? ORDER BY seq", (board.id,))]
            known = {r[0] for r in db.execute("SELECT id FROM findings WHERE board_id=?", (board.id,))}
            open_now = sum(1 for lead in existing if lead.on_frontier(at))
            if open_now + len(leads) > MAX_FRONTIER:
                raise Refused(
                    f"the frontier is at {open_now} of {MAX_FRONTIER}. Explore or rule out something "
                    "before declaring more: a frontier that grows faster than it is explored is a "
                    "list nobody reads."
                )
            # Validate the whole batch before writing any of it.
            prepared = []
            for item in leads:
                if not isinstance(item, dict):
                    raise Refused("each lead is an object with direction and rationale")
                direction = bounded(item.get("direction"), MIN_TEXT, MAX_DIRECTION, "direction")
                rationale = bounded(item.get("rationale"), MIN_TEXT, MAX_RATIONALE, "rationale")
                sources = [str(x).strip().upper() for x in (item.get("from_findings") or [])]
                missing = [x for x in sources if x not in known]
                if missing:
                    raise Refused(f"from_findings {missing} are not findings on this board")
                try:
                    priority = int(item.get("priority", 5))
                except (TypeError, ValueError):
                    priority = 5
                prepared.append((direction, rationale, sources, max(0, min(priority, 9))))
            warnings = []
            made = []
            for direction, rationale, sources, priority in prepared:
                for other in existing + made:
                    if other.state != "ruled_out" and similarity(direction, other.direction) >= SIMILARITY_WARN:
                        warnings.append(
                            f"'{direction}' reads a lot like {other.id} ('{other.direction}'). "
                            "If they are the same direction, rule one out."
                        )
                seq = self._next(db, "leads", board.id)
                lead_id = f"L{seq}"
                db.execute(
                    "INSERT INTO leads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (lead_id, board.id, seq, direction, rationale, json.dumps(sources), "open",
                     priority, None, None, "[]", None, stamp(at)),
                )
                made.append(self._lead(db.execute(
                    "SELECT * FROM leads WHERE board_id=? AND id=?", (board.id, lead_id)).fetchone()))
            return made, warnings

    def claim(self, name: str, lead_id: str, worker: str, *, one_at_a_time: bool = False,
              at: Optional[datetime] = None) -> Lead:
        at = at or now_utc()
        lead_id = str(lead_id or "").strip().upper()
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            self._must_accept(board)
            row = db.execute("SELECT * FROM leads WHERE board_id=? AND id=?", (board.id, lead_id)).fetchone()
            if row is None:
                raise Refused(f"no lead '{lead_id}' on '{board.name}'")
            lead = self._lead(row)
            if one_at_a_time:
                for r in db.execute("SELECT * FROM leads WHERE board_id=? AND state='claimed'", (board.id,)):
                    held = self._lead(r)
                    if held.claimed_by == worker and held.id != lead.id and not held.claim_expired(at):
                        raise Refused(f"you already hold {held.id}; record its finding before claiming another")
            if lead.state == "claimed" and lead.claimed_by == worker and not lead.claim_expired(at):
                return lead
            if lead.state == "claimed" and not lead.claim_expired(at):
                raise Refused(f"already_claimed: {lead.id} is held by {lead.claimed_by}")
            if lead.state in ("explored", "ruled_out"):
                raise Refused(f"{lead.id} is {lead.state.replace('_', ' ')}; pick a lead on the frontier")
            prior = lead.prior_claims + ([lead.claimed_by] if lead.claimed_by else [])
            db.execute(
                "UPDATE leads SET state='claimed', claimed_by=?, claimed_at=?, prior_claims=? "
                "WHERE board_id=? AND id=?",
                (worker, stamp(at), json.dumps(prior[-5:]), board.id, lead.id),
            )
            return self._lead(db.execute(
                "SELECT * FROM leads WHERE board_id=? AND id=?", (board.id, lead.id)).fetchone())

    def record_finding(self, name: str, *, statement: str, polarity: str, basis: str,
                       evidence: list, worker: str, lead_id: Optional[str] = None,
                       from_findings: Optional[list] = None, author: str = "model",
                       must_name_lead: bool = False, at: Optional[datetime] = None) -> tuple[Finding, Board]:
        at = at or now_utc()
        statement = bounded(statement, MIN_TEXT, MAX_STATEMENT, "statement")
        if polarity not in POLARITIES:
            raise Refused("polarity must be 'supports' or 'rules_out'")
        if basis not in BASES:
            raise Refused("basis must be 'observed' or 'inferred'")
        try:
            evidence_ids = sorted({int(str(x).lstrip("eE#")) for x in (evidence or [])})
        except ValueError:
            raise Refused("evidence ids are the numbers in a tool result's [evidence e<N>] tag, e.g. 12 or 'e12'")
        if not evidence_ids:
            raise Refused(
                "a finding needs evidence: the e<N> of each tool result that shows it. Without it this "
                "is a thing you said, not a thing the search established."
            )
        if len(evidence_ids) > MAX_EVIDENCE_PER_FINDING:
            raise Refused(f"at most {MAX_EVIDENCE_PER_FINDING} evidence ids per finding")
        sources = [str(x).strip().upper() for x in (from_findings or [])]
        if basis == "inferred" and not sources:
            raise Refused("an inferred finding has to cite the findings it came from in from_findings")
        if must_name_lead and not lead_id:
            raise Refused("name the lead you claimed: a subagent's finding is what its exploration of that lead showed")

        with self._db(write=True) as db:
            board = self._must_board(db, name)
            self._must_accept(board)
            marks = ",".join("?" for _ in evidence_ids)
            found = {r[0] for r in db.execute(f"SELECT id FROM evidence WHERE id IN ({marks})", evidence_ids)}
            unknown = [f"e{x}" for x in evidence_ids if x not in found]
            if unknown:
                raise Refused(
                    f"no recorded tool result has evidence id {', '.join(unknown)}. Cite the [evidence e<N>] "
                    "tag at the end of a tool result you actually received."
                )
            known = {r[0] for r in db.execute("SELECT id FROM findings WHERE board_id=?", (board.id,))}
            missing = [x for x in sources if x not in known]
            if missing:
                raise Refused(f"from_findings {missing} are not findings on this board")

            attached = None
            if lead_id:
                lead_id = str(lead_id).strip().upper()
                row = db.execute("SELECT * FROM leads WHERE board_id=? AND id=?", (board.id, lead_id)).fetchone()
                if row is None:
                    raise Refused(f"no lead '{lead_id}' on this board")
                lead = self._lead(row)
                if lead.state == "claimed" and lead.claimed_by == worker:
                    pass
                elif lead.state == "claimed" and not lead.claim_expired(at):
                    raise Refused(f"{lead.id} is held by {lead.claimed_by}, not you; claim a lead before writing a finding against it")
                elif lead.state == "explored":
                    raise Refused(f"{lead.id} has already been explored. One finding per exploration: "
                                  "if you learned something further, it belongs to a new lead.")
                elif lead.state == "ruled_out":
                    raise Refused(f"{lead.id} was ruled out")
                else:
                    raise Refused(f"claim {lead.id} with search_board_claim before exploring it")
                attached = lead.id

            seq = self._next(db, "findings", board.id)
            finding_id = f"F{seq}"
            db.execute(
                "INSERT INTO findings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (finding_id, board.id, seq, statement, polarity, basis, author, attached,
                 json.dumps(evidence_ids), json.dumps(sources), stamp(at)),
            )
            db.execute(f"UPDATE evidence SET cited=1 WHERE id IN ({marks})", evidence_ids)
            if attached:
                db.execute("UPDATE leads SET state='explored', closed_by=? WHERE board_id=? AND id=?",
                           (finding_id, board.id, attached))
            self._spend(db, board)
            finding = self._finding(db.execute(
                "SELECT * FROM findings WHERE board_id=? AND id=?", (board.id, finding_id)).fetchone())
            after = self._board(db.execute("SELECT * FROM boards WHERE id=?", (board.id,)).fetchone())
            return finding, after

    def rule_out(self, name: str, lead_id: str, finding_id: str, worker: str, *,
                 respect_claims: bool = False, at: Optional[datetime] = None) -> Lead:
        at = at or now_utc()
        lead_id = str(lead_id or "").strip().upper()
        finding_id = str(finding_id or "").strip().upper()
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            self._must_accept(board)
            row = db.execute("SELECT * FROM findings WHERE board_id=? AND id=?", (board.id, finding_id)).fetchone()
            if row is None:
                raise Refused(f"no finding '{finding_id}' on this board")
            finding = self._finding(row)
            if finding.polarity != "rules_out":
                raise Refused(f"{finding.id} is a 'supports' finding. Ruling a lead out needs a finding that "
                              "rules it out; otherwise this is losing interest, not eliminating.")
            row = db.execute("SELECT * FROM leads WHERE board_id=? AND id=?", (board.id, lead_id)).fetchone()
            if row is None:
                raise Refused(f"no lead '{lead_id}' on this board")
            lead = self._lead(row)
            if lead.state in ("explored", "ruled_out"):
                raise Refused(f"{lead.id} is already {lead.state.replace('_', ' ')}")
            if (respect_claims and lead.state == "claimed" and not lead.claim_expired(at)
                    and lead.claimed_by != worker):
                raise Refused(f"{lead.id} is being explored by {lead.claimed_by}; leave it to them, or say "
                              "in your report which finding you think rules it out")
            db.execute("UPDATE leads SET state='ruled_out', closed_by=? WHERE board_id=? AND id=?",
                       (finding.id, board.id, lead.id))
            return self._lead(db.execute(
                "SELECT * FROM leads WHERE board_id=? AND id=?", (board.id, lead.id)).fetchone())

    def close(self, name: str, status: str, rationale: str, at: Optional[datetime] = None) -> Board:
        at = at or now_utc()
        if status == "solved":
            raise Refused("you cannot close a board solved. A passing goal test does that; on a judgment "
                          "or observation board the most you can reach is proposed_solved.")
        if status == "exhausted":
            raise Refused("you cannot close a board exhausted: the operator does. Close it proposed_exhausted "
                          "with the argument that nothing is left to try.")
        if status not in ("proposed_solved", "proposed_exhausted", "abandoned"):
            raise Refused("close as proposed_solved, proposed_exhausted or abandoned")
        rationale = bounded(rationale, MIN_TEXT, MAX_RATIONALE, "rationale")
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            if not board.live:
                raise Refused(f"'{board.name}' is already closed ({board.status})")
            if status == "proposed_exhausted":
                leads = [self._lead(r) for r in db.execute(
                    "SELECT * FROM leads WHERE board_id=? AND state IN ('open','claimed')", (board.id,))]
                left = [lead for lead in leads if lead.on_frontier(at) or lead.state == "claimed"]
                if left:
                    raise Refused(
                        f"{len(left)} leads are still on the frontier or being explored. Exhausted means "
                        "nothing is left to try: rule out what the findings eliminated, or close "
                        "'abandoned' if you are stopping for another reason."
                    )
            db.execute("UPDATE boards SET status=?, closed_at=?, outcome=? WHERE id=?",
                       (status, stamp(at), rationale, board.id))
            return self._board(db.execute("SELECT * FROM boards WHERE id=?", (board.id,)).fetchone())

    def record_test(self, name: str, *, passed: bool, verdict: str, command: str, output: str,
                    worker: str, conversation_id: str = "", session_id: str = "") -> tuple[Finding, Board]:
        """The plugin's own verdict on a goal test: evidence, finding, step and status in one write."""
        body = f"[search-board goal test] board={name}\n$ {command}\nverdict: {verdict}\n{output}"
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            self._must_accept(board)
            cursor = db.execute(
                "INSERT INTO evidence(created_at, tool_name, args_excerpt, excerpt, chars, sha256, "
                "conversation_id, session_id, tool_call_id, cited) VALUES(?,?,?,?,?,?,?,?,?,1)",
                (stamp(), "search_board_test", json.dumps({"command": command})[:ARGS_EXCERPT],
                 excerpt(body), len(body), hashlib.sha256(body.encode("utf-8", "replace")).hexdigest(),
                 conversation_id or "", session_id or "", ""),
            )
            evidence_id = int(cursor.lastrowid)
            seq = self._next(db, "findings", board.id)
            finding_id = f"F{seq}"
            statement = (f"Goal test passed: `{command}` exited 0." if passed
                         else f"Goal test {verdict}: `{command}`.")
            db.execute(
                "INSERT INTO findings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (finding_id, board.id, seq, statement, "supports" if passed else "rules_out",
                 "observed", "plugin", None, json.dumps([evidence_id]), "[]", stamp()),
            )
            self._spend(db, board)
            if passed:
                db.execute("UPDATE boards SET status='solved', closed_at=?, outcome=? WHERE id=?",
                           (stamp(), f"goal test passed; see {finding_id}", board.id))
            finding = self._finding(db.execute(
                "SELECT * FROM findings WHERE board_id=? AND id=?", (board.id, finding_id)).fetchone())
            after = self._board(db.execute("SELECT * FROM boards WHERE id=?", (board.id,)).fetchone())
            return finding, after

    def read_notes(self, board: Board) -> list[Note]:
        """Every standing note, and mark the unread ones read (main agent only)."""
        with self._db(write=True) as db:
            notes = [self._note(r) for r in db.execute(
                "SELECT * FROM notes WHERE board_id=? AND removed_at IS NULL ORDER BY seq", (board.id,))]
            db.execute("UPDATE notes SET read_at=? WHERE board_id=? AND removed_at IS NULL AND read_at IS NULL",
                       (stamp(), board.id))
            return notes

    # -- the evidence ledger

    def record_evidence(self, *, tool_name: str, args: object, result: str, conversation_id: str = "",
                        session_id: str = "", tool_call_id: str = "") -> int:
        try:
            args_text = json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            args_text = str(args)
        with self._db(write=True) as db:
            cursor = db.execute(
                "INSERT INTO evidence(created_at, tool_name, args_excerpt, excerpt, chars, sha256, "
                "conversation_id, session_id, tool_call_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (stamp(), str(tool_name)[:200], args_text[:ARGS_EXCERPT], excerpt(result), len(result),
                 hashlib.sha256(result.encode("utf-8", "replace")).hexdigest(),
                 conversation_id or "", session_id or "", tool_call_id or ""),
            )
            return int(cursor.lastrowid)

    def prune_evidence(self, keep_days: int = EVIDENCE_KEEP_DAYS) -> int:
        cutoff = stamp(now_utc() - timedelta(days=keep_days))
        with self._db(write=True) as db:
            cursor = db.execute("DELETE FROM evidence WHERE cited=0 AND created_at < ?", (cutoff,))
            return cursor.rowcount

    # -- the operator's writes

    def add_note(self, name: str, text: str) -> tuple[Note, Board]:
        text = bounded(text, 1, MAX_NOTE, "note")
        about = None
        match = re.match(r"^\s*\[?([LF]\d+)\]?[:\s]\s*(.+)$", text, re.S | re.I)
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            if board.status in ("solved", "exhausted", "abandoned"):
                raise Refused(f"'{board.name}' is closed ({board.status})")
            if match:
                candidate = match.group(1).upper()
                table = "leads" if candidate.startswith("L") else "findings"
                if db.execute(f"SELECT 1 FROM {table} WHERE board_id=? AND id=?", (board.id, candidate)).fetchone():
                    about = candidate
                    text = match.group(2).strip()
            seq = self._next(db, "notes", board.id)
            note_id = f"N{seq}"
            db.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?)",
                       (note_id, board.id, seq, text, about, stamp(), None, None))
            note = self._note(db.execute(
                "SELECT * FROM notes WHERE board_id=? AND id=?", (board.id, note_id)).fetchone())
            return note, board

    def remove_note(self, name: str, note_id: str) -> Board:
        note_id = str(note_id or "").strip().upper()
        if note_id.isdigit():
            note_id = f"N{note_id}"
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            cursor = db.execute(
                "UPDATE notes SET removed_at=? WHERE board_id=? AND id=? AND removed_at IS NULL",
                (stamp(), board.id, note_id))
            if cursor.rowcount == 0:
                raise Refused(f"'{board.name}' has no note {note_id}")
            return board

    def extend(self, name: str, steps: int) -> Board:
        try:
            steps = int(steps)
        except (TypeError, ValueError):
            raise Refused("steps must be a number")
        if not 1 <= steps <= MAX_STEP_BUDGET:
            raise Refused(f"extend by 1..{MAX_STEP_BUDGET} steps")
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            if not board.live:
                raise Refused(f"'{board.name}' is closed ({board.status})")
            budget = min(board.step_budget + steps, board.steps_used + MAX_STEP_BUDGET)
            db.execute("UPDATE boards SET step_budget=?, status='open' WHERE id=?", (budget, board.id))
            return self._board(db.execute("SELECT * FROM boards WHERE id=?", (board.id,)).fetchone())

    def decide(self, name: str, decision: str, why: str = "") -> Board:
        """The operator's word: confirm, exhaust, reject (reopen), or abandon."""
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            if decision == "confirm":
                if board.status != "proposed_solved":
                    raise Refused(f"'{board.name}' is {board.status}, not proposed_solved")
                db.execute("UPDATE boards SET status='solved', outcome=? WHERE id=?",
                           (f"{board.outcome or ''} [confirmed solved by the operator]".strip(), board.id))
            elif decision == "exhaust":
                if board.status != "proposed_exhausted":
                    raise Refused(f"'{board.name}' is {board.status}, not proposed_exhausted")
                db.execute("UPDATE boards SET status='exhausted', outcome=? WHERE id=?",
                           (f"{board.outcome or ''} [closed exhausted by the operator]".strip(), board.id))
            elif decision == "reject":
                if board.status not in AWAITING_OPERATOR:
                    raise Refused(f"'{board.name}' is {board.status}: only a proposal can be sent back")
                if db.execute("SELECT 1 FROM boards WHERE name=? AND status IN ('open','suspended') AND id<>?",
                              (board.name, board.id)).fetchone():
                    raise Refused(f"another board named '{board.name}' is open; close it first")
                status = "suspended" if board.steps_used >= board.step_budget else "open"
                db.execute("UPDATE boards SET status=?, closed_at=NULL WHERE id=?", (status, board.id))
                what = "Not solved" if board.status == "proposed_solved" else "Not exhausted"
                text = f"{what}: the operator sent it back." + (f" {why.strip()}" if why.strip() else "")
                seq = self._next(db, "notes", board.id)
                db.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?)",
                           (f"N{seq}", board.id, seq, text[:MAX_NOTE], None, stamp(), None, None))
            elif decision == "abandon":
                if board.status in ("solved", "exhausted", "abandoned"):
                    raise Refused(f"'{board.name}' is already closed ({board.status})")
                note = "[abandoned by the operator]" + (f" {why.strip()}" if why.strip() else "")
                db.execute("UPDATE boards SET status='abandoned', closed_at=COALESCE(closed_at, ?), outcome=? WHERE id=?",
                           (stamp(), f"{board.outcome or ''} {note}".strip(), board.id))
            else:
                raise Refused(f"unknown decision '{decision}'")
            return self._board(db.execute("SELECT * FROM boards WHERE id=?", (board.id,)).fetchone())


    # ================================================================ the workflow
    #
    # working ──agent: report──▶ review ──operator: back──▶ working (a new round)
    #                              │ └──operator: verify──▶ verification ──operator: accept──▶ deliverable
    #                              │                            └──operator: back / review──▶ …
    #                              └──operator: exhaust (a proposed_exhausted board)──▶ deliverable
    # any stage ──operator: discard──▶ discarded
    #
    # A finished board (solved by its test, proposed_solved or proposed_exhausted) owes a write-up.
    # The operator reviews it and leaves notes; sent back, the board opens again in a new round and
    # the agent must answer every note before writing it up again. Sent to verification, two checks
    # run independently: the plugin's verifier (a separate model call that sees only the board, the
    # write-up, the files and the evidence) and the agent's own. The verifier's report is held from
    # the agent until it has filed its own; then the agent reconciles them, and the operator accepts
    # the board as delivered or sends it back.

    def _flow(self, db: sqlite3.Connection, board: "Board") -> "Flow":
        row = db.execute("SELECT * FROM flow WHERE board_id=?", (board.id,)).fetchone()
        if row is None:
            return Flow(board_id=board.id, stage="working", round=1, entered_at=stamp())
        return Flow(**{key: row[key] for key in row.keys()})

    @staticmethod
    def _put_flow(db: sqlite3.Connection, flow: "Flow") -> None:
        values = flow.__dict__
        columns = ",".join(values)
        marks = ",".join("?" for _ in values)
        db.execute(f"INSERT OR REPLACE INTO flow({columns}) VALUES({marks})", list(values.values()))

    @staticmethod
    def _review_note(row: sqlite3.Row) -> "ReviewNote":
        return ReviewNote(id=row["id"], board_id=row["board_id"], round=row["round"], text=row["text"],
                          created_at=row["created_at"], answer=row["answer"], answered_at=row["answered_at"])

    def _review_notes(self, db: sqlite3.Connection, board: "Board") -> list:
        return [self._review_note(r) for r in db.execute(
            "SELECT * FROM review_notes WHERE board_id=? AND removed_at IS NULL ORDER BY seq", (board.id,))]

    def _push(self, db: sqlite3.Connection, text: str, *, wake: bool, board: Optional["Board"] = None) -> None:
        db.execute("INSERT INTO notices(created_at, text, wake, board_id) VALUES(?,?,?,?)",
                   (stamp(), text, 1 if wake else 0, board.id if board else None))

    @staticmethod
    def _reopen(db: sqlite3.Connection, board: "Board") -> None:
        if db.execute("SELECT 1 FROM boards WHERE name=? AND status IN ('open','suspended') AND id<>?",
                      (board.name, board.id)).fetchone():
            raise Refused(f"another board named '{board.name}' is open; close it before reopening this one")
        status = "suspended" if board.steps_used >= board.step_budget else "open"
        db.execute("UPDATE boards SET status=?, closed_at=NULL, outcome=NULL WHERE id=?", (status, board.id))

    # -- reads

    def flow(self, name: str) -> tuple["Board", "Flow"]:
        with self._db() as db:
            board = self._must_board(db, name)
            return board, self._flow(db, board)

    def workflow(self, name: str) -> "Workflow":
        """Everything the workflow holds for a board: the flow, every round's report, the review
        notes and the latest round's delivered files (without their content)."""
        with self._db() as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            reports = [dict(round=r["round"], text=r["text"], at=r["at"]) for r in db.execute(
                "SELECT * FROM reports WHERE board_id=? ORDER BY round", (board.id,))]
            files = [DeliveredFile(path=r["path"], chars=r["chars"], truncated=bool(r["truncated"]), error=r["error"])
                     for r in db.execute("SELECT path, chars, truncated, error FROM files WHERE board_id=? AND round=? "
                                         "ORDER BY path", (board.id, flow.report_round))]
            return Workflow(board=board, flow=flow, reports=reports, notes=self._review_notes(db, board), files=files)

    def delivered_file(self, name: str, path: str) -> tuple["DeliveredFile", str]:
        with self._db() as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            row = db.execute("SELECT * FROM files WHERE board_id=? AND round=? AND path=?",
                             (board.id, flow.report_round, path)).fetchone()
            if row is None:
                raise Refused(f"'{board.name}' delivered no file '{path}'")
            return (DeliveredFile(path=row["path"], chars=row["chars"], truncated=bool(row["truncated"]),
                                  error=row["error"]), row["content"])

    def write_up_due(self, board: "Board", flow: "Flow") -> bool:
        return (flow.stage == "working" and board.status in FINISHED
                and not (flow.report is not None and flow.report_round == flow.round))

    def waiting_for_operator(self, board: "Board", flow: "Flow") -> Optional[str]:
        """What the operator is asked to do with this board, if anything."""
        if flow.stage == "review":
            return "review"
        if flow.stage == "verification" and flow.verification is not None:
            return "accept or send back"
        if flow.stage == "working" and board.status in AWAITING_OPERATOR and flow.report is None:
            return None  # a write-up is due first
        return None

    # -- the agent's writes

    def remember_session(self, name: str, session_key: str) -> None:
        """The session that works on a board: where a turn is started when the operator moves it."""
        if not session_key:
            return
        with self._db(write=True) as db:
            board = self._get_board(db, str(name or "").strip())
            if board is None:
                return
            flow = self._flow(db, board)
            if flow.session_key != session_key:
                flow.session_key = session_key
                self._put_flow(db, flow)

    def submit_report(self, name: str, report: str, files: list) -> tuple["Board", "Flow"]:
        report = bounded(report, MIN_REPORT, MAX_REPORT, "report")
        if len(files) > MAX_FILES:
            raise Refused(f"at most {MAX_FILES} files")
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            if board.status not in FINISHED:
                raise Refused(f"'{board.name}' is {board.status}: close it proposed_solved or proposed_exhausted "
                              "(or pass its goal test) before writing it up")
            if flow.stage != "working":
                raise Refused(f"'{board.name}' is in the operator's {flow.stage} already")
            open_notes = [n.id for n in self._review_notes(db, board) if n.answer is None]
            if open_notes:
                raise Refused(f"answer the operator's review notes first (search_board_answer): {', '.join(open_notes)}")
            at = stamp()
            db.execute("INSERT OR REPLACE INTO reports VALUES(?,?,?,?)", (board.id, flow.round, report, at))
            db.execute("DELETE FROM files WHERE board_id=? AND round=?", (board.id, flow.round))
            for f in files:
                db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
                           (board.id, flow.round, f["path"], f.get("content") or "", int(f.get("chars") or 0),
                            1 if f.get("truncated") else 0, f.get("error")))
            flow.report, flow.report_round, flow.report_at = report, flow.round, at
            flow.stage, flow.entered_at = "review", at
            self._put_flow(db, flow)
            return board, flow

    def answer(self, name: str, note_id: str, answer: str) -> list:
        answer = bounded(answer, 2, MAX_ANSWER, "answer")
        note_id = str(note_id or "").strip().upper()
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            if flow.stage != "working":
                raise Refused(f"'{board.name}' is in the operator's {flow.stage}: nothing to answer now")
            cursor = db.execute("UPDATE review_notes SET answer=?, answered_at=? WHERE board_id=? AND id=? "
                                "AND removed_at IS NULL", (answer, stamp(), board.id, note_id))
            if cursor.rowcount == 0:
                raise Refused(f"no review note '{note_id}' on '{board.name}'")
            return [n.id for n in self._review_notes(db, board) if n.answer is None]

    def own_check(self, name: str, verdict: str, findings: str) -> "Flow":
        findings = bounded(findings, MIN_REPORT, MAX_VERIFY, "findings")
        if verdict not in ("passed", "failed"):
            raise Refused("verdict is passed or failed")
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            if flow.stage != "verification":
                raise Refused(f"'{board.name}' is not in verification (it is {flow.stage})")
            if flow.own_report is not None:
                raise Refused("you filed your own check already; reconcile with search_board_verification")
            flow.own_verdict, flow.own_report = verdict, findings
            if flow.verifier_state in ("done", "failed"):
                flow.verifier_told = 1  # it comes back in this call's answer
            self._put_flow(db, flow)
            return flow

    def reconcile(self, name: str, verdict: str, report: str) -> "Flow":
        report = bounded(report, MIN_REPORT, MAX_VERIFY, "report")
        if verdict not in ("passed", "failed"):
            raise Refused("verdict is passed or failed")
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            if flow.stage != "verification":
                raise Refused(f"'{board.name}' is not in verification (it is {flow.stage})")
            if flow.own_report is None:
                raise Refused("file your own check first (search_board_verify)")
            if flow.verifier_state not in ("done", "failed"):
                raise Refused("the independent verifier has not finished; reconcile when its report comes")
            flow.verdict, flow.verification, flow.verified_at = verdict, report, stamp()
            self._put_flow(db, flow)
            return flow

    # -- the operator's moves

    def comment(self, name: str, text: str) -> "ReviewNote":
        text = bounded(text, 1, MAX_NOTE, "note")
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            if flow.stage != "review":
                raise Refused(f"'{board.name}' is not in review (it is {flow.stage})")
            seq = self._next(db, "review_notes", board.id)
            note_id = f"R{seq}"
            db.execute("INSERT INTO review_notes VALUES(?,?,?,?,?,?,?,?,?)",
                       (note_id, board.id, seq, flow.round, text, stamp(), None, None, None))
            return self._review_note(db.execute(
                "SELECT * FROM review_notes WHERE board_id=? AND id=?", (board.id, note_id)).fetchone())

    def uncomment(self, name: str, note_id: str) -> "Board":
        note_id = str(note_id or "").strip().upper()
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            cursor = db.execute("UPDATE review_notes SET removed_at=? WHERE board_id=? AND id=? AND answer IS NULL "
                                "AND removed_at IS NULL", (stamp(), board.id, note_id))
            if cursor.rowcount == 0:
                raise Refused(f"'{board.name}' has no unanswered review note {note_id}")
            return board

    def send_back(self, name: str, why: str = "") -> tuple["Board", "Flow"]:
        why = (why or "").strip()[:MAX_NOTE]
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            source = flow.stage
            if source not in ("review", "verification", "deliverable"):
                raise Refused(f"'{board.name}' is {flow.stage}, not in review, verification or complete")
            open_notes = [n for n in self._review_notes(db, board) if n.answer is None]
            self._reopen(db, board)
            if source != "review" and why:
                where = "complete" if source == "deliverable" else "verification"
                seq = self._next(db, "notes", board.id)
                db.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?)",
                           (f"N{seq}", board.id, seq, f"Sent back from {where}: {why}", None, stamp(), None, None))
            flow.round += 1
            flow.stage, flow.entered_at = "working", stamp()
            flow.verifier_state = flow.verifier_token = None  # a running verifier's report is no one's now
            flow.accepted_at = None
            self._put_flow(db, flow)
            if source == "review":
                listed = "".join(f"\n  {n.id}: {n.text}" for n in open_notes)
                count = f" with {len(open_notes)} note{'s' if len(open_notes) != 1 else ''}" if open_notes else ""
                text = (f"The operator reviewed '{board.name}' and sent it back to you{count}. It is open again "
                        f"(round {flow.round}).{listed}\nAnswer each of their notes with search_board_answer "
                        "(search_board_read shows them), work on what they ask, then close it and write it up again "
                        "with search_board_report: it goes back to their review.")
            elif source == "deliverable":
                text = (f"The operator reopened '{board.name}' from their completed work"
                        f"{': ' + why if why else ''}. It is open again (round {flow.round}). Work on it, then "
                        "close it and write it up again (search_board_report): it goes back to their review.")
            else:
                done = "" if flow.verification else " (unfinished; the verifier's result will be dropped)"
                text = (f"The operator sent '{board.name}' back from verification{': ' + why if why else ''}"
                        f"{done}. It is open again (round {flow.round}). Work on what the verification found, "
                        "then close it and write it up again (search_board_report): it goes back to their review.")
            self._push(db, text, wake=True, board=board)
            after = self._board(db.execute("SELECT * FROM boards WHERE id=?", (board.id,)).fetchone())
            return after, flow

    def to_verification(self, name: str) -> tuple["Board", "Flow"]:
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            if flow.stage not in ("review", "deliverable"):
                raise Refused(f"'{board.name}' is {flow.stage}, not in review or complete")
            if board.status in ("proposed_exhausted", "exhausted"):
                raise Refused(f"'{board.name}' is {board.status.replace('_', ' ')}: there is nothing to verify; "
                              "close it exhausted or send it back")
            again = flow.stage == "deliverable"
            unanswered = sum(1 for n in self._review_notes(db, board) if n.answer is None)
            flow.stage, flow.entered_at, flow.accepted_at = "verification", stamp(), None
            flow.verifier_state, flow.verifier_token = "wanted", secrets.token_hex(8)
            flow.verifier_verdict = flow.verifier_report = flow.verifier_note = flow.verifier_started_at = None
            flow.verifier_told = 0
            flow.own_verdict = flow.own_report = flow.verdict = flow.verification = flow.verified_at = None
            self._put_flow(db, flow)
            open_part = (f" with {unanswered} of their notes still open: answer them in your check"
                         if unanswered else "")
            self._push(db, (
                f"The operator sent '{board.name}' to verification{' again, from their completed work' if again else ''}"
                f"{open_part}. Check it yourself now, independently: from its goal, its test, the write-up and the "
                f"delivered files (search_board_review shows them as delivered), not from memory of having done it. "
                "Run the test, read the files, try what the write-up claims. Then file your check with "
                "search_board_verify. An independent verifier checks it too; its report is held from you until you "
                "file yours. Then reconcile the two with search_board_verification."), wake=True, board=board)
            return board, flow

    def to_review(self, name: str, why: str = "") -> tuple["Board", "Flow"]:
        why = (why or "").strip()
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            source = flow.stage
            if source not in ("verification", "deliverable"):
                raise Refused(f"'{board.name}' is {flow.stage}, not in verification or complete")
            flow.stage, flow.entered_at, flow.accepted_at = "review", stamp(), None
            flow.verifier_state = flow.verifier_token = None
            self._put_flow(db, flow)
            if board.status == "exhausted":
                db.execute("UPDATE boards SET status='proposed_exhausted' WHERE id=?", (board.id,))
            where = "their completed work" if source == "deliverable" else "verification"
            self._push(db, f"- the operator took '{board.name}' back to their review, from {where}"
                           f"{': ' + why if why else ''}. Nothing is asked of you yet.", wake=False, board=board)
            return board, flow

    def accept(self, name: str) -> tuple["Board", "Flow"]:
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            if flow.stage != "verification":
                raise Refused(f"'{board.name}' is not in verification (it is {flow.stage})")
            if flow.verification is None:
                raise Refused(f"'{board.name}' has no verification report yet")
            at = stamp()
            flow.stage, flow.entered_at, flow.accepted_at = "deliverable", at, at
            self._put_flow(db, flow)
            note = f"[verified ({flow.verdict}); accepted by the operator]"
            outcome = f"{(board.outcome or '').replace(note, '').strip()} {note}".strip()
            db.execute("UPDATE boards SET status='solved', closed_at=COALESCE(closed_at, ?), outcome=? WHERE id=?",
                       (at, outcome, board.id))
            self._push(db, f"- the operator accepted '{board.name}' as delivered (verification: {flow.verdict}). "
                           "It is closed and complete.", wake=False, board=board)
            return self._board(db.execute("SELECT * FROM boards WHERE id=?", (board.id,)).fetchone()), flow

    def discard(self, name: str, why: str = "") -> tuple["Board", "Flow"]:
        why = (why or "").strip()
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            if flow.stage == "discarded":
                raise Refused(f"'{board.name}' is discarded already")
            flow.stage, flow.entered_at = "discarded", stamp()
            flow.verifier_state = flow.verifier_token = None
            self._put_flow(db, flow)
            note = "[discarded by the operator]" + (f" {why}" if why else "")
            db.execute("UPDATE boards SET status='abandoned', closed_at=COALESCE(closed_at, ?), outcome=? WHERE id=?",
                       (stamp(), f"{board.outcome or ''} {note}".strip(), board.id))
            self._push(db, f"- the operator discarded '{board.name}'{': ' + why if why else ''}. It is closed "
                           "(abandoned); leave it be.", wake=False, board=board)
            return self._board(db.execute("SELECT * FROM boards WHERE id=?", (board.id,)).fetchone()), flow

    def retry_verifier(self, name: str) -> "Flow":
        with self._db(write=True) as db:
            board = self._must_board(db, name)
            flow = self._flow(db, board)
            if flow.stage != "verification" or flow.verifier_state not in ("failed", None):
                raise Refused(f"'{board.name}' has no failed verifier to run again")
            if flow.verification is not None:
                raise Refused(f"'{board.name}' is reconciled already; send it to verification again instead")
            flow.verifier_state, flow.verifier_token = "wanted", secrets.token_hex(8)
            flow.verifier_report = flow.verifier_note = flow.verifier_verdict = None
            flow.verifier_told = 0
            self._put_flow(db, flow)
            return flow

    def _settle(self, db: sqlite3.Connection, board: "Board", note: str) -> None:
        """confirm/exhaust without going through verification: delivered all the same."""
        flow = self._flow(db, board)
        at = stamp()
        flow.stage, flow.entered_at, flow.accepted_at = "deliverable", at, at
        flow.verifier_state = flow.verifier_token = None
        self._put_flow(db, flow)
        db.execute("UPDATE boards SET closed_at=COALESCE(closed_at, ?) WHERE id=?", (at, board.id))

    # -- the operator's older words, kept: they now move the workflow too

    def operator_extend(self, name: str, steps) -> "Board":
        board = self.extend(name, steps)
        self.add_notice(f"- the operator extended '{board.name}' to {board.step_budget} steps; it is "
                        f"{board.status} ({board.steps_used} used)", wake=True, board=board)
        return board

    def operator_decide(self, name: str, decision: str, why: str = "") -> "Board":
        if decision == "abandon":
            return self.discard(name, why)[0]
        board, flow = self.flow(name)
        if decision == "reject" and flow.stage in ("review", "verification", "deliverable"):
            return self.send_back(name, why)[0]
        before = board
        board = self.decide(name, decision, why)
        said = f" They said: {why.strip()}" if (why or "").strip() else ""
        if decision in ("confirm", "exhaust"):
            with self._db(write=True) as db:
                self._settle(db, board, decision)
            word = "confirmed '{}' solved" if decision == "confirm" else "closed '{}' exhausted, as you proposed"
            self.add_notice("- the operator " + word.format(board.name), board=board)
        elif decision == "reject":
            what = "not solved" if before.status == "proposed_solved" else "not exhausted"
            self.add_notice(f"The operator sent '{board.name}' back: {what}.{said} It is {board.status} again, and "
                            "the reason is a note on it: read the board (search_board_read) and go on.",
                            wake=True, board=board)
        return board

    # -- the verifier's side (run by the plugin, never by the agent)

    def verifiers_wanted(self) -> list:
        with self._db() as db:
            return [(r["board_id"], r["verifier_token"]) for r in db.execute(
                "SELECT board_id, verifier_token FROM flow WHERE stage='verification' AND verifier_state='wanted'")]

    def revive_stale_verifiers(self, minutes: int = 15) -> int:
        """A verifier left running by a process that went away is wanted again (a new token drops
        the old one's report, should it ever come)."""
        cutoff = stamp(now_utc() - timedelta(minutes=minutes))
        with self._db(write=True) as db:
            stale = db.execute("SELECT board_id FROM flow WHERE verifier_state='running' AND "
                               "(verifier_started_at IS NULL OR verifier_started_at < ?)", (cutoff,)).fetchall()
            for row in stale:
                db.execute("UPDATE flow SET verifier_state='wanted', verifier_token=? WHERE board_id=?",
                           (secrets.token_hex(8), row["board_id"]))
            return len(stale)

    def verifier_start(self, board_id: str, token: str) -> bool:
        with self._db(write=True) as db:
            cursor = db.execute(
                "UPDATE flow SET verifier_state='running', verifier_started_at=? WHERE board_id=? "
                "AND verifier_token=? AND verifier_state='wanted' AND stage='verification'",
                (stamp(), board_id, token))
            return cursor.rowcount == 1

    def verifier_material(self, board_id: str) -> dict:
        """All the verifier sees: the board, the write-up, the files as delivered, the review, and every
        finding with the tool output it cites. Nothing of the agent's conversation."""
        with self._db() as db:
            row = db.execute("SELECT * FROM boards WHERE id=?", (board_id,)).fetchone()
            if row is None:
                raise Refused(f"no board {board_id}")
            board = self._board(row)
            flow = self._flow(db, board)
            snap = self._snapshot(db, board)
            files = [dict(path=r["path"], content=r["content"], truncated=bool(r["truncated"]), error=r["error"])
                     for r in db.execute("SELECT * FROM files WHERE board_id=? AND round=? ORDER BY path",
                                         (board.id, flow.report_round))]
            cited = sorted({e for f in snap.findings for e in f.evidence})
        evidence = {e.id: e for e in self.evidence(cited)}
        return dict(board=board, report=flow.report or "", files=files, findings=snap.findings,
                    evidence=evidence, notes=self.workflow(board.name).notes, leads=snap.leads)

    def verifier_finish(self, board_id: str, token: str, *, ok: bool, verdict: Optional[str],
                        report: str, note: Optional[str] = None) -> Optional[str]:
        """Keep the verifier's report on the board, not with the agent. Returns the text to wake the
        agent with when it has already filed its own check (its report is no longer held), else None."""
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM boards WHERE id=?", (board_id,)).fetchone()
            if row is None:
                return None
            board = self._board(row)
            flow = self._flow(db, board)
            if flow.verifier_token != token or flow.verifier_state != "running":
                return None  # the board moved on: the report is no one's
            flow.verifier_state = "done" if ok else "failed"
            flow.verifier_verdict = verdict if ok else None
            flow.verifier_report = (report or "")[:MAX_VERIFY]
            flow.verifier_note = note
            text = None
            if flow.own_report is not None and not flow.verifier_told:
                flow.verifier_told = 1
                body = flow.verifier_report if ok else f"(the verifier failed: {note})"
                text = (f"[search-board: the independent verifier's report, not operator input]\nThe verifier of "
                        f"'{board.name}' finished ({flow.verifier_state}, verdict {flow.verifier_verdict or 'none'}). "
                        f"Reconcile it with your own check in search_board_verification.\n\n{body}")
                self._push(db, text, wake=True, board=board)
            self._put_flow(db, flow)
            return text

    # -- notices: what the agent is told

    def add_notice(self, text: str, *, wake: bool = False, board: Optional["Board"] = None) -> None:
        with self._db(write=True) as db:
            self._push(db, text, wake=wake, board=board)

    def wake_due(self) -> list:
        """Notices that should start a turn and have not been tried: (id, board's session key, text)."""
        with self._db() as db:
            return [(r["id"], r["session_key"], r["text"]) for r in db.execute(
                "SELECT n.id, n.text, f.session_key FROM notices n LEFT JOIN flow f ON f.board_id=n.board_id "
                "WHERE n.wake=1 AND n.attempted_at IS NULL AND n.delivered_at IS NULL ORDER BY n.id")]

    def mark_woken(self, notice_id: int, delivered: bool) -> None:
        with self._db(write=True) as db:
            db.execute("UPDATE notices SET attempted_at=?, delivered_at=CASE WHEN ? THEN ? ELSE delivered_at END "
                       "WHERE id=?", (stamp(), 1 if delivered else 0, stamp(), notice_id))

    def take_notices(self) -> list:
        """Everything not told yet (a wake that could not start a turn included), told once."""
        with self._db(write=True) as db:
            rows = db.execute("SELECT id, text FROM notices WHERE delivered_at IS NULL ORDER BY id").fetchall()
            if rows:
                db.execute("UPDATE notices SET delivered_at=? WHERE delivered_at IS NULL", (stamp(),))
            return [row["text"] for row in rows]


# ---------------------------------------------------------------- workflow records

MIN_REPORT = 40
MAX_REPORT = 40_000
MAX_ANSWER = 4_000
MAX_VERIFY = 20_000
MAX_FILES = 30
MAX_FILE_CHARS = 100_000
MAX_FILES_CHARS = 2_000_000
FINISHED = ("solved", "proposed_solved", "proposed_exhausted")
STAGES = ("working", "review", "verification", "deliverable", "discarded")


@dataclass
class Flow:
    board_id: str
    stage: str
    round: int
    entered_at: str
    report: Optional[str] = None
    report_round: int = 0
    report_at: Optional[str] = None
    verifier_state: Optional[str] = None  # wanted | running | done | failed
    verifier_token: Optional[str] = None
    verifier_verdict: Optional[str] = None
    verifier_report: Optional[str] = None
    verifier_note: Optional[str] = None
    verifier_started_at: Optional[str] = None
    verifier_told: int = 0
    own_verdict: Optional[str] = None
    own_report: Optional[str] = None
    verdict: Optional[str] = None
    verification: Optional[str] = None
    verified_at: Optional[str] = None
    accepted_at: Optional[str] = None
    session_key: Optional[str] = None


@dataclass
class ReviewNote:
    id: str
    board_id: str
    round: int
    text: str
    created_at: str
    answer: Optional[str]
    answered_at: Optional[str]


@dataclass
class DeliveredFile:
    path: str
    chars: int
    truncated: bool
    error: Optional[str]


@dataclass
class Workflow:
    board: Board
    flow: Flow
    reports: list
    notes: list
    files: list
