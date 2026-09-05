"""Lab files, device binding, and CDP cabling verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciscoyoke.stream.tracker import Confidence
from ciscoyoke.topology.apply import ApplyRefusedError, apply, make_plan
from ciscoyoke.topology.labfile import (
    AmbiguousMatchError,
    LabFileError,
    parse,
    resolve,
)
from ciscoyoke.topology.verify import (
    Neighbour,
    Outcome,
    normalise_interface,
    parse_cdp,
    undeclared_links,
    verify,
)
from ciscoyoke.transport.identity import PortIdentity

LAB = json.dumps(
    {
        "name": "ccna-ospf-single-area",
        "description": "Two routers, one switch",
        "devices": {
            "r1": {"match": {"serial": "FTX0812A1BC"}, "config": "configs/r1.cfg"},
            "r2": {"match": {"model": "CISCO1760"}},
            "sw1": {"match": {"label": "bench-left"}},
        },
        "cabling": [
            ["r1:FastEthernet0/0", "sw1:FastEthernet0/1"],
            ["r2:FastEthernet0/0", "sw1:FastEthernet0/2"],
        ],
    }
)


def test_a_lab_file_parses() -> None:
    lab = parse(LAB)
    assert lab.name == "ccna-ospf-single-area"
    assert len(lab.devices) == 3
    assert len(lab.cabling) == 2
    assert str(lab.cabling[0].a) == "r1:FastEthernet0/0"


def test_cabling_to_an_undeclared_device_is_refused() -> None:
    body = json.loads(LAB)
    body["cabling"].append(["ghost:Fa0/1", "sw1:Fa0/9"])
    with pytest.raises(LabFileError, match="undeclared device"):
        parse(json.dumps(body))


def test_a_device_with_no_match_criteria_is_refused() -> None:
    body = json.loads(LAB)
    body["devices"]["r3"] = {"match": {}}
    with pytest.raises(LabFileError, match="cannot be found"):
        parse(json.dumps(body))


def test_a_malformed_endpoint_is_refused() -> None:
    body = json.loads(LAB)
    body["cabling"] = [["r1", "sw1:Fa0/1"]]
    with pytest.raises(LabFileError, match="not a valid endpoint"):
        parse(json.dumps(body))


# -- binding ---------------------------------------------------------------

STRONG = PortIdentity(device="COM3", vid=0x0403, pid=0x6001, serial_number="A1")
WEAK = PortIdentity(device="COM7")


def test_a_serial_match_is_high_confidence() -> None:
    lab = parse(LAB)
    spec = lab.device("r1")
    assert spec is not None

    resolution = resolve(spec, {STRONG: {"serial": "FTX0812A1BC", "model": "CISCO1760"}})

    assert resolution.confidence is Confidence.HIGH
    assert resolution.safe_to_write is True
    assert "serial" in resolution.basis


def test_a_port_match_is_low_confidence_and_not_written_to() -> None:
    """A port name identifies whatever enumerated there this boot.

    Writing to that is the wrong-device hazard exactly.
    """
    lab = parse(json.dumps({
        "name": "t",
        "devices": {"sw1": {"match": {"port": "COM7"}}},
    }))
    spec = lab.device("sw1")
    assert spec is not None

    resolution = resolve(spec, {WEAK: {}})

    assert resolution.confidence is Confidence.LOW
    assert resolution.safe_to_write is False


def test_a_strong_selector_on_an_unrecognisable_port_is_downgraded() -> None:
    """A binding is only as strong as its weakest half.

    Matching a serial number is worthless if the endpoint behind it cannot be
    recognised again after a replug.
    """
    lab = parse(LAB)
    spec = lab.device("r1")
    assert spec is not None

    resolution = resolve(spec, {WEAK: {"serial": "FTX0812A1BC"}})
    assert resolution.confidence is Confidence.LOW


def test_an_ambiguous_match_is_an_error_not_a_coin_flip() -> None:
    lab = parse(LAB)
    spec = lab.device("r2")
    assert spec is not None

    second = PortIdentity(device="COM4", vid=0x0403, pid=0x6001, serial_number="A2")
    with pytest.raises(AmbiguousMatchError, match="more than"):
        resolve(spec, {STRONG: {"model": "CISCO1760"}, second: {"model": "CISCO1760"}})


def test_a_device_not_on_the_bench_is_reported() -> None:
    lab = parse(LAB)
    spec = lab.device("r1")
    assert spec is not None

    with pytest.raises(LabFileError, match="not found on"):
        resolve(spec, {STRONG: {"serial": "SOMETHING-ELSE"}})


# -- apply -----------------------------------------------------------------


def test_apply_refuses_an_incomplete_plan() -> None:
    """All-or-nothing at plan time.

    A half-applied topology across four boxes leaves nobody able to say which
    half.
    """
    lab = parse(LAB)
    plan = make_plan(lab, {STRONG: {"serial": "FTX0812A1BC"}})

    assert plan.complete is False
    with pytest.raises(ApplyRefusedError):
        apply(plan, lambda _r, _c: 0, confirm=True)


def test_apply_refuses_without_confirmation() -> None:
    lab = parse(json.dumps({
        "name": "t",
        "devices": {"r1": {"match": {"serial": "A1"}}},
    }))
    plan = make_plan(lab, {STRONG: {"serial": "A1"}})

    assert plan.complete is True
    with pytest.raises(ApplyRefusedError, match="Re-run with confirmation"):
        apply(plan, lambda _r, _c: 0)


def test_apply_refuses_weak_bindings_by_default() -> None:
    lab = parse(json.dumps({
        "name": "t",
        "devices": {"sw1": {"match": {"port": "COM7"}}},
    }))
    plan = make_plan(lab, {WEAK: {}})

    assert plan.complete is True
    assert plan.safe is False
    with pytest.raises(ApplyRefusedError, match="weakly-matched"):
        apply(plan, lambda _r, _c: 0, confirm=True)


def test_all_unresolved_devices_are_reported_at_once() -> None:
    """Fixing them one rerun at a time is how people give up and do it by hand."""
    lab = parse(LAB)
    plan = make_plan(lab, {})

    assert len(plan.problems) == 3


# -- CDP verification ------------------------------------------------------

CDP_OUTPUT = """
-------------------------
Device ID: sw1.lab.local
Entry address(es):
  IP address: 10.0.0.2
