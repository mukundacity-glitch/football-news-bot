from pathlib import Path
import re


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"missing expected block: {label}")
    return text.replace(old, new, 1)


renderer_path = Path("src/verification/renderer.py")
renderer = renderer_path.read_text(encoding="utf-8")
renderer = renderer.replace("from .reported_transfer_gate import is_reported_transfer\n", "")

new_transfer = '''    def _render_transfer_template(self, decision: VerificationDecision) -> str:
        facts = decision.verified_facts
        player = str(required(facts, "subject_name"))
        origin = str(required(facts, "club_from_name"))
        destination = str(required(facts, "club_to_name"))

        if decision.status in {EventStatus.OFFICIAL, EventStatus.COMPLETED}:
            status = "COMPLETED"
        elif decision.status in {
            EventStatus.UNKNOWN,
            EventStatus.RUMOUR,
            EventStatus.INTEREST,
        }:
            status = "RUMOUR"
        else:
            status = "IN PROGRESS"

        raw_kind = str(facts.get("transfer_kind") or "").strip().casefold()
        deal_type = "Loan" if "loan" in raw_kind else "Permanent"
        deal = f"Deal — {deal_type}"
        contract = facts.get("contract_length") or facts.get("contract_date")
        if contract:
            deal += f" | Contract {self._cap(contract, 42)}"

        if status == "COMPLETED":
            take = "Check the FPL impact before making your next transfer."
        elif status == "IN PROGRESS":
            take = "Monitor for confirmation before making an FPL move."
        else:
            take = "Wait for stronger confirmation before making an FPL move."

        description = [
            f"🚨 REPORTED TRANSFER — {self._cap(player, 42)}",
            f"{self._cap(origin, 38)} → {self._cap(destination, 38)}",
            self._cap(deal, 92),
            f"Status: {status}",
            take,
        ]
        return self._finish_transfer_template(
            decision,
            description,
            self._hashtags("#TransferNews", destination or origin),
        )

'''
pattern = re.compile(
    r"    def _render_transfer_template\(self, decision: VerificationDecision\) -> str:\n"
    r".*?(?=    def _render_suspension_template)",
    re.DOTALL,
)
renderer, count = pattern.subn(new_transfer, renderer, count=1)
if count != 1:
    raise SystemExit(f"transfer renderer replacement count={count}")

helper = '''    def _finish_transfer_template(
        self,
        decision: VerificationDecision,
        description: List[str],
        hashtag_line: str,
    ) -> str:
        """Render the locked seven-line transfer caption without touching graphics."""
        body = [" ".join(str(line or "").split()) for line in description]
        tags = " ".join(str(hashtag_line or "").split())
        if len(body) != 5 or not all(body):
            raise RenderingError("transfer caption requires exactly five information lines")
        if len(tags.split()) != 3 or not all(
            tag.startswith("#") for tag in tags.split()
        ):
            raise RenderingError("transfer caption requires exactly three hashtags")

        def rendered() -> str:
            return "\\n".join(
                [body[0], body[1], body[2], body[3], "", body[4], tags]
            )

        minima = {0: 32, 1: 24, 2: 18, 4: 28}
        while twitter_weight(rendered()) > self.limit:
            candidates = [
                (len(body[index]) - minimum, index, minimum)
                for index, minimum in minima.items()
                if len(body[index]) > minimum
            ]
            if not candidates:
                raise RenderingError("fixed transfer caption does not fit X limit")
            _room, index, minimum = max(candidates)
            body[index] = self._cap(
                body[index], max(minimum, len(body[index]) - 8)
            )

        result = rendered()
        lines = result.splitlines()
        if len(lines) != 7 or lines[4] != "":
            raise RenderingError("fixed transfer caption structure changed during fitting")
        decision.rendered_text = result
        return result

'''
marker = "    def _finish_fixed_template(\n"
if helper not in renderer:
    if marker not in renderer:
        raise SystemExit("fixed-template helper marker not found")
    renderer = renderer.replace(marker, helper + marker, 1)
renderer_path.write_text(renderer, encoding="utf-8")


