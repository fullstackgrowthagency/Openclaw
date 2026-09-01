"""
Method-name constants shared by both sides of the relay -- a single
source of truth so a typo in one side can't silently create a request
the other side never recognizes. One name per non-streaming
`BrokerClient` method (see fx_bot/interfaces/broker.py); streaming
(`subscribe_quotes`/`unsubscribe_quotes`) is request/ack-only here since
the actual stream of ticks arrives as `quote` events, not responses.
"""
from __future__ import annotations


class RequestMethod:
    GET_ACCOUNT_EQUITY = "get_account_equity"
    GET_FREE_MARGIN = "get_free_margin"
    GET_POSITIONS = "get_positions"
    GET_SNAPSHOT = "get_snapshot"
    GET_BARS = "get_bars"
    SUBSCRIBE_QUOTES = "subscribe_quotes"
    UNSUBSCRIBE_QUOTES = "unsubscribe_quotes"
    PLACE_ORDER = "place_order"
    CANCEL_ORDER = "cancel_order"
    MODIFY_ORDER = "modify_order"
    GET_ORDER_STATUS = "get_order_status"
    POLL_FILLS = "poll_fills"

    ALL = (
        GET_ACCOUNT_EQUITY, GET_FREE_MARGIN, GET_POSITIONS, GET_SNAPSHOT, GET_BARS,
        SUBSCRIBE_QUOTES, UNSUBSCRIBE_QUOTES, PLACE_ORDER, CANCEL_ORDER, MODIFY_ORDER,
        GET_ORDER_STATUS, POLL_FILLS,
    )


class EventMethod:
    """One-way, connector-pushed frames -- no request ever precedes these."""
    QUOTE = "quote"
    POSITION_UPDATE = "position_update"
    ORDER_UPDATE = "order_update"
    HEARTBEAT = "heartbeat"
    MT5_DISCONNECTED = "mt5_disconnected"
    MT5_RECONNECTED = "mt5_reconnected"

    ALL = (QUOTE, POSITION_UPDATE, ORDER_UPDATE, HEARTBEAT, MT5_DISCONNECTED, MT5_RECONNECTED)
