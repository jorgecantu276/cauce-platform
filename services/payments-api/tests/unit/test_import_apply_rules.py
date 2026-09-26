"""Pure rules behind the ETL apply: deterministic identity, customer/charge
compatibility, per-row decision and stored-row revalidation. No I/O."""

import copy
from decimal import Decimal
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments import imports


CID = imports.customer_id_for("b1", "CLI-1")


def row(source_row=2, ext="CLI-1", name="Ferretería del Norte", email="cobros@example.com",
        charge="FAC-1", amount=1250000, description="Material", due="2026-09-30"):
    return {
        "sourceRow": source_row,
        "customer": {"externalId": ext, "displayName": name, "email": email},
        "charge": {"externalId": charge, "amountMinor": amount, "currency": "MXN", "description": description, "dueDate": due},
    }


def stored_customer(business="b1", ext="CLI-1", name="Ferretería del Norte", email="cobros@example.com"):
    return {"id": imports.customer_id_for(business, ext), "display_name": name, "email": email}


def stored_charge(business="b1", customer_ext="CLI-1", charge_ext="FAC-1", amount=1250000, description="Material",
                  due="2026-09-30", cancelled_at=None, currency="MXN"):
    return {
        "id": imports.charge_id_for(business, charge_ext), "customer_id": imports.customer_id_for(business, customer_ext),
        "amount_minor": Decimal(amount), "currency": currency, "description": description, "due_date": due, "cancelled_at": cancelled_at,
    }


# --- deterministic identity ---------------------------------------------------

def test_ids_are_deterministic_and_scoped_by_business_and_kind():
    a = imports.customer_id_for("business-A", "CLI-1")
    assert a == imports.customer_id_for("business-A", "CLI-1")
    assert a != imports.customer_id_for("business-B", "CLI-1")  # same externalId, other business
    assert a != imports.customer_id_for("business-A", "CLI-2")
    assert imports.customer_id_for("business-A", "X") != imports.charge_id_for("business-A", "X")  # kinds never collide


def test_a_delimiter_in_an_external_id_cannot_forge_another_identity():
    assert imports.customer_id_for("a\ncustomer\nb", "c") != imports.customer_id_for("a", "b\ncustomer\nc")


# --- customer compatibility ---------------------------------------------------

@pytest.mark.parametrize("existing_name,existing_email,name,email,conflict", [
    ("Uno", "a@x.com", "Uno", "a@x.com", False),
    ("Uno", None, "Uno", "a@x.com", False),      # a missing email never contradicts
    ("Uno", "a@x.com", "Uno", None, False),
    ("Uno", None, "Uno", None, False),
    ("Uno", "a@x.com", "Uno", "b@x.com", True),  # both present and different
    ("Uno", "a@x.com", "Dos", "a@x.com", True),  # different name
    ("Uno", None, "Dos", None, True),
])
def test_customer_identity_compatibility(existing_name, existing_email, name, email, conflict):
    assert imports.customer_identity_conflict(existing_name, existing_email, name, email) is conflict


# --- per-row decision ---------------------------------------------------------

def test_a_brand_new_row_creates_both():
    decision = imports.decide_row(row(), None, None, CID)
    assert decision == {"conflict": None, "createCustomer": True, "createCharge": True}


def test_repeat_customer_with_the_same_identity_is_reused_and_only_the_charge_is_created():
    decision = imports.decide_row(row(), stored_customer(), None, CID)
    assert decision == {"conflict": None, "createCustomer": False, "createCharge": True}


def test_an_identical_existing_charge_is_reused_not_recreated():
    decision = imports.decide_row(row(), stored_customer(), stored_charge(), CID)
    assert decision == {"conflict": None, "createCustomer": False, "createCharge": False}


def test_conflicting_customer_identity_blocks_the_row_and_creates_nothing():
    decision = imports.decide_row(row(name="Otro Nombre"), stored_customer(), None, CID)
    assert decision == {"conflict": "customer_identity_conflict", "createCustomer": False, "createCharge": False}


