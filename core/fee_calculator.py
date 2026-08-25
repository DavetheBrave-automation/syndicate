"""
core/fee_calculator.py — Kalshi taker fee calculations.

Kalshi charges a 7% taker fee on the notional traded.
Notional = quantity × min(yes_price, no_price) in dollars.

This is the ONLY place where the fee rate is defined — all other modules
import from here. Change FEE_RATE here to update the entire system.

Source: Kalshi fee schedule, retail tier (Advanced account).
Confidence: MEDIUM-HIGH — verify against live account balance before go-live.
Audit: audits/2026-04-16_kalshi_fees.md
"""

FEE_RATE = 0.07  # 7% taker fee on min(yes_price, no_price) notional


def calculate_fee(quantity: int, price_cents: int) -> float:
    """
    Fee for ONE leg (entry OR exit) of a trade.

    Args:
        quantity:    number of contracts
        price_cents: YES price in cents (0–100), regardless of which side traded

    Returns:
        fee in dollars (rounded to 4 decimal places)

    Formula:
        fee = FEE_RATE × quantity × min(yes_price, 1 - yes_price)

    Examples:
        YES trade, 135 contracts @ 74¢:  0.07 × 135 × 0.26 = $2.457
        NO  trade, 135 contracts @ 26¢ NO (yes=74¢): same result
        YES trade,   5 contracts @ 74¢:  0.07 × 5 × 0.26  = $0.091
    """
    yes_price = price_cents / 100.0
    no_price  = 1.0 - yes_price
    return round(FEE_RATE * quantity * min(yes_price, no_price), 4)


def calculate_roundtrip_fee(quantity: int, entry_cents: int, exit_cents: int) -> float:
    """
    Total fees for both entry AND exit legs.

    Args:
        quantity:     number of contracts
        entry_cents:  YES price at entry in cents
        exit_cents:   YES price at exit in cents

    Returns:
        total round-trip fee in dollars
    """
    return round(calculate_fee(quantity, entry_cents) + calculate_fee(quantity, exit_cents), 4)


def calculate_chunked_fee(chunks: list[int], price_cents: int) -> float:
    """
    Total fee for a chunked order (multiple sub-orders all at the same price).

    Args:
        chunks:      list of chunk sizes (e.g. [99, 99, 36])
        price_cents: YES price in cents

    Returns:
        total fee in dollars for all chunks combined
    """
    return round(sum(calculate_fee(c, price_cents) for c in chunks), 4)


def fee_drag_pct(quantity: int, entry_cents: int, exit_cents: int) -> float:
    """
    Round-trip fee expressed as a percentage of the entry stake.

    Useful for comparing fee drag to agent edge_pct.

    Args:
        quantity:     number of contracts
        entry_cents:  YES price at entry in cents
        exit_cents:   YES price at exit in cents

    Returns:
        fee drag as a percentage (e.g. 4.1 means 4.1% of stake goes to fees)
    """
    display_entry = min(entry_cents, 100 - entry_cents)  # actual cost per contract
    stake = quantity * display_entry / 100.0
    if stake <= 0:
        return 0.0
    rt_fee = calculate_roundtrip_fee(quantity, entry_cents, exit_cents)
    return round(rt_fee / stake * 100.0, 2)
