from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "postnl"

PLATFORMS = [
    Platform.SENSOR,
    Platform.IMAGE,
]

CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30  # minutes


class ParcelStatus(StrEnum):
    REGISTERED = "registered"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    AT_PICKUP_POINT = "at_pickup_point"
    DELIVERED = "delivered"
    RETURNING = "returning"
    PROBLEM = "problem"
    UNKNOWN = "unknown"
