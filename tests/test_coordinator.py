"""Tests for the PostNL coordinator helpers and transform_shipment."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.postnl.const import ParcelStatus
from custom_components.postnl.coordinator import (
    _DUTCH_MONTHS,
    PostNLCoordinator,
    _convert_native_dimensions,
    _delivery_dt,
    _refresh_interval,
    extract_letters,
    map_parcel_status,
    normalize_parcel,
    parse_letter_date,
    sort_parcels_by_ts,
)


# ---------------------------------------------------------------------------
# _delivery_dt
# ---------------------------------------------------------------------------


def test_delivery_dt_parses_iso_with_tz():
    parcel = {"delivered_at": "2026-06-12T10:00:00+02:00"}
    dt = _delivery_dt(parcel)
    assert dt is not None
    assert dt.year == 2026 and dt.hour == 10


def test_delivery_dt_assigns_utc_when_naive():
    parcel = {"delivered_at": "2026-06-12T10:00:00"}
    dt = _delivery_dt(parcel)
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.tzinfo.utcoffset(dt).total_seconds() == 0


def test_delivery_dt_handles_z_suffix():
    parcel = {"delivered_at": "2026-06-12T10:00:00Z"}
    dt = _delivery_dt(parcel)
    assert dt == datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)


def test_delivery_dt_returns_none_for_missing():
    assert _delivery_dt({}) is None
    assert _delivery_dt({"delivered_at": None}) is None
    assert _delivery_dt({"delivered_at": ""}) is None


def test_delivery_dt_returns_none_for_garbage():
    assert _delivery_dt({"delivered_at": "not a date"}) is None


# ---------------------------------------------------------------------------
# map_parcel_status
# ---------------------------------------------------------------------------


def test_map_parcel_status_delivered_flag_short_circuits():
    assert map_parcel_status({"delivered": True, "status_message": "anything"}) == ParcelStatus.DELIVERED


def test_map_parcel_status_unknown_when_message_missing():
    assert map_parcel_status({}) == ParcelStatus.UNKNOWN
    assert map_parcel_status({"status_message": ""}) == ParcelStatus.UNKNOWN
    assert map_parcel_status({"status_message": None}) == ParcelStatus.UNKNOWN


def test_map_parcel_status_out_for_delivery_beats_in_transit():
    # "onderweg naar het bezorgadres" contains "onderweg" but must be more specific
    assert map_parcel_status({"status_message": "Pakket is onderweg naar het bezorgadres"}) == ParcelStatus.OUT_FOR_DELIVERY


def test_map_parcel_status_wordt_vandaag_bezorgd_is_out_for_delivery():
    # "wordt vandaag bezorgd" contains "bezorgd" but must NOT match DELIVERED
    assert map_parcel_status({"status_message": "Pakket wordt vandaag bezorgd"}) == ParcelStatus.OUT_FOR_DELIVERY


def test_map_parcel_status_in_transit_for_onderweg():
    assert map_parcel_status({"status_message": "Pakket is onderweg"}) == ParcelStatus.IN_TRANSIT


def test_map_parcel_status_at_pickup_point_for_postnl_punt():
    assert map_parcel_status({"status_message": "Pakket ligt klaar bij PostNL punt"}) == ParcelStatus.AT_PICKUP_POINT


def test_map_parcel_status_registered_for_aangemeld():
    assert map_parcel_status({"status_message": "Pakket is aangemeld"}) == ParcelStatus.REGISTERED


def test_map_parcel_status_unknown_for_unmapped_string():
    assert map_parcel_status({"status_message": "Verstuurd via warpdrive"}) == ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# normalize_parcel
# ---------------------------------------------------------------------------


def test_normalize_parcel_canonical_top_level_keys():
    parcel = normalize_parcel({
        "barcode": "3SXYZ",
        "source_display_name": "Bol.com",
        "url": "https://example.com",
        "delivered": False,
        "status_message": "Pakket is onderweg",
        "delivery_date": None,
        "delivery_address_type": "Recipient",
        "planned_from": "2026-06-20T09:00:00Z",
        "planned_to": "2026-06-20T17:00:00Z",
    })
    assert parcel["carrier"] == "PostNL"
    assert parcel["barcode"] == "3SXYZ"
    assert parcel["sender"] == "Bol.com"
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Pakket is onderweg"
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["planned_from"] == "2026-06-20T09:00:00Z"
    assert parcel["planned_to"] == "2026-06-20T17:00:00Z"
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["url"] == "https://example.com"
    assert "status_message" not in parcel  # original lives only under raw
    assert parcel["raw"]["status_message"] == "Pakket is onderweg"


def test_normalize_parcel_pickup_detected_for_service_point():
    parcel = normalize_parcel({
        "barcode": "X",
        "delivered": False,
        "delivery_address_type": "ServicePoint",
        "status_message": "Pakket is onderweg",
    })
    assert parcel["pickup"] is True


def test_normalize_parcel_delivered_window_cleared():
    parcel = normalize_parcel({
        "barcode": "X",
        "delivered": True,
        "delivery_date": "2026-06-20T10:00:00Z",
        "status_message": "Pakket is bezorgd",
        "planned_from": "2026-06-20T09:00:00Z",
        "planned_to": "2026-06-20T11:00:00Z",
    })
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered_at"] == "2026-06-20T10:00:00Z"
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_normalize_parcel_passes_receiver_through():
    parcel = normalize_parcel({
        "barcode": "3SXYZ",
        "delivered": False,
        "status_message": "Pakket is onderweg",
        "receiver": "Peter",
    })
    assert parcel["receiver"] == "Peter"


def test_normalize_parcel_weight_and_dimensions_from_native():
    """Canonical weight (kg) + dimensions (cm + text) derive from native g + mm."""
    parcel = normalize_parcel({
        "barcode": "3SXYZ",
        "delivered": False,
        "status_message": "Pakket is onderweg",
        "dimensions": {"weight": 1500, "depth": 300, "width": 200, "height": 150},
    })
    assert parcel["weight"] == 1.5
    assert parcel["dimensions"] == {
        "length": 30.0,
        "width": 20.0,
        "height": 15.0,
        "text": "30 x 20 x 15 cm",
    }
    # Native dimensions stay on ``raw`` for power users.
    assert parcel["raw"]["dimensions"] == {
        "weight": 1500, "depth": 300, "width": 200, "height": 150,
    }


def test_normalize_parcel_weight_and_dimensions_none_when_native_missing():
    parcel = normalize_parcel({
        "barcode": "3SXYZ",
        "delivered": True,
        "status_message": "Pakket is bezorgd",
    })
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None


# ---------------------------------------------------------------------------
# _convert_native_dimensions
# ---------------------------------------------------------------------------


def test_convert_native_dimensions_converts_g_to_kg_and_mm_to_cm():
    native = {"weight": 1500, "depth": 300, "width": 200, "height": 150}
    weight, canonical = _convert_native_dimensions(native)
    assert weight == 1.5
    assert canonical == {
        "length": 30.0,
        "width": 20.0,
        "height": 15.0,
        "text": "30 x 20 x 15 cm",
    }


def test_convert_native_dimensions_handles_weight_only():
    weight, canonical = _convert_native_dimensions({"weight": 800})
    assert weight == 0.8
    assert canonical is None


def test_convert_native_dimensions_returns_none_for_empty_input():
    assert _convert_native_dimensions(None) == (None, None)
    assert _convert_native_dimensions({}) == (None, None)


def test_convert_native_dimensions_rounds_text_to_integers():
    """The text variant always renders integer cm, even for fractional values."""
    native = {"weight": 100, "depth": 254, "width": 124, "height": 76}
    _, canonical = _convert_native_dimensions(native)
    assert canonical["text"] == "25 x 12 x 8 cm"


# ---------------------------------------------------------------------------
# _refresh_interval
# ---------------------------------------------------------------------------


def test_refresh_interval_defaults_to_30_minutes_when_option_unset():
    entry = MagicMock()
    entry.options = {}
    assert _refresh_interval(entry).total_seconds() == 30 * 60


def test_refresh_interval_reads_from_options():
    entry = MagicMock()
    entry.options = {"refresh_interval": 60}
    assert _refresh_interval(entry).total_seconds() == 60 * 60


# ---------------------------------------------------------------------------
# parse_letter_date
# ---------------------------------------------------------------------------


def _today(year: int = 2026, month: int = 6, day: int = 16) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def test_parse_letter_date_today_uses_current_year():
    assert parse_letter_date("16 juni", today=_today()) == "2026-06-16"


def test_parse_letter_date_future_month_within_window_keeps_year():
    # 1 July is 15 days ahead of 16 June → still this year
    assert parse_letter_date("1 juli", today=_today()) == "2026-07-01"


def test_parse_letter_date_far_future_rolls_back_year():
    # 30 December seen on 16 June must belong to last December
    assert parse_letter_date("30 december", today=_today()) == "2025-12-30"


def test_parse_letter_date_returns_none_for_invalid():
    assert parse_letter_date("", today=_today()) is None
    assert parse_letter_date(None, today=_today()) is None
    assert parse_letter_date("garbage", today=_today()) is None
    assert parse_letter_date("31 februari", today=_today()) is None  # 31 Feb doesn't exist
    assert parse_letter_date("12 unknownmonth", today=_today()) is None
    assert parse_letter_date("notanumber juni", today=_today()) is None


def test_dutch_months_dict_has_twelve_months():
    assert len(_DUTCH_MONTHS) == 12
    assert _DUTCH_MONTHS["januari"] == 1
    assert _DUTCH_MONTHS["december"] == 12


# ---------------------------------------------------------------------------
# extract_letters
# ---------------------------------------------------------------------------


def _sdui_payload(letters: list[dict]) -> dict:
    return {
        "screen": {
            "sections": [
                {"type": "List", "items": [{"type": "Text"}]},  # ignored
                {"type": "Grid", "items": letters},
                {"type": "List", "items": [{"type": "Default"}]},  # ignored
            ]
        }
    }


def test_extract_letters_picks_up_letter_items():
    payload = _sdui_payload([
        {
            "type": "Letter",
            "editId": "ABC1",
            "title": "16 juni",
            "isUnread": False,
            "image": {"url": "https://example.com/a"},
        },
        {
            "type": "Letter",
            "editId": "ABC2",
            "title": "15 juni",
            "isUnread": True,
            "image": {"url": "https://example.com/b"},
        },
    ])
    letters = extract_letters(payload, today=_today())
    assert len(letters) == 2
    assert letters[0]["id"] == "ABC1"
    assert letters[0]["title"] == "16 juni"
    assert letters[0]["date"] == "2026-06-16"
    assert letters[0]["unread"] is False
    assert letters[0]["image_url"] == "https://example.com/a"
    assert letters[1]["unread"] is True


def test_extract_letters_ignores_non_letter_items():
    payload = _sdui_payload([{"type": "TextListItem", "title": "Header"}])
    assert extract_letters(payload, today=_today()) == []


def test_extract_letters_returns_empty_for_missing_screen():
    assert extract_letters({}, today=_today()) == []
    assert extract_letters(None, today=_today()) == []
    assert extract_letters({"screen": {}}, today=_today()) == []


def test_extract_letters_handles_missing_image_block():
    payload = _sdui_payload([
        {"type": "Letter", "editId": "X", "title": "16 juni", "isUnread": False},
    ])
    letters = extract_letters(payload, today=_today())
    assert letters[0]["image_url"] is None


# ---------------------------------------------------------------------------
# transform_shipment
# ---------------------------------------------------------------------------


def _make_coordinator(hass):
    entry = MagicMock()
    entry.options = {}
    coordinator = PostNLCoordinator(hass, entry)
    coordinator.jouw_api = MagicMock()
    return coordinator


async def test_transform_shipment_short_circuits_for_delivered(hass):
    coordinator = _make_coordinator(hass)
    shipment = {
        "key": "K1",
        "barcode": "3SABC",
        "title": "Online Retailer",
        "detailsUrl": "https://example.com",
        "shipmentType": "Parcel",
        "receiverTitle": "Peter ",
        "sourceDisplayName": "Brand",
        "deliveredTimeStamp": "2026-06-15T14:00:00Z",
        "deliveryAddressType": "ADDRESS",
        "delivered": True,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["barcode"] == "3SABC"
    assert parcel["delivered"] is True
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Pakket is bezorgd"
    # No track_and_trace call should be made for delivered shipments
    coordinator.jouw_api.track_and_trace.assert_not_called()


async def test_transform_shipment_fetches_planned_window_from_route_information(hass):
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SABC": {
                "statusPhase": {"message": "OP_WEG_VAN_AFZENDER"},
                "routeInformation": {
                    "plannedDeliveryTime": "2026-06-17T15:00:00Z",
                    "plannedDeliveryTimeWindow": {
                        "startDateTime": "2026-06-17T14:00:00Z",
                        "endDateTime": "2026-06-17T16:00:00Z",
                    },
                    "expectedDeliveryTime": "2026-06-17T15:15:00Z",
                },
            }
        }
    })
    shipment = {
        "key": "K2",
        "barcode": "3SABC",
        "title": "Brand",
        "detailsUrl": None,
        "delivered": False,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["raw_status"] == "OP_WEG_VAN_AFZENDER"
    assert parcel["planned_from"] == "2026-06-17T14:00:00Z"
    assert parcel["planned_to"] == "2026-06-17T16:00:00Z"
    assert parcel["raw"]["expected_datetime"] == "2026-06-17T15:15:00Z"


async def test_transform_shipment_falls_back_to_eta_window(hass):
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SXYZ": {
                "statusPhase": {"message": "BEZIG_MET_BEZORGEN"},
                "eta": {
                    "start": "2026-06-17T11:00:00Z",
                    "end": "2026-06-17T13:00:00Z",
                },
            }
        }
    })
    shipment = {
        "key": "K3",
        "barcode": "3SXYZ",
        "title": "Brand",
        "delivered": False,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["raw_status"] == "BEZIG_MET_BEZORGEN"
    assert parcel["planned_from"] == "2026-06-17T11:00:00Z"
    assert parcel["planned_to"] == "2026-06-17T13:00:00Z"


async def test_transform_shipment_falls_back_to_delivery_window_strings(hass):
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SDEF": {
                "statusPhase": {"message": "VERWACHT"},
                # No routeInformation, no eta
            }
        }
    })
    shipment = {
        "key": "K4",
        "barcode": "3SDEF",
        "title": "Brand",
        "delivered": False,
        "deliveryWindowFrom": "2026-06-18T09:00:00Z",
        "deliveryWindowTo": "2026-06-18T17:00:00Z",
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["planned_from"] == "2026-06-18T09:00:00Z"
    assert parcel["planned_to"] == "2026-06-18T17:00:00Z"


async def test_transform_shipment_receiver_from_recipient_person_name(hass):
    """Active path picks up recipient name from colli.recipient.names.personName."""
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SXYZ": {
                "statusPhase": {"message": "ONDERWEG"},
                "recipient": {"names": {"personName": "Peter Nijssen"}},
            }
        }
    })
    shipment = {
        "key": "K",
        "barcode": "3SXYZ",
        "title": "Brand",
        "receiverTitle": "Fallback",
        "delivered": False,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["receiver"] == "Peter Nijssen"


async def test_transform_shipment_receiver_falls_back_to_receiver_title(hass):
    """When colli.recipient.personName is missing, fall back to GraphQL receiverTitle."""
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SXYZ": {
                "statusPhase": {"message": "ONDERWEG"},
            }
        }
    })
    shipment = {
        "key": "K",
        "barcode": "3SXYZ",
        "title": "Brand",
        "receiverTitle": "Fallback Name",
        "delivered": False,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["receiver"] == "Fallback Name"


async def test_transform_shipment_delivered_receiver_uses_receiver_title(hass):
    coordinator = _make_coordinator(hass)
    shipment = {
        "key": "K",
        "barcode": "3SDEL",
        "title": "Brand",
        "receiverTitle": "Peter ",
        "delivered": True,
        "deliveredTimeStamp": "2026-06-15T14:00:00Z",
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["receiver"] == "Peter"
    # Delivered path skips Track & Trace, so no weight / dimensions available.
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None


async def test_transform_shipment_extracts_native_dimensions_from_colli(hass):
    """colli.details.dimensions surfaces as native g+mm on raw and converted on the top level."""
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={
        "colli": {
            "3SXYZ": {
                "statusPhase": {"message": "ONDERWEG"},
                "details": {
                    "dimensions": {
                        "weight": 1500, "depth": 300, "width": 200, "height": 150,
                    },
                },
            }
        }
    })
    shipment = {
        "key": "K",
        "barcode": "3SXYZ",
        "title": "Brand",
        "delivered": False,
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["weight"] == 1.5
    assert parcel["dimensions"]["text"] == "30 x 20 x 15 cm"
    assert parcel["raw"]["dimensions"]["weight"] == 1500
    assert parcel["raw"]["dimensions"]["depth"] == 300


# ---------------------------------------------------------------------------
# _fire_change_events
# ---------------------------------------------------------------------------


def _capture(hass, event_type: str) -> list:
    events: list = []
    hass.bus.async_listen(event_type, events.append)
    return events


def _norm(barcode: str, status_message: str, *, delivered: bool = False) -> dict:
    return normalize_parcel({
        "barcode": barcode,
        "delivered": delivered,
        "status_message": status_message,
    })


async def test_fire_change_events_silent_on_first_refresh(hass):
    coordinator = _make_coordinator(hass)
    reg = _capture(hass, "postnl_parcel_registered")
    chg = _capture(hass, "postnl_parcel_status_changed")

    # _known_state is None on a fresh coordinator → suppress.
    coordinator._fire_change_events([_norm("A", "Pakket is onderweg")])
    await hass.async_block_till_done()

    assert reg == []
    assert chg == []


async def test_fire_change_events_emits_registered_for_new_barcode(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    captured = _capture(hass, "postnl_parcel_registered")

    coordinator._fire_change_events([
        _norm("A", "Pakket is onderweg"),
        _norm("NEW", "Pakket is aangemeld"),
    ])
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["barcode"] == "NEW"
    assert payload["status"] == ParcelStatus.REGISTERED
    assert payload["carrier"] == "PostNL"


async def test_fire_change_events_emits_status_changed(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    captured = _capture(hass, "postnl_parcel_status_changed")

    coordinator._fire_change_events([_norm("A", "Pakket wordt vandaag bezorgd")])
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["barcode"] == "A"
    assert payload["old_status"] == ParcelStatus.IN_TRANSIT
    assert payload["new_status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert payload["status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_fire_change_events_no_event_when_status_unchanged(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    reg = _capture(hass, "postnl_parcel_registered")
    chg = _capture(hass, "postnl_parcel_status_changed")

    coordinator._fire_change_events([_norm("A", "Pakket is onderweg")])
    await hass.async_block_till_done()

    assert reg == []
    assert chg == []


async def test_fire_change_events_intra_in_transit_does_not_fire(hass):
    """Different Dutch strings mapping to the same canonical status fire nothing.

    "ontvangen" and "gesorteerd" both map to IN_TRANSIT — the raw_status
    changes but the normalised status does not, so no event is emitted.
    """
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    captured = _capture(hass, "postnl_parcel_status_changed")

    coordinator._fire_change_events([_norm("A", "Pakket is gesorteerd in het sorteercentrum")])
    await hass.async_block_till_done()

    assert captured == []


async def test_fire_change_events_skips_parcels_without_barcode(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {}
    captured = _capture(hass, "postnl_parcel_registered")

    coordinator._fire_change_events([_norm("", "Pakket is onderweg")])
    await hass.async_block_till_done()

    assert captured == []


# ---------------------------------------------------------------------------
# Event firing — parcel_delivery_time_changed
# ---------------------------------------------------------------------------


def _norm_with_window(
    barcode: str, planned_from: str | None, planned_to: str | None
) -> dict:
    return normalize_parcel({
        "barcode": barcode,
        "delivered": False,
        "status_message": "Pakket is onderweg",
        "planned_from": planned_from,
        "planned_to": planned_to,
    })


async def test_fire_change_events_delivery_time_changed_when_window_appears(hass):
    """A barcode whose planned_from gains a value fires delivery_time_changed."""
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {"A": (None, None)}
    captured = _capture(hass, "postnl_parcel_delivery_time_changed")

    coordinator._fire_change_events([
        _norm_with_window("A", "2026-06-17T14:00:00Z", "2026-06-17T16:00:00Z"),
    ])
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["barcode"] == "A"
    assert payload["old_planned_from"] is None
    assert payload["new_planned_from"] == "2026-06-17T14:00:00Z"


async def test_fire_change_events_delivery_time_changed_when_window_shifts(hass):
    """A barcode whose planned_from changes to a different value fires the event."""
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {
        "A": ("2026-06-17T10:00:00Z", "2026-06-17T12:00:00Z"),
    }
    captured = _capture(hass, "postnl_parcel_delivery_time_changed")

    coordinator._fire_change_events([
        _norm_with_window("A", "2026-06-17T14:00:00Z", "2026-06-17T16:00:00Z"),
    ])
    await hass.async_block_till_done()

    assert len(captured) == 1
    assert captured[0].data["old_planned_from"] == "2026-06-17T10:00:00Z"
    assert captured[0].data["new_planned_from"] == "2026-06-17T14:00:00Z"


async def test_fire_change_events_no_delivery_time_event_when_window_clears(hass):
    """value -> null transitions are intentionally silent."""
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {
        "A": ("2026-06-17T10:00:00Z", "2026-06-17T12:00:00Z"),
    }
    captured = _capture(hass, "postnl_parcel_delivery_time_changed")

    coordinator._fire_change_events([_norm_with_window("A", None, None)])
    await hass.async_block_till_done()

    assert captured == []


async def test_fire_change_events_no_delivery_time_event_when_unchanged(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {
        "A": ("2026-06-17T14:00:00Z", "2026-06-17T16:00:00Z"),
    }
    captured = _capture(hass, "postnl_parcel_delivery_time_changed")

    coordinator._fire_change_events([
        _norm_with_window("A", "2026-06-17T14:00:00Z", "2026-06-17T16:00:00Z"),
    ])
    await hass.async_block_till_done()

    assert captured == []


# ---------------------------------------------------------------------------
# _fire_letter_events
# ---------------------------------------------------------------------------


def _letter(letter_id: str, title: str = "16 juni", *, unread: bool = True, image_url: str | None = "https://example.com/a.jpg", date: str | None = "2026-06-16") -> dict:
    return {
        "id": letter_id,
        "title": title,
        "date": date,
        "unread": unread,
        "image_url": image_url,
    }


async def test_fire_letter_events_silent_on_first_refresh(hass):
    coordinator = _make_coordinator(hass)
    captured = _capture(hass, "postnl_letter_announced")

    # _known_letter_ids is None on a fresh coordinator → suppress.
    coordinator._fire_letter_events([_letter("ABC1"), _letter("ABC2")])
    await hass.async_block_till_done()

    assert captured == []


async def test_fire_letter_events_emits_announced_for_new_id(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_letter_ids = {"ABC1"}
    captured = _capture(hass, "postnl_letter_announced")

    coordinator._fire_letter_events([_letter("ABC1"), _letter("NEW", title="17 juni")])
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["id"] == "NEW"
    assert payload["title"] == "17 juni"
    assert payload["image_url"] == "https://example.com/a.jpg"
    assert payload["carrier"] == "PostNL"


async def test_fire_letter_events_no_event_when_letter_unchanged(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_letter_ids = {"ABC1"}
    captured = _capture(hass, "postnl_letter_announced")

    coordinator._fire_letter_events([_letter("ABC1")])
    await hass.async_block_till_done()

    assert captured == []


async def test_fire_letter_events_skips_letters_without_id(hass):
    coordinator = _make_coordinator(hass)
    coordinator._known_letter_ids = set()
    captured = _capture(hass, "postnl_letter_announced")

    coordinator._fire_letter_events([_letter("")])
    await hass.async_block_till_done()

    assert captured == []


async def test_transform_shipment_handles_missing_colli_entry(hass):
    coordinator = _make_coordinator(hass)
    coordinator.jouw_api.track_and_trace = MagicMock(return_value={"colli": {}})
    shipment = {
        "key": "K5",
        "barcode": "3SNOPE",
        "title": "Brand",
        "delivered": False,
        "deliveryWindowFrom": "2026-06-20T09:00:00Z",
        "deliveryWindowTo": "2026-06-20T17:00:00Z",
    }
    parcel = await coordinator.transform_shipment(shipment)
    assert parcel["raw_status"] == "Unknown"
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["planned_from"] == "2026-06-20T09:00:00Z"


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def _ts_parcel(barcode: str, planned_from: str | None = None, delivered_at: str | None = None) -> dict:
    return {
        "barcode": barcode,
        "planned_from": planned_from,
        "delivered_at": delivered_at,
    }


def test_sort_orders_ascending_by_planned_from():
    parcels = [
        _ts_parcel("late", planned_from="2026-06-15T10:00:00+00:00"),
        _ts_parcel("early", planned_from="2026-06-13T08:00:00+00:00"),
        _ts_parcel("mid", planned_from="2026-06-14T12:00:00+00:00"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["early", "mid", "late"]


def test_sort_orders_descending_for_delivered_at():
    parcels = [
        _ts_parcel("oldest", delivered_at="2026-06-13T08:00:00+00:00"),
        _ts_parcel("newest", delivered_at="2026-06-15T10:00:00+00:00"),
        _ts_parcel("mid", delivered_at="2026-06-14T12:00:00+00:00"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)]
    assert ordered == ["newest", "mid", "oldest"]


def test_sort_keeps_missing_or_garbage_timestamps_at_end():
    parcels = [
        _ts_parcel("no-ts"),
        _ts_parcel("garbage", planned_from="not-a-date"),
        _ts_parcel("early", planned_from="2026-06-13T08:00:00+00:00"),
        _ts_parcel("late", planned_from="2026-06-15T10:00:00+00:00"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered[:2] == ["early", "late"]
    assert set(ordered[2:]) == {"no-ts", "garbage"}


def test_sort_handles_z_suffix_timestamps():
    parcels = [
        _ts_parcel("a", planned_from="2026-06-15T10:00:00Z"),
        _ts_parcel("b", planned_from="2026-06-13T10:00:00Z"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["b", "a"]


def test_sort_mixes_naive_and_aware_timestamps_without_crashing():
    # Regression: PostNL sometimes returns mixed-tz timestamps in the same
    # bucket. The sort treated naive values as UTC, otherwise Python raises
    # "can't compare offset-naive and offset-aware datetimes".
    parcels = [
        _ts_parcel("aware", planned_from="2026-06-15T10:00:00+00:00"),
        _ts_parcel("naive", planned_from="2026-06-13T10:00:00"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["naive", "aware"]


def test_sort_empty_input_returns_empty_list():
    assert sort_parcels_by_ts([], "planned_from") == []