@pytest.mark.parametrize("change", [
    {"amount": 1}, {"description": "Otro concepto"}, {"due": "2026-10-01"}, {"customer_ext": "CLI-OTRO"}, {"currency": "USD"},
])
def test_an_existing_charge_with_a_different_payload_blocks_the_row(change):
    decision = imports.decide_row(row(), stored_customer(), stored_charge(**change), CID)
    assert decision["conflict"] == "charge_payload_conflict"
    assert decision["createCustomer"] is False and decision["createCharge"] is False


def test_a_cancelled_existing_charge_is_never_reopened_by_an_import():
    decision = imports.decide_row(row(), stored_customer(), stored_charge(cancelled_at="2026-09-01T00:00:00+00:00"), CID)
    assert decision["conflict"] == "charge_cancelled"


def test_an_existing_charge_without_its_customer_is_a_conflict_not_a_silent_repair():
    decision = imports.decide_row(row(), None, stored_charge(), CID)
    assert decision["conflict"] == "customer_missing"


def test_customer_conflict_wins_over_charge_state():
    decision = imports.decide_row(row(name="Otro"), stored_customer(), stored_charge(amount=1), CID)
    assert decision["conflict"] == "customer_identity_conflict"


# --- stored-row revalidation --------------------------------------------------

def _stored(rows):
    # Deep copies: each case mutates its own stored rows without touching the
    # shared fixtures the next assertion builds its summary from.
    return [{"sourceRow": r["sourceRow"], "valid": True, "normalized": copy.deepcopy(r), "issues": []} for r in rows]


def _summary(rows):
    return {"inputRows": len(rows), "validRows": len(rows), "errorRows": 0, "totalMinor": sum(r["charge"]["amountMinor"] for r in rows)}


def test_a_clean_stored_batch_revalidates():
    rows = [row(2, charge="F-1"), row(3, ext="CLI-2", name="Dos", email=None, charge="F-2", amount=5)]
    assert imports.revalidate_stored_rows(_stored(rows), _summary(rows)) is True


def test_revalidation_rejects_tampered_or_inconsistent_stored_rows():
    rows = [row(2, charge="F-1"), row(3, charge="F-2")]
    good = _stored(rows)
    assert imports.revalidate_stored_rows(good, _summary(rows)) is True

    tampered_amount = _stored(rows)
    tampered_amount[0]["normalized"]["charge"]["amountMinor"] = 0
    assert imports.revalidate_stored_rows(tampered_amount, _summary(rows)) is False

    duplicate_charge = _stored([row(2, charge="F-1"), row(3, charge="F-1")])
    assert imports.revalidate_stored_rows(duplicate_charge, _summary(rows)) is False

    ambiguous_customer = _stored([row(2, charge="F-1"), row(3, name="Otro Nombre", charge="F-2")])
    assert imports.revalidate_stored_rows(ambiguous_customer, _summary(rows)) is False

    marked_invalid = _stored(rows)
    marked_invalid[1]["valid"] = False
    assert imports.revalidate_stored_rows(marked_invalid, _summary(rows)) is False

    assert imports.revalidate_stored_rows(good, {**_summary(rows), "totalMinor": 1}) is False   # total no longer matches
    assert imports.revalidate_stored_rows(good[:1], _summary(rows)) is False                   # a row is missing
    assert imports.revalidate_stored_rows([{"valid": True, "normalized": None}], {"inputRows": 1, "validRows": 1, "errorRows": 0, "totalMinor": 0}) is False


def test_revalidation_never_accepts_a_float_amount():
    rows = [row(2, charge="F-1")]
    stored = _stored(rows)
    stored[0]["normalized"]["charge"]["amountMinor"] = 12.5
    assert imports.revalidate_stored_rows(stored, _summary(rows)) is False