templates_path = Path("tests/test_caption_master_templates.py")
templates = templates_path.read_text(encoding="utf-8")
old_shape = '''    lines = text.splitlines()
    assert len(lines) == 6
    assert lines[3] == ""
    assert not any(line.startswith("#") for line in lines[:5])
    assert len(lines[5].split()) == 3
    assert all(tag.startswith("#") for tag in lines[5].split())
'''
new_shape = '''    lines = text.splitlines()
    if value.event_type == EventType.TRANSFER:
        assert len(lines) == 7
        assert lines[4] == ""
        assert not any(line.startswith("#") for line in lines[:6])
        hashtag_line = lines[6]
    else:
        assert len(lines) == 6
        assert lines[3] == ""
        assert not any(line.startswith("#") for line in lines[:5])
        hashtag_line = lines[5]
    assert len(hashtag_line.split()) == 3
    assert all(tag.startswith("#") for tag in hashtag_line.split())
'''
templates = replace_once(templates, old_shape, new_shape, "template shape")
templates = replace_once(
    templates,
    '                "structured_source": "fotmob_transfer_table",\n',
    '                "structured_source": "fotmob_transfer_table",\n'
    '                "transfer_kind": "permanent",\n'
    '                "contract_length": "2030",\n',
    "reported transfer facts",
)
old_expected = '''        "🚨 REPORTED TRANSFER — Gerónimo Rulli\\n"
        "Marseille → Man City\\n"
        "Status: COMPLETED\\n"
        "\\n"
        "Monitor for confirmation before making an FPL move.\\n"
        "#FPL #TransferNews #ManCity"
'''
new_expected = '''        "🚨 REPORTED TRANSFER — Gerónimo Rulli\\n"
        "Marseille → Man City\\n"
        "Deal — Permanent | Contract 2030\\n"
        "Status: COMPLETED\\n"
        "\\n"
        "Check the FPL impact before making your next transfer.\\n"
        "#FPL #TransferNews #ManCity"
'''
templates = replace_once(templates, old_expected, new_expected, "reported transfer expected text")
templates = replace_once(
    templates,
    "def test_official_transfer_uses_official_status_and_three_hashtags():",
    "def test_official_transfer_uses_completed_status_and_three_hashtags():",
    "official transfer test name",
)
templates = replace_once(
    templates,
    '    assert "Status: OFFICIAL" in text\n',
    '    assert text.splitlines()[0] == "🚨 REPORTED TRANSFER — Dynamic Player"\n'
    '    assert "Status: COMPLETED" in text\n'
    '    assert "Deal — Permanent" in text\n',
    "official transfer status assertions",
)
insertion = '''

def test_transfer_in_progress_status_is_locked():
    text = render(
        decision(
            EventType.TRANSFER,
            {
                "subject_name": "Dynamic Player",
                "club_from_name": "Arsenal",
                "club_to_name": "Chelsea",
                "transfer_kind": "loan",
            },
            status=EventStatus.AGREEMENT,
            source_ids=["media.bbc_sport", "media.sky_sports"],
            authority_kind="tier_one_reported_transfer",
        )
    )
    assert text.splitlines()[2] == "Deal — Loan"
    assert text.splitlines()[3] == "Status: IN PROGRESS"


def test_transfer_rumour_status_is_locked():
    text = render(
        decision(
            EventType.TRANSFER,
            {
                "subject_name": "Dynamic Player",
                "club_from_name": "Arsenal",
                "club_to_name": "Chelsea",
            },
            status=EventStatus.RUMOUR,
            source_ids=["media.bbc_sport"],
            authority_kind="tier_one_reported_transfer",
        )
    )
    assert text.splitlines()[3] == "Status: RUMOUR"
'''
suspension_marker = "\n\ndef test_suspension_template_exact():\n"
if insertion not in templates:
    templates = replace_once(
        templates,
        suspension_marker,
        insertion + suspension_marker,
        "transfer status test insertion",
    )
templates_path.write_text(templates, encoding="utf-8")


integration_path = Path("tests/test_verification_v2.py")
integration = integration_path.read_text(encoding="utf-8")
for old, new, label in [
    (
        'assert "✅ TRANSFER UPDATE — Danny Welbeck" in decision.rendered_text',
        'assert "🚨 REPORTED TRANSFER — Danny Welbeck" in decision.rendered_text',
        "official transfer headline",
    ),
    (
        'assert "Status: OFFICIAL" in decision.rendered_text',
        'assert "Status: COMPLETED" in decision.rendered_text',
        "official transfer completed status",
    ),
    (
        'assert len(decision.rendered_text.splitlines()) == 6',
        'assert len(decision.rendered_text.splitlines()) == 7',
        "official transfer line count",
    ),
    (
        'assert "Status: REPORTED" in two.rendered_text',
        'assert "Status: IN PROGRESS" in two.rendered_text',
        "reported agreement status",
    ),
    (
        'assert "Arsenal → Barcelona | Permanent | Fee €10m" in decision.rendered_text',
        'assert "Deal — Permanent" in decision.rendered_text\n'
        '        assert "Fee €10m" not in decision.rendered_text',
        "fotmob deal line",
    ),
]:
    integration = replace_once(integration, old, new, label)
integration_path.write_text(integration, encoding="utf-8")
