from __future__ import annotations

import json
import re
from pathlib import Path


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one literal match, found {count}")
    write(path, text.replace(old, new, 1))


def sub_once(path: str, pattern: str, replacement: str) -> None:
    text = read(path)
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one regex match, found {count}")
    write(path, updated)


# 1) FotMob structured ingestion: explicit Contract extension rows are a
# non-directional CONTRACT event, never a Tottenham->Tottenham transfer.
ingestion_block = r'''def _fotmob_row_kind(row: Dict[str, Any]) -> str:
    """Return the explicit FotMob transaction kind without guessing from clubs."""
    fee = row.get("fee") or {}
    fee_label = str(fee.get("feeText") or "").strip().lower()
    if "contract extension" in fee_label:
        return "contract_extension"
    if bool(row.get("onLoan")) or "loan" in fee_label:
        return "loan"
    if "free" in fee_label:
        return "free"
    return "permanent"


def _fotmob_transfer_text(row: Dict[str, Any]) -> str:
    name = str(row.get("name") or "").strip()
    from_club = str(row.get("fromClubFullName") or row.get("fromClub") or "").strip()
    to_club = str(row.get("toClubFullName") or row.get("toClub") or "").strip()
    kind = _fotmob_row_kind(row)
    fee = row.get("fee") or {}
    fee_value = _fotmob_euro(fee.get("value"))
    contract_until = _fotmob_contract_until(row.get("toDate"))
    market_value = _fotmob_euro(row.get("marketValue"))
    position = str((row.get("position") or {}).get("label") or "").strip()

    if kind == "contract_extension":
        club = to_club or from_club
        lead = f"{name} extends the contract with {club}."
        bits = [
            lead,
            "FotMob listed the contract extension as completed.",
            "Deal type: contract extension.",
        ]
    elif kind == "loan":
        bits = [
            f"{name} has joined {to_club} from {from_club} on loan.",
            "FotMob listed the transfer as completed.",
            "Deal type: loan.",
        ]
    elif kind == "free":
        bits = [
            f"{name} has joined {to_club} from {from_club} on a free transfer.",
            "FotMob listed the transfer as completed.",
            "Deal type: free transfer.",
        ]
    else:
        bits = [
            f"{name} has joined {to_club} from {from_club}.",
            "FotMob listed the transfer as completed.",
            "Deal type: permanent transfer.",
        ]

    if fee_value and kind != "contract_extension":
        bits.append(f"Fee: {fee_value}.")
    if contract_until:
        bits.append(f"Contract until {contract_until}.")
    if market_value:
        bits.append(f"Market value: {market_value}.")
    if position:
        bits.append(f"Position: {position}.")
    return " ".join(bits)


def _fotmob_legacy_story(row: Dict[str, Any]) -> Dict[str, Any]:
    kind = _fotmob_row_kind(row)
    fee = row.get("fee") or {}
    fee_text = _fotmob_euro(fee.get("value"))
    is_extension = kind == "contract_extension"
    player_name = str(row.get("name") or "").strip()
    raw_from = str(row.get("fromClubFullName") or row.get("fromClub") or "").strip()
    raw_to = str(row.get("toClubFullName") or row.get("toClub") or "").strip()
    club = raw_to or raw_from
    text = _fotmob_transfer_text(row)

    if is_extension:
        event = "renewal"
        from_club = None
        to_club = club
        transfer_kind = None
    else:
        event = "loan" if kind == "loan" else "transfer"
        from_club = raw_from
        to_club = raw_to
        transfer_kind = kind

    return {
        "player": player_name,
        "event": event,
        "from_club": from_club,
        "to_club": to_club,
        "_structured_fotmob_transfer": not is_extension,
        "_structured_fotmob_contract_extension": is_extension,
        "_structured_transfer_group": (
            f"{player_name}|{raw_from}|{raw_to}" if not is_extension else None
        ),
        "fee": (fee_text or None) if not is_extension else None,
        "contract": _fotmob_contract_until(row.get("toDate")) or None,
        "market_value": _fotmob_euro(row.get("marketValue")) or None,
        "position": str((row.get("position") or {}).get("label") or "").strip() or None,
        "event_time": row.get("transferDate") or row.get("fromDate"),
        "transfer_kind": transfer_kind,
        "stage": 4,
        "collapsed": False,
        "historical": False,
        "headline": text,
        "raw_text": text,
        "sources": ["fotmob"],
    }
'''
sub_once(
    "src/verification/ingestion.py",
    r"def _fotmob_transfer_text\(row: Dict\[str, Any\]\) -> str:\n.*?\n\ndef _fetch_fotmob_transfers\(",
    ingestion_block + "\n\ndef _fetch_fotmob_transfers(",
)
replace_once(
    "src/verification/ingestion.py",
    '''            text = _fotmob_transfer_text(row)\n            created = row.get("transferDate") or row.get("fromDate")\n            items.append({''',
    '''            legacy_story = _fotmob_legacy_story(row)\n            text = _fotmob_transfer_text(row)\n            created = row.get("transferDate") or row.get("fromDate")\n            items.append({''',
)
replace_once(
    "src/verification/ingestion.py",
    '''                "metadata": {"structured_fotmob_transfer": True, "fotmob_row": row},\n                "_legacy_story": _fotmob_legacy_story(row),''',
    '''                "metadata": {\n                    "structured_fotmob_transfer": bool(\n                        legacy_story.get("_structured_fotmob_transfer")\n                    ),\n                    "structured_fotmob_contract_extension": bool(\n                        legacy_story.get("_structured_fotmob_contract_extension")\n                    ),\n                    "fotmob_row": row,\n                },\n                "_legacy_story": legacy_story,''',
)