Platform: cisco WS-C2950-24,  Capabilities: Switch
Interface: FastEthernet0/0,  Port ID (outgoing port): FastEthernet0/1
Holdtime : 143 sec
"""


def test_cdp_output_is_parsed() -> None:
    neighbours = parse_cdp(CDP_OUTPUT, "r1")
    assert len(neighbours) == 1
    assert neighbours[0].remote_device == "sw1.lab.local"
    assert neighbours[0].remote_interface == "FastEthernet0/1"


def test_interface_abbreviations_are_normalised() -> None:
    """Two Cisco commands disagree about spelling; a correct cable must not
    read as a mismatch because of it."""
    assert normalise_interface("FastEthernet0/1") == normalise_interface("Fa0/1")
    assert normalise_interface("GigabitEthernet1/0/1") == normalise_interface("Gi1/0/1")


def test_a_correct_cable_verifies() -> None:
    lab = parse(LAB)
    discovered = {"r1": parse_cdp(CDP_OUTPUT, "r1"), "r2": (), "sw1": ()}

    report = verify(lab, discovered)
    first = report.findings[0]

    assert first.outcome is Outcome.CORRECT
    assert "✓" in first.render()


def test_a_misconnected_cable_names_what_was_found() -> None:
    """'Wrong' is not actionable; 'found Fa0/3, expected Fa0/2' is."""
    lab = parse(LAB)
    wrong = CDP_OUTPUT.replace("FastEthernet0/1", "FastEthernet0/3")
    discovered = {"r1": parse_cdp(wrong, "r1"), "r2": (), "sw1": ()}

    finding = verify(lab, discovered).findings[0]

    assert finding.outcome is Outcome.MISCONNECTED
    assert "FastEthernet0/3" in finding.detail
    assert "expected sw1:FastEthernet0/1" in finding.detail


def test_a_missing_cable_is_distinguished_from_a_wrong_one() -> None:
    lab = parse(LAB)
    discovered: dict[str, tuple[Neighbour, ...]] = {"r1": (), "r2": (), "sw1": ()}

    finding = verify(lab, discovered).findings[0]

    assert finding.outcome is Outcome.MISSING
    assert "no neighbour" in finding.detail


def test_a_self_loop_is_called_out() -> None:
    lab = parse(LAB)
    looped = Neighbour("r1", "FastEthernet0/0", "r1", "FastEthernet0/1")

    finding = verify(lab, {"r1": (looped,), "r2": (), "sw1": ()}).findings[0]

    assert finding.outcome is Outcome.SELF_LOOPED
    assert "loop" in finding.detail


def test_an_unreachable_device_is_not_blamed_on_a_cable() -> None:
    """A device that could not be reached is a different problem."""
    lab = parse(LAB)

    finding = verify(lab, {}).findings[0]

    assert finding.outcome is Outcome.UNDISCOVERABLE
    assert "not reachable" in finding.detail


def test_the_report_summarises() -> None:
    lab = parse(LAB)
    discovered = {"r1": parse_cdp(CDP_OUTPUT, "r1"), "r2": (), "sw1": ()}

    rendered = verify(lab, discovered).render()
    assert "do not match" in rendered


def test_undeclared_links_are_surfaced_without_being_errors() -> None:
    """A management cable is fine; an unexpected one is how a lab quietly stops
    testing what it claims to."""
    lab = parse(LAB)
    extra = Neighbour("sw1", "FastEthernet0/24", "sw2", "FastEthernet0/24")

    assert undeclared_links(lab, {"sw1": (extra,)}) == (extra,)


def test_a_declared_link_is_not_reported_as_undeclared() -> None:
    lab = parse(LAB)
    declared = Neighbour("r1", "FastEthernet0/0", "sw1", "FastEthernet0/1")

    assert undeclared_links(lab, {"r1": (declared,)}) == ()


def test_lab_files_load_from_disk(tmp_path: Path) -> None:
    from ciscoyoke.topology.labfile import load

    path = tmp_path / "lab.json"
    path.write_text(LAB, encoding="utf-8")

    assert load(path).source == path