# 2) Extractor: both structured FotMob routes may establish provider identities,
# but transfer-only relationship facts remain transfer-only. Contract facts get
# their own explicit structured source marker and provider IDs.
replace_once(
    "src/verification/extractor.py",
    '''        structured_fotmob = bool(\n            document.metadata.get("structured_fotmob_transfer") is True\n            and document.source.profile_id == "media.fotmob"\n        )''',
    '''        structured_fotmob_transfer = bool(\n            document.metadata.get("structured_fotmob_transfer") is True\n            and document.source.profile_id == "media.fotmob"\n        )\n        structured_fotmob_contract = bool(\n            document.metadata.get("structured_fotmob_contract_extension") is True\n            and document.source.profile_id == "media.fotmob"\n        )\n        structured_fotmob = structured_fotmob_transfer or structured_fotmob_contract''',
)
replace_once(
    "src/verification/extractor.py",
    '''            if structured_fotmob:\n                provider_player_name = str(fotmob_row.get("name") or "").strip()''',
    '''            if structured_fotmob_transfer:\n                provider_player_name = str(fotmob_row.get("name") or "").strip()''',
)
replace_once(
    "src/verification/extractor.py",
    '''        elif event == EventType.CONTRACT:\n            if classification.status in {EventStatus.OFFICIAL, EventStatus.COMPLETED}:\n                evidence = classification.status_evidence or document.title\n                add_fact("contract_status", "extended", EvidenceSupport.TEXT_SPAN, evidence)\n            self._add_optional_grounded(\n                "contract_length", legacy_story.get("contract"), document, add_fact\n            )''',
    '''        elif event == EventType.CONTRACT:\n            if classification.status in {EventStatus.OFFICIAL, EventStatus.COMPLETED}:\n                evidence = classification.status_evidence or document.title\n                add_fact("contract_status", "extended", EvidenceSupport.TEXT_SPAN, evidence)\n            self._add_optional_grounded(\n                "contract_length", legacy_story.get("contract"), document, add_fact\n            )\n            if structured_fotmob_contract:\n                provider_player_name = str(fotmob_row.get("name") or "").strip()\n                add_fact(\n                    "structured_source", "fotmob_transfer_table",\n                    EvidenceSupport.STRUCTURED_DATA,\n                    "structured_fotmob_contract_extension=true",\n                )\n                add_fact(\n                    "provider_player_name", provider_player_name,\n                    EvidenceSupport.STRUCTURED_DATA, provider_player_name,\n                )\n                add_fact(\n                    "provider_player_id", str(fotmob_row.get("playerId") or ""),\n                    EvidenceSupport.STRUCTURED_DATA, str(fotmob_row.get("playerId") or ""),\n                )\n                add_fact(\n                    "provider_to_club_id", str(fotmob_row.get("toClubId") or ""),\n                    EvidenceSupport.STRUCTURED_DATA, str(fotmob_row.get("toClubId") or ""),\n                )\n                for key in ("market_value", "position"):\n                    value = legacy_story.get(key)\n                    if value:\n                        add_fact(key, value, EvidenceSupport.STRUCTURED_DATA, str(value))''',
)

# 3) V2 policy: CONTRACT becomes publishable but remains fail-closed and requires
# an exact contract term. Structured FotMob extension authority is a separate,
# narrow authority kind with the same <=48h structural freshness cap.
engine = read("src/verification/engine.py")
anchor = '''from .repository import VerificationRepository\nfrom .source_registry import SourceRegistry\n\n\nclass VerificationEngine:'''
if anchor not in engine:
    raise RuntimeError("engine constant anchor missing")
engine = engine.replace(
    anchor,
    '''from .repository import VerificationRepository\nfrom .source_registry import SourceRegistry\n\n\nFOTMOB_CONTRACT_AUTHORITY_KIND = "structured_fotmob_contract_extension"\n\n\nclass VerificationEngine:''',
    1,
)
engine = engine.replace(
    '''            claim.source_id == FOTMOB_SOURCE_ID\n            and claim.document.metadata.get("structured_fotmob_transfer") is True\n            for claim in event_claims''',
    '''            claim.source_id == FOTMOB_SOURCE_ID\n            and (\n                claim.document.metadata.get("structured_fotmob_transfer") is True\n                or claim.document.metadata.get("structured_fotmob_contract_extension") is True\n            )\n            for claim in event_claims''',
    1,
)
insert_after = '''        trusted_fotmob = select_trusted_fotmob_claim(claims, event)\n        if trusted_fotmob is not None:\n            return [trusted_fotmob], FOTMOB_NEWS_AUTHORITY_KIND\n\n'''
if insert_after not in engine:
    raise RuntimeError("engine FotMob lane anchor missing")
contract_lane = '''        if (\n            event == EventType.CONTRACT\n            and self.config.policy("allow_structured_fotmob_contract_extensions")\n        ):\n            structured_contracts = []\n            for claim in claims:\n                if not (\n                    claim.source_id == FOTMOB_SOURCE_ID\n                    and claim.document.source.verified\n                    and claim.article_category == event\n                    and claim.league_relevant\n                    and claim.status == EventStatus.COMPLETED\n                    and claim.document.metadata.get("structured_fotmob_contract_extension") is True\n                    and claim.facts.get("structured_source") == "fotmob_transfer_table"\n                    and claim.facts.get("contract_status") == "extended"\n                    and claim.facts.get("contract_length")\n                    and str(claim.facts.get("provider_player_id") or "").isdigit()\n                    and str(claim.facts.get("provider_to_club_id") or "").isdigit()\n                ):\n                    continue\n                club = self.entities.get(str(claim.facts.get("club_id") or ""))\n                if not club or not club.active_premier_league:\n                    continue\n                structured_contracts.append(claim)\n            if structured_contracts:\n                return [structured_contracts[0]], FOTMOB_CONTRACT_AUTHORITY_KIND\n\n'''
engine = engine.replace(insert_after, insert_after + contract_lane, 1)
engine = engine.replace(
    '''            FOTMOB_AUTHORITY_KIND: "structured FotMob completed-transfer listing",\n            FOTMOB_NEWS_AUTHORITY_KIND: "trusted FotMob Premier League report",''',
    '''            FOTMOB_AUTHORITY_KIND: "structured FotMob completed-transfer listing",\n            FOTMOB_CONTRACT_AUTHORITY_KIND: "structured FotMob contract-extension listing",\n            FOTMOB_NEWS_AUTHORITY_KIND: "trusted FotMob Premier League report",''',
    1,
)
engine = engine.replace(
    '''        if confirmation_kind == FOTMOB_AUTHORITY_KIND:\n            # Historical free-text outcome learning must not disable the\n            # separately approved structured table lane. Use the configured\n            # structural prior for this exact source/mode only.''',
    '''        if confirmation_kind in {FOTMOB_AUTHORITY_KIND, FOTMOB_CONTRACT_AUTHORITY_KIND}:\n            # Historical free-text outcome learning must not disable the\n            # separately approved structured table lanes. Use the configured\n            # structural prior for these exact source/modes only.''',
    1,
)
engine = engine.replace(
    '''            self.config.threshold("max_fotmob_transfer_age_hours")\n            if confirmation_kind == FOTMOB_AUTHORITY_KIND''',
    '''            self.config.threshold("max_fotmob_transfer_age_hours")\n            if confirmation_kind in {FOTMOB_AUTHORITY_KIND, FOTMOB_CONTRACT_AUTHORITY_KIND}''',
    1,
)
engine = engine.replace(
    '''            "FotMob listing"\n            if confirmation_kind == FOTMOB_AUTHORITY_KIND''',
    '''            "FotMob listing"\n            if confirmation_kind in {FOTMOB_AUTHORITY_KIND, FOTMOB_CONTRACT_AUTHORITY_KIND}''',
    1,
)
write("src/verification/engine.py", engine)

# 4) Configuration and startup validation.
config_path = Path("config/verification.json")
config = json.loads(config_path.read_text(encoding="utf-8"))
publishable = config["policy"]["publishable_article_categories"]
if "CONTRACT" not in publishable:
    publishable.append("CONTRACT")
config["policy"]["allow_structured_fotmob_contract_extensions"] = True
config["events"]["CONTRACT"] = {
    "required_facts": [
        "subject_id", "subject_name", "club_id", "club_name",
        "contract_status", "contract_length",
    ],
    "allowed_subject_types": ["PLAYER"],
    "conflict_fields": ["subject_id", "club_id", "contract_length"],
    "material_fields": ["contract_status", "contract_length"],
    "official_relation_fields": ["club_id", "competition_id"],
    "progressive_fields": ["contract_length"],
}
config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

replace_once(
    "src/verification/config.py",
    '''        if self.policy("allow_structured_fotmob_completed_transfers") is True:\n            fotmob_age = self.threshold("max_fotmob_transfer_age_hours")\n            if fotmob_age <= 0 or fotmob_age > 48:\n                raise ConfigurationError("structured FotMob transfer age must be within 48 hours")''',
    '''        if self.policy("allow_structured_fotmob_completed_transfers") is True:\n            fotmob_age = self.threshold("max_fotmob_transfer_age_hours")\n            if fotmob_age <= 0 or fotmob_age > 48:\n                raise ConfigurationError("structured FotMob transfer age must be within 48 hours")\n        if self.policy("allow_structured_fotmob_contract_extensions") is True:\n            fotmob_age = self.threshold("max_fotmob_transfer_age_hours")\n            if fotmob_age <= 0 or fotmob_age > 48:\n                raise ConfigurationError("structured FotMob contract age must be within 48 hours")''',
)

# 5) NEWS BOT owns contract extensions too. PRESS CONFERENCE BOT remains isolated.
replace_once(
    ".github/workflows/bot.yml",
    "LIVE_EVENT_SCOPE: transfer,loan,loan_option,injury,suspension",
    "LIVE_EVENT_SCOPE: transfer,loan,loan_option,injury,suspension,renewal",
)
replace_once(
    "tests/test_press_deadline_routing.py",
    '''        "transfer,loan,loan_option,injury,suspension"''',
    '''        "transfer,loan,loan_option,injury,suspension,renewal"''',
)
replace_once(
    "tests/test_press_deadline_routing.py",
    '''    assert "LIVE_EVENT_SCOPE: transfer,loan,loan_option,injury,suspension" in general''',
    '''    assert "LIVE_EVENT_SCOPE: transfer,loan,loan_option,injury,suspension,renewal" in general''',
)

# main.py's supported live categories were intentionally smaller than its parser.
# Add only renewal; manager/stay/official-statement remain excluded from NEWS BOT.
main_text = read("main.py")
match = re.search(
    r"ALL_LIVE_POST_EVENT_NAMES = frozenset\(\{(?P<body>.*?)\}\)",
    main_text,
    re.S,
)
if not match:
    raise RuntimeError("ALL_LIVE_POST_EVENT_NAMES block not found")
block = match.group(0)
if '"renewal"' not in block:
    revised = block[:-2].rstrip() + ',\n    "renewal",\n})'
    main_text = main_text[:match.start()] + revised + main_text[match.end():]
    write("main.py", main_text)

replace_once(
    "tests/test_autopost_unlimited.py",
    '''    assert main._live_event_allowed({"event": "renewal"}) is False''',
    '''    assert main._live_event_allowed({"event": "renewal"}) is True''',
)

# 6) Regression tests based on the structured shape shown in FotMob Transfer Center.
test_path = Path("tests/test_fotmob_contract_extensions.py")
test_path.write_text(r'''from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.verification.ingestion import _fotmob_legacy_story, _fotmob_transfer_text
from src.verification.models import DecisionType, EventType
from src.verification.runtime import VerificationRuntime


def _now_iso(hours_ago: float = 1.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _row(*, fee_text: str = "Contract extension", to_date: str | None = "2032-06-30T00:00:00Z"):
    return {
        "playerId": 10,
        "name": "Danny Welbeck",
        "fromClubId": 10204,
        "fromClub": "Brighton",
        "fromClubFullName": "Brighton",
        "toClubId": 10204,
        "toClub": "Brighton",
        "toClubFullName": "Brighton",
        "transferDate": _now_iso(),
        "fromDate": _now_iso(),
        "toDate": to_date,
        "fee": {"feeText": fee_text, "value": 0},
        "marketValue": 48000000,
        "position": {"label": "ST"},
        "onLoan": False,
    }


def _obs(row):
    story = _fotmob_legacy_story(row)
    text = _fotmob_transfer_text(row)
    return {
        "document": {
            "id": "fotmob-contract-test",
            "document_id": "fotmob-contract-test",
            "title": text,
            "summary": "",
            "text": text,
            "source_url": "https://www.fotmob.com/leagues/47/transfers/premier-league?season=2026%2F2027",
            "publisher_url": "https://www.fotmob.com/",
            "publisher_name": "FotMob",
            "source_id": "media.fotmob",
            "source_hint": "media.fotmob",
            "source_handle": "fotmob",
            "transport": "FOTMOB",
            "configured_direct_feed": False,
            "declared_sport": "football",
            "created_at": row["transferDate"],
            "published_at": row["transferDate"],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "feed_id": "fotmob.premier_league.transfers",
            "metadata": {
                "structured_fotmob_transfer": bool(story.get("_structured_fotmob_transfer")),
                "structured_fotmob_contract_extension": bool(
                    story.get("_structured_fotmob_contract_extension")
                ),
                "fotmob_row": row,
            },
        },
        "legacy_story": story,
    }


@pytest.fixture
def runtime(tmp_path):
    fpl = {
        "teams": [{"id": 1, "name": "Brighton", "short_name": "BHA"}],
        "elements": [{
            "id": 10,
            "first_name": "Danny",
            "second_name": "Welbeck",
            "web_name": "Welbeck",
            "team": 1,
        }],
    }
    rt = VerificationRuntime.create(db_path=tmp_path / "verification.sqlite3", fpl_data=fpl)
    yield rt
    rt.close()


def test_explicit_fotmob_contract_extension_routes_to_contract_not_transfer(runtime):
    row = _row()
    story = _fotmob_legacy_story(row)
    assert story["event"] == "renewal"
    assert story["_structured_fotmob_contract_extension"] is True
    assert story["_structured_fotmob_transfer"] is False
    assert story["from_club"] is None
    assert story["to_club"] == "Brighton"
    assert story["contract"] == "Jun 2032"
    assert "extends the contract with Brighton" in story["raw_text"]

    decision = runtime.verify_observations([_obs(row)])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.event_type == EventType.CONTRACT
    assert decision.authority_kind == "structured_fotmob_contract_extension"
    assert decision.authority_source_ids == ["media.fotmob"]
    assert decision.verified_facts["club_name"] == "Brighton"
    assert decision.verified_facts["contract_status"] == "extended"
    assert decision.verified_facts["contract_length"] == "Jun 2032"
    assert decision.verified_facts["provider_player_id"] == "10"
    assert decision.verified_facts["provider_to_club_id"] == "10204"
    assert "CONTRACT UPDATE" in decision.rendered_text


def test_same_club_row_is_not_guessed_as_extension_without_explicit_label(runtime):
    row = _row(fee_text="Permanent transfer")
    story = _fotmob_legacy_story(row)
    assert story["event"] == "transfer"
    assert story["_structured_fotmob_contract_extension"] is False
    assert story["_structured_fotmob_transfer"] is True

    decision = runtime.verify_observations([_obs(row)])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish
    assert decision.event_type == EventType.TRANSFER


def test_structured_fotmob_extension_without_contract_end_date_fails_closed(runtime):
    row = _row(to_date=None)
    decision = runtime.verify_observations([_obs(row)])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish
    mandatory = decision.gate("mandatory_facts")
    assert not mandatory.passed
    assert "contract_length" in mandatory.reason


def test_unstructured_fotmob_contract_story_cannot_use_structured_authority(runtime):
    row = _row()
    obs = _obs(row)
    obs["document"]["metadata"] = {}
    obs["legacy_story"]["_structured_fotmob_contract_extension"] = False
    obs["legacy_story"]["_structured_fotmob_transfer"] = False
    decision = runtime.verify_observations([obs])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish
    assert decision.authority_kind != "structured_fotmob_contract_extension"


def test_stale_structured_fotmob_contract_extension_does_not_backfill(runtime):
    row = _row()
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    row["transferDate"] = old
    row["fromDate"] = old
    decision = runtime.verify_observations([_obs(row)])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish
    temporal = decision.gate("temporal_consistency")
    assert not temporal.passed
''', encoding="utf-8")

print("FotMob contract-extension patch applied successfully")
